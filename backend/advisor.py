"""営業部長（アドバイザー）

全案件を見張って、案件ごとの助言と、チーム全体の総評を出す。
考え方は Salesforce「営業改革のコンパス」の要点をワコーメタル向けに置き換えたもの:
  - 段階は「営業がしたこと」ではなく「お客様の状態」で見る
  - 段階ごとの確認項目（BANT・キーマン・競合・受注予定日）を埋めることが前進
  - 「買わない理由」を一つずつ消す
  - 失注は「競合負け」と「何も買わなかった」に分け、後者は再アプローチ
  - 抜け漏れ（滞留・予定日ずれ・段階の後退・理由なし失注）を毎日つぶす
  - 見込み額は 金額×確度。パイプラインは目標の3倍・受注率30%が目安
  - 受注も失注も感想戦（振り返り）を残す

ルールの助言はAIなしで即時に出す。AIの相談（案件・チーム）は任意。
"""
import json
import os
import re
from datetime import date, timedelta
from typing import Callable, Optional

from fastapi import APIRouter, Depends, HTTPException

import profiles
import sales

LEVEL_ORDER = {"high": 0, "mid": 1, "low": 2, "info": 3}

# AIに渡す営業の原則（マニュアルの要約）
PLAYBOOK = """
# 当社の営業の原則（営業部長の考え方）
1. 案件の段階は「お客様の状態」で判断する。「見積を出した」ではなく「お客様が金額に納得した」。営業の行動はそれを進める手段。
2. 段階ごとの確認項目を埋めることが前進。課題認識・図面仕様・納期・予算・決め方（決裁者）・競合・選ばれる理由・キーマン・金額納得・次の進め方。
3. 営業は「買う理由」を増やすだけでなく「買わない理由」を一つずつ消す仕事。価格・要件・納期・決裁者の優先順位・関係者の納得・競合が選ばれない理由。
4. 受注予定日がない案件は後回しにされ保留化する。お客様の「この日までに」を聞き出す。
5. 同じ段階に長く止まる案件は、窓口が適切な相手でない可能性がある。キーマンへの接触・上長同行を検討する。
6. 意思決定が近い段階で上長に同行を頼まないと、決裁者の意向を外した提案になりやすい。
7. 「価格が高い」と言われたら、競合比か予算比か、お客様の想定する投資対効果はどうかを確かめる。すぐ値引きに逃げない。
8. 失注は「競合に負けた」と「お客様が何も買わなかった」に分ける。後者は宝の山。検討をやめた見込み客の多くは2年以内に他社から買うので、定期的に再アプローチする。
9. 失注理由は営業本人の申告だけでは本当の理由が隠れることがある（対応の遅さなど）。別の人が聞く・決まった時期を確認する。責めるためでなく仕組みを直すため。
10. 受注・失注のあとは感想戦（振り返り）。どの時点で何をすべきだったか、別の筋書きはあったか。
11. 名刺・訪問で得た情報は会社の資産。登録までが営業活動。
12. 営業の時間は月160時間しかない。見込みの高い顧客と案件に時間を振り向ける。ターゲットを決め、売りたくない相手には無理に売らない。
13. パイプラインは目標の3倍程度が目安（受注率30%）。受注率が低すぎれば見込みの薄い案件を追いすぎ、高すぎれば新規開拓不足。
14. 段階ごとの確度を掛けた見込み額で着地を読む。初期段階ばかりなら案件を前に進める、少なければ発掘に注力。
15. 営業力の段階: 0=誰にでも同じ説明 / 1=お客様に合わせて提案 / 2=深いヒアリングでお客様の選定基準を変える / 3=お客様社内の関係者の合意形成まで支援する。
18. 当社は営業専任がいない少人数の会社。完璧を求めず、いちばん効く一手に絞って助言する。
16. 当社（受注生産・大型筐体の板金）は図面・数量・材質・納期・塗装や表面処理の条件で見積の精度と勝ち負けが決まる。大型・一貫対応・短納期など「当社が選ばれる理由」を案件ごとに言語化する。
17. ブループリント（自社メディア）の取材先は将来のお客様候補でもある。取材で生まれた関係を営業につなげる。
"""

def playbook(own: str) -> str:
    """会社ごとに原則を合わせる（和宏電機は盤の前提。板金とブループリントの話はしない）"""
    lines = PLAYBOOK.rstrip("\n").split("\n")
    if sales.profile_key(own) == "panel":
        lines = [x for x in lines if not x.startswith(("16. ", "17. ", "18. "))] + profiles.PANEL_PLAYBOOK.split("\n")
    elif not sales.has_bp(own):
        lines = [x for x in lines if not x.startswith("17. ")]
    return "\n".join(lines) + "\n"


def _d(s: Optional[str]) -> Optional[date]:
    try:
        return date.fromisoformat((s or "")[:10]) if s else None
    except ValueError:
        return None


def _user_acts(acts: list) -> list:
    return [a for a in acts if a.get("kind") != "stage"]


def _adv(level, title, detail, principle=None, action=None):
    out = {"level": level, "title": title, "detail": detail}
    if principle:
        out["principle"] = principle
    if action:
        out["action"] = action
    return out


def deal_advice(d: dict, acts: list, settings: dict, today: Optional[date] = None) -> list:
    """1案件への助言（重要な順）。d は sales._deal_dict の結果"""
    today = today or date.today()
    p = sales.pipeline_of(d)
    stage = d["stage"]
    out = []
    ua = _user_acts(acts)
    last = _d(ua[0]["occurred_at"]) if ua else _d(d.get("created_at"))
    idle = (today - last).days if last else 0
    checks = {ck["key"]: ck for ck in p["checks"]}
    missing = d.get("checks_missing") or []
    order = p["stages"]
    idx = order.index(stage) if stage in order else 0
    stall = effective_stall(settings)

    # ── 終わった案件 ──
    if not d.get("is_open"):
        if stage == p["lost"]:
            if p["key"] == "sales" and not d.get("lost_type"):
                out.append(_adv("mid", "失注の種類を記録しましょう",
                                "「競合に負けた」のか「お客様が何も買わなかった（見送り・現状維持）」のかで次の打ち手が変わります。",
                                "失注は2種類に分ける", "失注の種類を選ぶ"))
            if d.get("lost_type") == "no_decision" and not d.get("reapproach_on"):
                out.append(_adv("mid", "再アプローチの時期を決めましょう",
                                "何も買わなかったお客様は、まだニーズを持ったままです。放っておくと他社に取られます。",
                                "見送り客の多くは2年以内に他社から買う", "再アプローチ日を入れる"))
            ra = _d(d.get("reapproach_on"))
            if ra and ra <= today:
                out.append(_adv("high", "再アプローチの時期です",
                                f"{ra.month}/{ra.day} に再アプローチ予定でした。当時の失注理由（{d.get('lost_reason') or '未記録'}）が解消できる提案を持って連絡しましょう。",
                                "失注商談は宝の山", "新しい案件として作り直す"))
            if p["key"] == "sales" and d.get("lost_reason") == "その他" and not (d.get("lost_detail") or "").strip():
                out.append(_adv("low", "失注の本当の理由を残しましょう",
                                "「その他」だけでは次に生かせません。対応のまずさが隠れていることもあるので、可能なら別の人がお客様に聞くのも手です。",
                                "失注理由は本人以外が聞くと正確"))
        closed = _d(d.get("closed_at"))
        if stage == p["won"] and p["key"] == "sales":
            rp = _d(d.get("repeat_on"))
            if rp and rp <= today + timedelta(days=14):
                out.append(_adv("mid", "そろそろ次の発注時期です",
                                f"{rp.month}/{rp.day} ごろに次回分の発注が見込まれています。数量や納期の予定を聞き、リピート案件を作っておきましょう。",
                                "リピートは先回りで声をかける", "リピート案件を作る"))
            elif not rp and not d.get("repeat_deal_id") and closed and (today - closed).days <= 60:
                out.append(_adv("low", "次の発注の見込み時期を入れておきましょう",
                                "受注生産でも同じ品物の追加・次回分は出ます。見込み時期を入れておけば、その頃に部長が声をかけます。",
                                "リピートは先回りで声をかける", "次回の発注見込みを入れる"))
        if stage == "受注" and p is sales.PANEL_SALES and not d.get("prod_deal_id") and closed and (today - closed).days <= 60:
            out.append(_adv("mid", "受注後の進行を作りましょう",
                            "承認図の提出と返却・長納期機器の手配・立会検査・出荷までを追いかけます。返却の遅れや納期の危険を部長が見張ります。",
                            "承認図の返却が納期を決める", "受注後の進行を作る"))
        if stage == "完了" and p["key"] == "prod" and closed and (today - closed).days <= 60:
            out.append(_adv("low", "次の計画を聞いておきましょう",
                            "納めた現場・エンドユーザーには増設や他の拠点の更新計画があることが多いです。試運転のお礼のときに聞きましょう。",
                            "リピートは先回りで声をかける"))
        if not (d.get("review") or "").strip() and closed and (today - closed).days <= 60:
            out.append(_adv("low", "感想戦（振り返り）を書きましょう",
                            "どの時点で何をすべきだったか・別の進め方はあったかを3行で。次の案件の勝ち筋になります。",
                            "案件感想戦", "振り返りを書く"))
        return apply_strictness(out, d, settings)

    # ── 進行中 ──
    if d.get("no_card"):
        out.append(_adv("low", "名刺をつなぎましょう",
                        "名刺（人物情報）がないとメール作成や関係者の把握ができません。名刺を登録したら「名刺をつなぐ」から選んでください。",
                        "名刺は会社の資産", "名刺をつなぐ"))
    if not d.get("next_action"):
        out.append(_adv("high", "次の一手が決まっていません",
                        "次のアクションがない案件は止まります。お客様と次の日程や宿題を合意し、期限付きで入れましょう。",
                        "段階を進めるのはお客様との合意", "次のアクションを入れる"))
    if d.get("days_in_stage", 0) >= stall and stage not in ("保留", "部材手配", "製作中"):
        out.append(_adv("high", f"「{stage}」で{d['days_in_stage']}日止まっています",
                        "同じ段階に長く止まるのは、今の窓口が決める立場にないか、お客様の優先度が下がっているサインです。"
                        "決め手を持つ人（設計・購買の責任者）に会えるか、上長同行で状況を確かめましょう。",
                        "滞留案件は窓口を疑う", "キーマンに会う段取りをする"))
    if d.get("stage_down"):
        out.append(_adv("mid", "段階が後退しています",
                        "一度進んだ案件が戻ったときは、お客様側で何かが変わっています（予算・担当・仕様）。変化の理由を確認しましょう。",
                        "フェーズダウンは要注意"))
    ec = _d(d.get("expected_close"))
    if p["key"] == "sales":
        if ec and ec < today:
            out.append(_adv("high", "受注予定日を過ぎています",
                            f"予定日 {ec.month}/{ec.day} を過ぎました。お客様の決定がずれた理由を確認し、予定日を引き直しましょう。",
                            "完了予定日の経過は抜け漏れ", "予定日を確認する"))
        elif not ec and idx >= order.index("ヒアリング") and stage != "保留":
            out.append(_adv("mid", "受注予定日（お客様が決める時期）を聞きましょう",
                            "お客様社内に「この日までに」がないと発注は後回しになり保留化しやすくなります。",
                            "受注予定日がないと保留化する", "受注予定日を入れる"))
        if (d.get("close_changes") or 0) >= 2:
            out.append(_adv("mid", f"受注予定日が{d['close_changes']}回ずれています",
                            "予定日のずれが続くのは、本当の決定者や決め手が見えていない可能性があります。決裁の流れを確かめ直しましょう。",
                            "予定日の度重なる変更は抜け漏れ"))
        if stage == "見積提出":
            if idle >= 7:
                out.append(_adv("high", f"見積提出後 {idle}日 動きがありません",
                                "見積後の沈黙は危険信号です。金額・仕様・納期・社内の決め方のどこで止まっているか、電話で確かめましょう。",
                                "買わない理由を一つずつ消す", "電話で状況確認"))
            if "keyman" in missing:
                out.append(_adv("mid", "決裁者を押さえていません",
                                "意思決定が近い段階で決裁者の意向を外すと最後に止まります。社長・部長の同行で決裁者に会いましょう。",
                                "決定前の上長同行"))
            if "price_ok" in missing and "competitor" in missing:
                out.append(_adv("mid", "相見積もりと金額感が見えていません",
                                "「高い」と言われる前に、比較相手と予算を把握しておくと値引き以外の打ち手が持てます。",
                                "価格は競合比か予算比かを確かめる"))
        if p is sales.PANEL_SALES:
            out += _panel_advice(d, stage, idx, order, missing)
        ap = (d.get("appo_at") or "")[:10]
        if ap and stage in ("テレアポ", "アポ") and ap < today.isoformat():
            out.append(_adv("mid", "アポの結果を記録しましょう",
                            f"{ap[5:7].lstrip('0')}/{ap[8:10].lstrip('0')} のアポが終わっています。会えたか・何が分かったかを記録して段階を進めましょう。"
                            + ("テレアポ業者にも結果が伝わります。" if d.get("vendor_id") else ""),
                            "初動の速さ", "結果を記録"))
        elif stage == "テレアポ" and not ap and d.get("days_in_stage", 0) >= 14:
            out.append(_adv("low", "テレアポから2週間たちました",
                            "アポにつながらないなら、資料送付やメールで接点を残すか、見送りにして再アプローチ日を決めましょう。", None))
        if stage == "名刺交換":
            em = [a for a in ua if a["kind"] == "email"]
            created = _d(d.get("created_at"))
            if not em and created and sales.business_days_between(created, today) >= 2:
                out.append(_adv("mid", "御礼がまだです",
                                "名刺交換から時間がたつほど印象は薄れます。今日中に御礼メールを送り、次の打合せを打診しましょう。",
                                "初動の速さ", "メールを作成"))
        if stage == "保留" and not d.get("next_action_due"):
            out.append(_adv("mid", "保留の再開条件と時期を決めましょう",
                            "「いつ・何がそろえば再開するか」を決めておかないと、そのまま消えます。確認日を入れておきましょう。",
                            "保留は再開条件を決める"))
        if not d.get("amount") and idx >= order.index("ヒアリング") and stage != "保留":
            out.append(_adv("low", "金額（概算）を入れましょう",
                            "概算でも金額が入ると見込み額（金額×確度）に反映され、着地が読めるようになります。",
                            "見込み額は金額×確度"))
        if idle >= 14 and stage in ("アポ", "ヒアリング") and not any(a["kind"] == "meeting" for a in ua[:5]):
            out.append(_adv("low", "電話・メールだけで進めていませんか",
                            "重要な案件は顔を合わせて課題を深掘りするほど、お客様の選定基準をこちらに寄せられます。",
                            "お客様の選定基準を変える営業"))
    elif p["key"] == "prod":
        out += _prod_advice(d, stage, today, idle)
    else:  # ブループリント
        if stage == "日程調整" and d.get("days_in_stage", 0) >= 14:
            out.append(_adv("mid", "取材日がなかなか決まりません",
                            "候補日を3つ出して選んでもらう形にすると決まりやすくなります。",
                            None, "候補日を送る"))
        if stage == "初稿提出" and idle >= 7:
            out.append(_adv("mid", f"初稿の確認待ちが {idle}日です",
                            "確認者と期限を決めて、やさしく催促しましょう。修正点が多い場合は電話で一度に聞くと早いです。",
                            None, "確認の催促"))
        if stage == "アップロード完了":
            pass
        if stage in ("取材", "初稿提出"):
            out.append(_adv("info", "取材先は将来のお客様候補です",
                            "取材で工場の課題や外注の状況が見えたら、営業の案件も作っておきましょう。",
                            "取材から営業へつなぐ"))

    # 確認項目の不足（段階に応じて）
    if missing:
        labels = [checks[k]["label"] for k in missing if k in checks]
        more = f" ほか{len(labels) - 3}つ" if len(labels) > 3 else ""
        lvl = "mid" if ((p["key"] == "sales" and idx >= order.index("ヒアリング"))
                        or (p["key"] == "prod" and stage in ("承認済", "部材手配", "立会検査", "出荷"))) else "low"
        out.append(_adv(lvl, f"確認できていない項目があと{len(missing)}個",
                        "まだ: " + "／".join(labels[:3]) + more + "。下の「確認項目」に聞き方があります。",
                        "段階は確認項目で進む", "確認項目を埋める"))
    return apply_strictness(out, d, settings)


def _panel_advice(d: dict, stage: str, idx: int, order: list, missing: list) -> list:
    """和宏電機（盤）の営業だけの助言"""
    out = []
    if d.get("same_site"):
        ids = "・".join(f"#{x}" for x in d["same_site"][:3])
        out.append(_adv("info", f"同じ現場の引合がほかにもあります（{ids}）",
                        "同じ現場に別の商社・メーカーからも引合が来ています。商流のどこが本命か・誰が決めるかを確かめ、見積の出し方をそろえましょう。",
                        "同じ現場の引合は商流を確かめる"))
    if d.get("channel") in ("重電メーカー経由", "商社経由", "ゼネコン・設備会社") and not (d.get("end_user") or d.get("site_name")) \
            and idx >= order.index("アポ"):
        out.append(_adv("low", "エンドユーザーと現場名を聞きましょう",
                        "間に入る会社がある案件は、最終的に使うお客様と現場がわかると、次の更新や他の引合につながります。",
                        "商流とエンドユーザーを押さえる"))
    if stage == "見積提出" and "long_lead" in missing:
        out.append(_adv("mid", "長納期の機器の納期を確かめましょう",
                        "変圧器・遮断器・PCSは納期が長くなることがあります。機器メーカー・商社に今の納期を確かめ、見積の納期に織り込みましょう。",
                        "長納期の機器は見積の段階で押さえる", "機器の納期を確認"))
    dl = _d(d.get("delivery_on"))
    if dl and stage in ("ヒアリング", "見積提出") and (dl - date.today()).days <= 60:
        out.append(_adv("mid", f"納期（{dl.month}/{dl.day}）まで{max(0, (dl - date.today()).days)}日です",
                        "承認図・機器の手配・製作を考えると余裕がありません。発注の時期を確かめ、機器の納期を先に押さえましょう。",
                        "承認図の返却が納期を決める", "発注時期を確認"))
    return out


PROD_BEFORE_MAKE = ("承認図作成", "承認図提出", "承認済", "部材手配")


def _prod_advice(d: dict, stage: str, today: date, idle: int) -> list:
    """受注後の進行の助言"""
    out = []
    dl = _d(d.get("delivery_on"))
    if not dl and stage not in ("据付・試運転",):
        out.append(_adv("mid", "納期（受電日）を入れましょう",
                        "納期が入っていると、承認図の返却や製作の遅れを部長が逆算して知らせます。カレンダーにも出ます。",
                        "承認図の返却が納期を決める", "納期を入れる"))
    if dl:
        left = (dl - today).days
        if left < 0 and stage not in ("据付・試運転",):
            out.append(_adv("high", f"納期（{dl.month}/{dl.day}）を過ぎています",
                            "お客様と現場の工程を確かめ、納期を引き直しましょう。遅れの理由と新しい日付を書面で残します。",
                            None, "お客様と納期を確認"))
        elif left <= 21 and stage in PROD_BEFORE_MAKE:
            out.append(_adv("high", f"納期まで{left}日なのに製作に入れていません",
                            "承認図の返却・機器の到着のどちらで止まっているかを確かめ、間に合わないならすぐお客様に相談しましょう。",
                            "承認図の返却が納期を決める", "工程を確認"))
        elif left <= 14 and stage in ("製作中", "立会検査"):
            out.append(_adv("mid", f"納期まで{left}日です",
                            "立会検査・出荷日・搬入経路とクレーンの手配を確定しましょう。",
                            None, "出荷の段取り"))
    if stage == "承認図提出" and d.get("days_in_stage", 0) >= 10:
        out.append(_adv("mid", f"承認図の返却待ちが{d['days_in_stage']}日です",
                        "返却が遅れるほど製作と納期が詰まります。返却の見込み日と、修正点があるかを電話で確かめましょう。",
                        "承認図の返却が納期を決める", "返却を催促"))
    if stage in ("承認済", "部材手配") and "long_lead" in (d.get("checks_missing") or []):
        out.append(_adv("mid", "長納期の機器を手配しましょう",
                        "変圧器・遮断器・PCSなどは発注してから数か月かかることがあります。承認が出たらすぐ発注し、回答納期を記録しましょう。",
                        "長納期の機器は見積の段階で押さえる", "機器を発注"))
    return out


def effective_stall(settings: dict) -> int:
    stall = settings.get("stall_days") or 30
    return max(stall, 60) if settings.get("strictness", "gentle") == "gentle" else stall


# ゆるめ（少人数の会社向け）で出さない指摘・弱める指摘
GENTLE_DROP = ("金額（概算）を入れましょう", "エンドユーザーと現場名を聞きましょう", "次の計画を聞いておきましょう", "電話・メールだけで進めていませんか", "受注予定日（お客様が決める時期）を聞きましょう",
               "受注予定日が", "名刺をつなぎましょう", "失注の本当の理由を残しましょう", "相見積もりと金額感が見えていません",
               "御礼がまだです")
GENTLE_SOFT = ("次の一手が決まっていません", "止まっています", "段階が後退しています", "決裁者を押さえていません",
               "失注の種類を記録しましょう", "感想戦")
LIMIT = {"gentle": 3, "normal": 5, "strict": 99}


def apply_strictness(out: list, d: dict, settings: dict) -> list:
    mode = settings.get("strictness", "gentle")
    if mode == "gentle":
        res = []
        for a in out:
            t = a["title"]
            if any(t.startswith(x) for x in GENTLE_DROP):
                continue
            if t.startswith("確認できていない項目") and d.get("stage") not in ("見積提出", "初稿提出", "質問事項送付",
                                                                         "承認済", "立会検査", "出荷"):
                continue  # ゆるめでは見積の段階まで来てから
            a = dict(a)
            if any(x in t for x in GENTLE_SOFT):
                a["level"] = "low"
            elif a["level"] == "high":
                a["level"] = "mid"
            res.append(a)
        out = res
    out = sorted(out, key=lambda a: LEVEL_ORDER[a["level"]])
    return out[:LIMIT.get(mode, 5)]


# ── チーム全体（部長の目） ──
LEAKS = [
    ("no_next", "次のアクションがない案件"),
    ("quote_wait", "ヒアリング（見積中）のまま7日以上たつ案件"),
    ("close_passed", "受注予定日を過ぎた案件"),
    ("stalled", "同じ段階に長く止まっている案件"),
    ("stage_down", "段階が後退した案件"),
    ("close_changed", "受注予定日が2回以上ずれた案件"),
    ("closing_idle", "今月受注予定なのに15日以上動きがない案件"),
    ("no_close_date", "受注予定日がない案件（ヒアリング以降）"),
    ("lost_no_reason", "失注の理由・種類が未記録"),
    ("reapproach_due", "再アプローチの時期が来た失注案件"),
    ("review_missing", "感想戦（振り返り）がない受注・失注"),
    ("no_card", "名刺がつながっていない案件"),
    ("repeat_due", "次の発注時期が近い受注案件（リピートの声かけ）"),
    ("delivery_risk", "納期が近い・過ぎたのに製作が進んでいない案件"),
    ("drawing_wait", "承認図の返却待ちが10日以上の案件"),
    ("no_prod", "受注後の進行がない受注案件"),
]
QUOTE_WAIT_DAYS = 7
GENTLE_LEAKS = {"no_next", "quote_wait", "close_passed", "stalled", "reapproach_due", "repeat_due", "lost_no_reason", "delivery_risk", "drawing_wait"}


def team_review(deals: list, acts_by_deal: dict, settings: dict, today: Optional[date] = None) -> dict:
    today = today or date.today()
    stall = effective_stall(settings)
    mode = settings.get("strictness", "gentle")
    out = {"today": today.isoformat(), "pipelines": {}, "strictness": mode}
    panel = any(sales.pipeline_of(d) is sales.PANEL_SALES for d in deals if (d.get("pipeline") or "sales") == "sales")
    if panel or any(d.get("segment") or d.get("channel") for d in deals):
        out["segments"] = mix_breakdown(deals, "segment", today)
        out["channels"] = mix_breakdown(deals, "channel", today)
    vd = [d for d in deals if d.get("vendor_id")]
    if vd:
        lin = sales.PIPELINES["sales"]["linear"]
        rows = {}
        for d in vd:
            r_ = rows.setdefault(d.get("vendor_name") or f"業者#{d['vendor_id']}",
                                 {"name": d.get("vendor_name") or f"業者#{d['vendor_id']}", "total": 0, "open": 0, "appo": 0, "won": 0, "won_amount": 0})
            r_["total"] += 1
            r_["open"] += 1 if d.get("is_open") else 0
            if d.get("appo_at") or (d["stage"] in lin and lin.index(d["stage"]) >= lin.index("アポ")):
                r_["appo"] += 1
            if d["stage"] == "受注":
                r_["won"] += 1
                r_["won_amount"] += d.get("amount") or 0
        out["vendors"] = sorted(rows.values(), key=lambda x: -x["total"])
    for key, p in sales.PIPELINES.items():
        ds = [d for d in deals if (d.get("pipeline") or "sales") == key]
        if not ds and key != "sales":
            continue
        open_ = [d for d in ds if d["is_open"]]
        leaks = {k: [] for k, _ in LEAKS}
        month_end = (today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        for d in ds:
            ua = _user_acts(acts_by_deal.get(d["id"], []))
            last = _d(ua[0]["occurred_at"]) if ua else _d(d.get("created_at"))
            idle = (today - last).days if last else 0
            ec = _d(d.get("expected_close"))
            if d["is_open"]:
                if not d.get("next_action"):
                    leaks["no_next"].append(d["id"])
                if key == "sales" and d["stage"] == "ヒアリング" and d.get("days_in_stage", 0) >= QUOTE_WAIT_DAYS:
                    leaks["quote_wait"].append(d["id"])
                if ec and ec < today and key == "sales":
                    leaks["close_passed"].append(d["id"])
                if d.get("days_in_stage", 0) >= stall and d["stage"] not in ("保留", "部材手配", "製作中"):
                    leaks["stalled"].append(d["id"])
                if key == "prod":
                    dl = _d(d.get("delivery_on"))
                    if dl and d["stage"] != "据付・試運転" and (dl < today or ((dl - today).days <= 21 and d["stage"] in PROD_BEFORE_MAKE)):
                        leaks["delivery_risk"].append(d["id"])
                    if d["stage"] == "承認図提出" and d.get("days_in_stage", 0) >= 10:
                        leaks["drawing_wait"].append(d["id"])
                if d.get("stage_down"):
                    leaks["stage_down"].append(d["id"])
                if (d.get("close_changes") or 0) >= 2:
                    leaks["close_changed"].append(d["id"])
                if ec and today <= ec <= month_end and idle >= 15:
                    leaks["closing_idle"].append(d["id"])
                if key == "sales" and not ec and d["stage"] in ("ヒアリング", "見積提出"):
                    leaks["no_close_date"].append(d["id"])
                if d.get("no_card"):
                    leaks["no_card"].append(d["id"])
            else:
                if d["stage"] == p["lost"] and key == "sales" and (not d.get("lost_reason") or not d.get("lost_type")):
                    leaks["lost_no_reason"].append(d["id"])
                ra = _d(d.get("reapproach_on"))
                if d["stage"] == p["lost"] and ra and ra <= today:
                    leaks["reapproach_due"].append(d["id"])
                closed = _d(d.get("closed_at"))
                if closed and (today - closed).days <= 60 and not (d.get("review") or "").strip():
                    leaks["review_missing"].append(d["id"])
                rp = _d(d.get("repeat_on"))
                if d["stage"] == p["won"] and rp and rp <= today + timedelta(days=14):
                    leaks["repeat_due"].append(d["id"])
                if panel and key == "sales" and d["stage"] == "受注" and not d.get("prod_deal_id") \
                        and closed and (today - closed).days <= 60:
                    leaks["no_prod"].append(d["id"])

        # 段階別
        stages = []
        total_open_amount = sum(d.get("amount") or 0 for d in open_)
        for s in p["stages"]:
            sd = [d for d in ds if d["stage"] == s]
            amt = sum(d.get("amount") or 0 for d in sd)
            stages.append({
                "stage": s, "count": len(sd), "amount": amt,
                "weighted": sum(d.get("weighted") or 0 for d in sd),
                "prob": p["prob"].get(s, 0),
                "share": round(amt * 100 / total_open_amount, 1) if total_open_amount and s not in sales.CLOSED_STAGES else None,
                "avg_days": round(sum(d.get("days_in_stage", 0) for d in sd) / len(sd)) if sd and s not in sales.CLOSED_STAGES else None,
                "ids": [d["id"] for d in sd],
            })
        # 直近90日の受注率
        since = today - timedelta(days=90)
        recent = [d for d in ds if not d["is_open"] and (_d(d.get("closed_at")) or today) >= since]
        won = [d for d in recent if d["stage"] == p["won"]]
        lost = [d for d in recent if d["stage"] == p["lost"]]
        win_rate = round(len(won) / (len(won) + len(lost)), 3) if (won or lost) else None
        days_to_win = [(_d(d["closed_at"]) - _d(d["created_at"])).days for d in won if _d(d.get("closed_at")) and _d(d.get("created_at"))]
        # 月別（6か月）: 新規・受注・失注。前月比も
        months = []
        for i in range(5, -1, -1):
            y, m = today.year, today.month - i
            while m <= 0:
                m += 12
                y -= 1
            ym = f"{y:04d}-{m:02d}"
            c_new = [d for d in ds if (d.get("created_at") or "")[:7] == ym]
            c_won = [d for d in ds if d["stage"] == p["won"] and (d.get("closed_at") or "")[:7] == ym]
            c_lost = [d for d in ds if d["stage"] == p["lost"] and (d.get("closed_at") or "")[:7] == ym]
            months.append({"month": ym, "new": len(c_new), "won": len(c_won),
                           "won_amount": sum(d.get("amount") or 0 for d in c_won), "lost": len(c_lost)})
        for i, mth in enumerate(months):
            prev = months[i - 1]["won_amount"] if i else None
            mth["won_mom"] = round(mth["won_amount"] * 100 / prev) if prev else None
        # 失注の分析（どの段階から・何の理由で）
        all_lost = [d for d in ds if d["stage"] == p["lost"]]
        from_stage, reasons, types = {}, {}, {}
        for d in all_lost:
            from_stage[d.get("lost_from_stage") or "不明"] = from_stage.get(d.get("lost_from_stage") or "不明", 0) + 1
            if d.get("lost_reason"):
                reasons[d["lost_reason"]] = reasons.get(d["lost_reason"], 0) + 1
            types[d.get("lost_type") or "未記録"] = types.get(d.get("lost_type") or "未記録", 0) + 1
        # 重要案件（見込み額→金額の大きい順）
        focus = sorted(open_, key=lambda d: (-(d.get("weighted") or 0), -(d.get("amount") or 0), d["id"]))[:10]
        # 活動（直近30日）
        a_since = (today - timedelta(days=30)).isoformat()
        mix = {"meeting": 0, "call": 0, "email": 0, "memo": 0}
        for d in ds:
            for a in _user_acts(acts_by_deal.get(d["id"], [])):
                if (a.get("occurred_at") or "") >= a_since and a["kind"] in mix:
                    mix[a["kind"]] += 1
        weighted = sum(d.get("weighted") or 0 for d in open_)
        target = settings.get("monthly_target")
        summary = {
            "open": len(open_), "open_amount": total_open_amount, "weighted": weighted,
            "won_90": len(won), "lost_90": len(lost), "win_rate_90": win_rate,
            "avg_days_to_win": round(sum(days_to_win) / len(days_to_win)) if days_to_win else None,
            "target": target, "coverage": round(total_open_amount / target, 2) if target else None,
            "activity_30": mix,
            # 次の一手（次のアクション）が入っている進行中の案件の割合（入力率より大事な数字）
            "next_ratio": round(sum(1 for d in open_ if d.get("next_action")) / len(open_), 3) if open_ else None,
        }
        out["pipelines"][key] = {
            "label": p["label"], "summary": summary, "stages": stages, "months": months,
            "lost": {"from_stage": from_stage, "reasons": reasons, "types": types},
            "leaks": [{"key": k, "label": label, "ids": leaks[k]} for k, label in LEAKS
                      if leaks[k] and (mode != "gentle" or k in GENTLE_LEAKS)],
            "focus": [{"id": d["id"], "title": d["title"], "company": (d.get("card") or {}).get("company") or d.get("company_snapshot"),
                       "stage": d["stage"], "amount": d.get("amount"), "weighted": d.get("weighted"),
                       "advice": (d.get("advice") or [{}])[0].get("title")} for d in focus],
            "coach": coach_points(key, p, ds, open_, stages, summary, leaks, from_stage, reasons, types, deals, mode, panel, today),
        }
    return out


def quote_speed(c, tenant_id: str, deals: list, today: Optional[date] = None) -> dict:
    """見積の速さ：ヒアリング（見積中）に入ってから見積提出までの日数（直近180日に見積を出した案件）"""
    today = today or date.today()
    sales_ids = {d["id"]: d for d in deals if (d.get("pipeline") or "sales") == "sales"}
    first = {}   # deal_id → {"h": 日付, "q": 日付}
    for a in c.execute("""SELECT deal_id, occurred_at, title FROM activities WHERE tenant_id=? AND kind='stage'
            AND (title LIKE '%→ ヒアリング%' OR title LIKE '%→ 見積提出%') ORDER BY occurred_at, id""", (tenant_id,)):
        if a["deal_id"] not in sales_ids:
            continue
        x = first.setdefault(a["deal_id"], {})
        when = _d(a["occurred_at"])
        if "→ ヒアリング" in (a["title"] or "") and "h" not in x:
            x["h"] = when
        elif "→ 見積提出" in (a["title"] or "") and "q" not in x and x.get("h"):
            x["q"] = when
    since = today - timedelta(days=180)
    days = sorted((x["q"] - x["h"]).days for x in first.values() if x.get("h") and x.get("q") and x["q"] >= since and x["q"] >= x["h"])
    return {"count": len(days), "avg": round(sum(days) / len(days), 1) if days else None,
            "median": days[len(days) // 2] if days else None, "wait_days": QUOTE_WAIT_DAYS}


def owner_breakdown(deals: list, settings: dict) -> list:
    """担当者ごとの数字（部長の目の担当者別）"""
    rows = {}
    since = (date.today() - timedelta(days=90)).isoformat()
    for d in deals:
        o = d.get("owner") or ""
        r_ = rows.setdefault(o, {"owner": o, "open": 0, "amount": 0, "weighted": 0, "needs_action": 0,
                                 "won_90": 0, "lost_90": 0, "flags": 0})
        p = sales.pipeline_of(d)
        if d.get("is_open"):
            r_["open"] += 1
            r_["amount"] += d.get("amount") or 0
            r_["weighted"] += d.get("weighted") or 0
            r_["needs_action"] += 1 if d.get("needs_action") else 0
            r_["flags"] += 1 if any(a["level"] in ("high", "mid") for a in d.get("advice") or []) else 0
        elif (d.get("closed_at") or "")[:10] >= since:
            if d["stage"] == p["won"]:
                r_["won_90"] += 1
            elif d["stage"] == p["lost"]:
                r_["lost_90"] += 1
    return sorted(rows.values(), key=lambda x: (x["owner"] == "", -x["open"], x["owner"]))


def mix_breakdown(deals: list, field: str, today: date) -> list:
    """分野別・商流別の数字（営業案件）"""
    rows = {}
    since = (today - timedelta(days=365)).isoformat()
    for d in deals:
        if (d.get("pipeline") or "sales") != "sales":
            continue
        k = d.get(field) or "未設定"
        r_ = rows.setdefault(k, {"name": k, "open": 0, "open_amount": 0, "won_365": 0, "won_amount_365": 0, "lost_365": 0})
        if d.get("is_open"):
            r_["open"] += 1
            r_["open_amount"] += d.get("amount") or 0
        elif (d.get("closed_at") or "")[:10] >= since:
            if d["stage"] == "受注":
                r_["won_365"] += 1
                r_["won_amount_365"] += d.get("amount") or 0
            elif d["stage"] == "失注":
                r_["lost_365"] += 1
    order = (profiles.SEGMENTS + profiles.SHEET_SEGMENTS if field == "segment"
             else profiles.CHANNELS + profiles.SHEET_CHANNELS) + ["未設定"]
    return sorted(rows.values(), key=lambda x: order.index(x["name"]) if x["name"] in order else 99)


def _panel_points(ds: list, open_: list, today: date) -> list:
    """和宏電機（盤）の営業チームへのひとこと"""
    pts = []
    if len(open_) >= 5 and not any(d.get("segment") in profiles.GROWTH_SEGMENTS for d in open_):
        pts.append(_adv("info", "成長分野（データセンター・再エネ）の引合がありません",
                        "関西でもデータセンターの建設計画が続いています。重電メーカー・商社の担当者に、DCや太陽光の計画がないか聞いてみましょう。",
                        "成長分野に種をまく"))
    since = (today - timedelta(days=365)).isoformat()
    won = [d for d in ds if d["stage"] == "受注" and (d.get("closed_at") or "")[:10] >= since and d.get("amount")]
    total = sum(d["amount"] for d in won)
    if len(won) >= 3 and total:
        by_co = {}
        for d in won:
            co = (d.get("card") or {}).get("company") or d.get("company_snapshot") or "不明"
            by_co[co] = by_co.get(co, 0) + d["amount"]
        co, amt = max(by_co.items(), key=lambda x: x[1])
        share = round(amt * 100 / total)
        if share >= 50 and co != "不明":
            pts.append(_adv("info", f"この1年の受注金額の{share}%が{co}です",
                            "主要なお客様を大切にしつつ、ほかの重電メーカー・商社・電気工事会社からの引合も増やしておくと安心です。",
                            "主要取引先に偏りすぎない"))
    late = [d for d in open_ if d["stage"] in ("ヒアリング", "見積提出") and not d.get("channel")]
    if len(late) >= 3:
        pts.append(_adv("low", f"商流が未記録の案件が{len(late)}件あります",
                        "誰から誰へ発注が流れるか（メーカー経由・商社経由など）がわかると、誰に会えば決まるかが見えます。",
                        "商流とエンドユーザーを押さえる"))
    groups = {}
    for d in open_:
        k = sales.norm_site(d.get("site_name"))
        if k:
            groups.setdefault(k, []).append(d["id"])
    dup = [v for v in groups.values() if len(v) >= 2]
    if dup:
        pts.append(_adv("info", f"同じ現場の引合が{len(dup)}組あります",
                        "例: " + "・".join(f"#{x}" for x in dup[0][:3]) + "。どの商流が本命かを確かめ、見積をそろえましょう。",
                        "同じ現場の引合は商流を確かめる"))
    return pts


def coach_points(key, p, ds, open_, stages, summary, leaks, from_stage, reasons, types, all_deals, mode="gentle",
                 panel=False, today: Optional[date] = None) -> list:
    """部長からチームへのひとこと（ルール）"""
    pts = []
    if key == "sales":
        early = sum(st["count"] for st in stages if st["stage"] in ("テレアポ", "名刺交換", "御礼済", "アポ"))
        late = sum(st["count"] for st in stages if st["stage"] in ("ヒアリング", "見積提出"))
        if len(open_) == 0:
            pts.append(_adv("high", "進行中の案件がありません", "まずは案件の発掘から。展示会の名刺・過去の失注先・以前のお客様に連絡しましょう。", "発掘量の確保"))
        elif early < max(3, late):
            pts.append(_adv("mid", "案件の入口が細っています",
                            f"初期段階（名刺交換〜アポ）が{early}件しかありません。数か月後の受注が減ります。新規の発掘に時間を割きましょう。",
                            "初期段階は金額より件数を見る"))
        elif late == 0:
            pts.append(_adv("mid", "初期段階の案件ばかりです",
                            "ヒアリング・見積まで進んだ案件がありません。キーマンに会い、図面と納期を聞いて前に進めましょう。",
                            "案件を前進させる"))
        wr = summary["win_rate_90"]
        n = summary["won_90"] + summary["lost_90"]
        if wr is not None and n >= 5 and mode != "gentle":
            if wr < 0.2:
                pts.append(_adv("mid", f"受注率 {round(wr * 100)}% は低めです",
                                "見込みの薄い案件を追いすぎている可能性があります。追う案件の基準（BANT）を厳しくしましょう。",
                                "受注率30%が目安"))
            elif wr > 0.8:
                pts.append(_adv("mid", f"受注率 {round(wr * 100)}% は高すぎます",
                                "勝てる案件だけを追っていないでしょうか。既存の受注処理は任せて新規開拓に出ましょう。",
                                "高すぎる受注率は新規開拓不足"))
        if summary["coverage"] is not None:
            if summary["coverage"] < 3:
                pts.append(_adv("mid", f"パイプラインは目標の{summary['coverage']}倍です",
                                "目標の3倍が目安です。案件の発掘を増やすか、止まっている案件を動かしましょう。",
                                "パイプラインは目標の3倍"))
            if summary["target"] and summary["weighted"] < summary["target"]:
                pts.append(_adv("info", "見込み額（金額×確度）が目標に届いていません",
                                "金額は足りていても初期段階が多いと着地は届きません。確度の高い段階へ進めることが先です。",
                                "見込み額で着地を読む"))
        if from_stage:
            top = max(from_stage.items(), key=lambda x: x[1])
            if top[1] >= 2 and top[0] != "不明":
                pts.append(_adv("info", f"失注は「{top[0]}」からが最多（{top[1]}件）",
                                "その段階の確認項目（とくにキーマン・競合・金額の納得）が弱い可能性があります。",
                                "どの段階から負けたかで鍛える所がわかる"))
        if types.get("no_decision"):
            pts.append(_adv("info", f"「何も買わなかった」失注が{types['no_decision']}件あります",
                            "競合に決まったわけではありません。再アプローチ日を決めて定期的に連絡しましょう。", "失注商談は宝の山"))
        mix = summary["activity_30"]
        if (mix["call"] + mix["email"]) >= 6 and mix["meeting"] == 0:
            pts.append(_adv("info", "この30日は訪問・打合せの記録がありません",
                            "重要な案件は顔を合わせて課題を深掘りしましょう。訪問したら記録までが営業活動です。", "顧客を軸に活動を見る"))
        if len(leaks["no_card"]) >= 3:
            pts.append(_adv("low", f"名刺がない案件が{len(leaks['no_card'])}件あります",
                            "名刺を登録すると自動でつながります。人物情報は会社の資産です。", "名刺は会社の資産"))
        if not panel and len(open_) >= 5 and any(d.get("channel") for d in ds):
            new = [d for d in open_ if d.get("channel") in profiles.NEW_CHANNELS]
            if len(new) < 2:
                pts.append(_adv("info", f"新しいお客様からの引合が{len(new)}件です",
                                "既存のお客様の案件が中心です。受注を増やすには新規の入口（展示会・紹介・Web・訪問）を毎月少しずつ増やしましょう。",
                                "発掘量の確保"))
        if panel:
            pts += _panel_points(ds, open_, today or date.today())
            if leaks.get("no_prod"):
                pts.append(_adv("mid", f"受注後の進行がない受注案件が{len(leaks['no_prod'])}件あります",
                                "受注案件の「受注後の進行を作る」から始めると、承認図の返却や納期の危険を追いかけられます。",
                                "承認図の返却が納期を決める"))
    elif key == "prod":
        if leaks.get("delivery_risk"):
            pts.append(_adv("high", f"納期が危ない案件が{len(leaks['delivery_risk'])}件あります",
                            "承認図の返却・機器の到着のどこで止まっているかを確かめ、間に合わないならすぐお客様に相談しましょう。",
                            "承認図の返却が納期を決める"))
        if leaks.get("drawing_wait"):
            pts.append(_adv("mid", f"承認図の返却待ちが{len(leaks['drawing_wait'])}件あります",
                            "返却の見込み日を電話で確かめましょう。返却が遅れた分だけ製作と納期が詰まります。",
                            "承認図の返却が納期を決める"))
        no_dl = [d for d in open_ if not d.get("delivery_on")]
        if no_dl:
            pts.append(_adv("low", f"納期が未入力の案件が{len(no_dl)}件あります",
                            "納期（受電日）を入れると、遅れの危険を部長が逆算して知らせます。", None))
    elif key == "bp":
        done = [d for d in ds if d["stage"] in ("取材", "初稿提出", "アップロード完了")]
        sales_companies = {sales._norm_company((d.get("card") or {}).get("company") or d.get("company_snapshot"))
                           for d in all_deals if (d.get("pipeline") or "sales") == "sales"}
        not_linked = [d for d in done if sales._norm_company((d.get("card") or {}).get("company") or d.get("company_snapshot")) not in sales_companies]
        if not_linked:
            pts.append(_adv("info", f"取材済みで営業案件がない会社が{len(not_linked)}社あります",
                            "取材でできた関係を営業につなげましょう。外注の状況や困りごとを聞けていれば案件にできます。",
                            "取材から営業へつなぐ"))
    if key == "sales" and leaks.get("repeat_due"):
        pts.append(_adv("mid", f"リピートの声かけ時期の会社が{len(leaks['repeat_due'])}社あります",
                        "前回の受注から次の発注が近づいています。数量と納期の予定を聞いておきましょう。", "リピートは先回りで声をかける"))
    if sum(len(v) for v in leaks.values()) == 0 and open_:
        pts.append(_adv("info", "抜け漏れはありません", "この状態を保ちましょう。", None))
    pts = sorted(pts, key=lambda a: LEVEL_ORDER[a["level"]])
    if mode == "gentle":
        pts = [dict(a, level="low" if a["level"] == "high" else a["level"]) for a in pts][:3]
    return pts


# ── AI ──
def _ai_available() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def call_ai(prompt: str, max_tokens: int = 2500) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    msg = client.messages.create(model=os.getenv("SALES_AI_MODEL", "claude-sonnet-5-5"), max_tokens=max_tokens,
                                 messages=[{"role": "user", "content": prompt}])
    return "".join(getattr(b, "text", "") for b in msg.content)


def _json(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def _deal_brief(d: dict, acts: list, company_deals: list, own: str) -> str:
    p = sales.pipeline_of(d)
    checks = {ck["key"]: ck["label"] for ck in p["checks"]}
    done = [f"✓ {checks[k]}" + (f"（{v.get('note')}）" if v.get("note") else "") for k, v in d.get("checks", {}).items()
            if k in checks and v.get("done")]
    miss = [f"✗ {checks[k]}" for k in d.get("checks_missing") or [] if k in checks]
    acts_txt = "\n".join(f"- {a['occurred_at'][:10]} [{sales.ACTIVITY_KINDS.get(a['kind'], a['kind'])}] {a.get('title') or ''}"
                         f"{(' : ' + (a.get('body') or '')[:300].replace(chr(10), ' ')) if a.get('body') else ''}"
                         for a in acts[:20])
    items = d.get("items") or []
    items_txt = sales.items_text(items, sales.profile_key(own)) if items else ""
    hist = "\n".join(f"- #{x['id']} {x['title']}（{x['stage']}{'・' + x['lost_reason'] if x.get('lost_reason') else ''}）"
                     for x in company_deals[:10])
    card = d.get("card") or {}
    return f"""# 自社
{own}
# 案件 #{d['id']}（{p['label']}）
案件名: {d['title']}
会社: {card.get('company') or d.get('company_snapshot') or '不明'} / 相手: {card.get('name') or d.get('contact_snapshot') or '不明'} {card.get('department_position') or ''}
段階: {d['stage']}（この段階に{d.get('days_in_stage', 0)}日）
金額: {d.get('amount') or '未入力'} / 受注予定日: {d.get('expected_close') or '未定'}（変更{d.get('close_changes') or 0}回）
次のアクション: {d.get('next_action') or 'なし'}（期限 {d.get('next_action_due') or 'なし'}）
失注理由: {d.get('lost_reason') or '-'} / 失注の種類: {sales.LOST_TYPES.get(d.get('lost_type') or '', '-')} / 振り返り: {d.get('review') or '-'}
{_panel_brief(d)}# 見積明細
{items_txt or '（なし）'}
# 確認項目
{chr(10).join(done + miss) or '（なし）'}
# 活動記録（新しい順）
{acts_txt or '（なし）'}
# 同じ会社の過去・他の案件
{hist or '（なし）'}
"""


def _panel_brief(d: dict) -> str:
    vals = [("分野", d.get("segment")), ("商流", d.get("channel")), ("現場名", d.get("site_name")),
            ("エンドユーザー", d.get("end_user")), ("納期（受電日）", d.get("delivery_on"))]
    if not any(v for _, v in vals) and not d.get("same_site"):
        return ""
    line = " / ".join(f"{k}: {v or '未記録'}" for k, v in vals)
    if d.get("same_site"):
        line += f"\n同じ現場の進行中の引合: " + "、".join(f"#{x}" for x in d["same_site"])
    return line + "\n"


def ai_deal(d: dict, acts: list, company_deals: list, own: str) -> dict:
    prompt = f"""あなたは{own}の営業部長です。部下が担当する次の案件について、具体的に助言してください。
{playbook(own)}
{_deal_brief(d, acts, company_deals, own)}
# 書き方
- 一般論ではなく、この案件の記録に基づいて書く。記録にないことは推測と明記するか書かない
- 日本語。短く言い切る。読点は少なめに
- お客様に聞くべきことは、そのまま口に出せる質問文で書く

JSONだけを返してください:
{{"headline": "この案件の見立て（1文）",
  "risks": ["お客様が買わない理由になりそうなこと（最大3つ）"],
  "next_actions": ["今週やること（期限・相手が分かるように。最大3つ）"],
  "questions": ["次に会ったときお客様に聞く質問（最大4つ）"],
  "stage_view": "お客様の状態から見た本当の段階と、その理由（1〜2文）"}}"""
    data = _json(call_ai(prompt))
    if not data or not data.get("headline"):
        raise ValueError("AIの応答を読み取れませんでした。もう一度お試しください")
    clean = lambda xs, n: [str(x)[:300] for x in (xs or []) if x][:n]
    return {"headline": str(data["headline"])[:300], "risks": clean(data.get("risks"), 3),
            "next_actions": clean(data.get("next_actions"), 3), "questions": clean(data.get("questions"), 4),
            "stage_view": str(data.get("stage_view") or "")[:400]}


def ai_team(review: dict, deals_by_id: dict, own: str) -> dict:
    lines = []
    for key, pv in review["pipelines"].items():
        s = pv["summary"]
        lines.append(f"## {pv['label']}")
        lines.append(f"進行中 {s['open']}件 / 金額 {s['open_amount']} / 見込み額 {s['weighted']} / 直近90日 受注{s['won_90']}・失注{s['lost_90']}"
                     f" / 目標 {s['target'] or '未設定'} / 30日の活動 {s['activity_30']}")
        lines.append("段階別: " + "、".join(f"{st['stage']} {st['count']}件" for st in pv["stages"]))
        for lk in pv["leaks"]:
            names = [f"#{i} {deals_by_id[i]['title']}" for i in lk["ids"][:5] if i in deals_by_id]
            lines.append(f"抜け漏れ「{lk['label']}」{len(lk['ids'])}件: " + "、".join(names))
        lines.append("重要案件: " + "、".join(f"#{f['id']} {f['company'] or ''} {f['title']}（{f['stage']}）" for f in pv["focus"][:8]))
        if pv["lost"]["reasons"]:
            lines.append(f"失注理由: {pv['lost']['reasons']} / どの段階から: {pv['lost']['from_stage']} / 種類: {pv['lost']['types']}")
    for name, rows in (("分野別", review.get("segments")), ("商流別", review.get("channels"))):
        if rows:
            lines.append(f"## {name}（進行中件数・金額 / 1年の受注件数・金額）")
            lines.append("、".join(f"{x['name']} {x['open']}件 {x['open_amount']}円 / 受注{x['won_365']}件 {x['won_amount_365']}円" for x in rows))
    prompt = f"""あなたは{own}の営業部長です。営業チーム全体の状況を見て、今週の方針を部下に伝えてください。
{playbook(own)}
# 現状（{review['today']}）
{chr(10).join(lines)}
# 書き方
- 長い総評はいらない。「今週動くべき案件」を3件だけ選ぶ（受注に近い・止まっている・期限が来ているもの）
- 各案件に、今週やること（1つ・具体的に）と、その理由（短く）
- 日本語。短く言い切る。読点は少なめに
JSONだけを返してください:
{{"headline": "今週いちばん大事なこと（1文）",
  "top3": [{{"id": 12, "action": "今週やること", "why": "理由"}}],
  "praise": "うまくいっている点（あれば1文。なければ空）"}}"""
    data = _json(call_ai(prompt))
    if not data or not data.get("headline"):
        raise ValueError("AIの応答を読み取れませんでした。もう一度お試しください")
    top = []
    for x in (data.get("top3") or []):
        if not isinstance(x, dict):
            continue
        try:
            i = int(str(x.get("id")).lstrip("#"))
        except (TypeError, ValueError):
            continue
        if i in deals_by_id and i not in [y["id"] for y in top]:   # 実在する案件だけ（AIの作り話を出さない）
            top.append({"id": i, "action": str(x.get("action") or "")[:200], "why": str(x.get("why") or "")[:200]})
    return {"headline": str(data["headline"])[:300], "top3": top[:3],
            "points": [str(x)[:300] for x in (data.get("points") or []) if x][:5],
            "praise": str(data.get("praise") or "")[:300]}


# ── API ──
def build_router(check_session: Callable, fetch_cards: Callable, get_tenant: Optional[Callable] = None,
                 load_all: Optional[Callable] = None, load_deal: Optional[Callable] = None) -> APIRouter:
    """load_all(tenant) -> (deals, acts_by_deal, settings) / load_deal(tenant, id) -> deal(dict with activities, company_deals)"""
    r = APIRouter(prefix="/api/sales/advisor")

    def own(t):
        return ((get_tenant(t) if get_tenant else None) or {}).get("display_name") or t

    @r.get("/team")
    def team(owner: str = "", session: dict = Depends(check_session)):
        t = session["tenant_id"]
        deals, acts_by_deal, settings = load_all(t)
        breakdown = owner_breakdown(deals, settings)
        if owner:
            deals = [d for d in deals if (d.get("owner") or "") == ("" if owner == "-" else owner)]
        review = team_review(deals, acts_by_deal, settings)
        review["owners"] = breakdown
        review["owner"] = owner
        allowed = {p["key"] for p in sales.pipelines_for(own(t))}
        review["pipelines"] = {k: v for k, v in review["pipelines"].items() if k in allowed}
        with sales._conn() as c:
            row = c.execute("""SELECT created_at, body FROM deal_advice WHERE tenant_id=? AND deal_id IS NULL
                ORDER BY id DESC LIMIT 1""", (t,)).fetchone()
            review["quote"] = quote_speed(c, t, deals)
        review["ai"] = {"created_at": row["created_at"], **json.loads(row["body"])} if row else None
        review["ai_available"] = _ai_available()
        return review

    @r.post("/team/ai")
    def team_ai(session: dict = Depends(check_session)):
        t = session["tenant_id"]
        if not _ai_available():
            raise HTTPException(400, "AIのキーが未設定です")
        deals, acts_by_deal, settings = load_all(t)
        review = team_review(deals, acts_by_deal, settings)
        allowed = {p["key"] for p in sales.pipelines_for(own(t))}
        review["pipelines"] = {k: v for k, v in review["pipelines"].items() if k in allowed}
        try:
            body = ai_team(review, {d["id"]: d for d in deals}, own(t))
        except Exception as e:
            raise HTTPException(502, f"AIの総評に失敗しました: {e}")
        with sales._conn() as c:
            c.execute("INSERT INTO deal_advice (tenant_id, deal_id, created_at, body) VALUES (?,?,?,?)",
                      (t, None, sales._now(), json.dumps(body, ensure_ascii=False)))
            c.commit()
        return {"created_at": sales._now(), **body}

    @r.post("/deals/{deal_id}")
    def deal_ai(deal_id: int, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        if not _ai_available():
            raise HTTPException(400, "AIのキーが未設定です")
        d = load_deal(t, deal_id)
        try:
            body = ai_deal(d, d.get("activities") or [], d.get("company_deals") or [], own(t))
        except Exception as e:
            raise HTTPException(502, f"AIへの相談に失敗しました: {e}")
        with sales._conn() as c:
            c.execute("INSERT INTO deal_advice (tenant_id, deal_id, created_at, body) VALUES (?,?,?,?)",
                      (t, deal_id, sales._now(), json.dumps(body, ensure_ascii=False)))
            c.commit()
        return {"created_at": sales._now(), **body}

    return r


def slack_lines(review: dict, app_url: str) -> list:
    """朝のSlack通知に添える部長のひとこと（ゆるめは月曜だけ）"""
    out = []
    if review.get("strictness", "gentle") == "gentle" and date.fromisoformat(review["today"]).weekday() != 0:
        return out
    for key, pv in review["pipelines"].items():
        pts = [p for p in pv["coach"] if p["level"] in ("high", "mid")][:2]
        leaks = [f"{lk['label']} {len(lk['ids'])}件" for lk in pv["leaks"][:3]]
        if not pts and not leaks:
            continue
        out.append(f"*営業部長より（{pv['label']}）*")
        out += [f"• {p['title']}：{p['detail']}" for p in pts]
        if leaks:
            out.append("• 抜け漏れ: " + " / ".join(leaks) + f"（<{app_url}/sales.html?view=boss|部長の目>）")
    return out
