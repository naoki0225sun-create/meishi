"""
AI議事録・メモからの追記
  面談の文字起こし（Plaud Note などの書き出し: txt / md / docx / pdf / srt / vtt …）や走り書きのメモを読み、
  ・記録（議事録）
  ・案件の更新案（次のアクション・金額・段階・確認項目・現場・納期・見積明細 …）
  を作る。画面で直してから反映する（反映は既存の API を使う）。
読み取りは時間がかかるので裏で動かし、画面は完了を待つ。
"""
import json
import logging
from datetime import date
from typing import Callable, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel

import intake
import profiles
import sales

log = logging.getLogger("digest")
MAX_SOURCE_CHARS = 300_000
FIELD_KEYS = ("title", "next_action", "next_action_due", "amount", "expected_close", "stage", "lost_reason",
              "lost_detail", "lost_type", "reapproach_on",
              "segment", "channel", "site_name", "end_user", "delivery_on", "appo_at")
MAX_FILES = 6
MAX_TOTAL_BYTES = 22 * 1024 * 1024   # PDF・画像はAIに送るとき1.3倍ほどになる（送れる上限に収める）
# 段階の意味（AIが「今の状況」を割り振るときの目安）
STAGE_MEANING = {
    "テレアポ": "電話で接触しているがまだ会えていない",
    "名刺交換": "名刺を交換しただけ。具体的な話はまだ",
    "御礼済": "御礼を送った。次に会う約束はまだ",
    "アポ": "会う約束がある、または会ったが具体的な引合（図面・仕様）はまだ",
    "ヒアリング": "図面・仕様・数量をもらって中身を詰めている／見積を作っている（「見積中」はここ）",
    "見積提出": "見積を出して、お客様の回答を待っている",
    "受注": "注文が決まった（発注書・内示をもらった）",
    "失注": "断られた・他社に決まった・今回は見送り（価格が合わない等）",
    "保留": "お客様の都合で止まっている（予算・計画が未定で、時期を決めて確認する）",
}


def init_db():
    with sales._conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS deal_digests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            deal_id INTEGER NOT NULL,
            status TEXT NOT NULL,          -- analyzing | ready | error | applied
            filename TEXT,
            source_text TEXT,
            result TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            applied_at TEXT
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_digest_deal ON deal_digests(tenant_id, deal_id)")
        sales._add_column(c, "activities", "digest_id", "digest_id INTEGER")
        sales._add_column(c, "deal_digests", "answers", "answers TEXT")   # 「次に確かめること」への回答・直してほしいこと（やり直しのたびに足していく）
        c.commit()


PROMPT = """あなたは{own}の営業アシスタントです。下の案件について、資料（面談の文字起こし・議事録・走り書きのメモなど）から
「営業の記録（議事録）」と「案件の更新案」を作ってください。今日は {today} です。資料：{source}

{brief}
# 確認項目（key: 内容）
{checks}
# この案件で使える段階（今は「{cur_stage}」）
{stages}
{choices}
# ルール
- 資料（メモ）は短い走り書きのことが多い。書かれていることから、営業として自然に読み取れることは汲み取る
  （例: 「図面をもらった・見積してほしい」→ 段階はヒアリング（見積中）、次のアクションは見積の作成と提出）
  （例: 「価格が合わない。失注で」→ 段階は失注、lost_reason は 価格）
  （例: 「見積を出した」「一部提出済み」「見積を送った」→ 段階は見積提出。次のアクションは回答の確認）
  （例: 「〇日に訪問してもらう予定」→ 次のアクションはその訪問の準備、期限はその日）
  （例: 「11/12 14時に訪問のアポ」→ appo_at は 2026-11-12T14:00。時間が無ければ日付だけ）
- 金額・日付・数量・名前など事実は、書かれていないものを作らない
- 文字起こしは話し言葉・言い間違い・聞き取りミスを含む。意味を整えて要約する。数字・日付・固有名詞は慎重に
- 話者名が「話者1」などのときは、文脈から当社側かお客様側かを判断する（{own}の人は当社側）
- minutes は議事録。次の見出しで箇条書きにする（該当なしの見出しは省く）：
  ■要点 ■決まったこと ■お客様の宿題 ■当社の宿題 ■数字・条件（数量・金額・納期・仕様） ■気になる点（リスク・競合・懸念）
- situation は「今どういう状況か」を1文（40字程度）で。例: 「図面をもらい見積中。来月中に提出の約束」
- stage は今のお客様の状態に合う段階を必ず判断する。今の段階のままでよければ fields に入れない。
  前に進んだとき・失注・保留になったときに入れる（失注と保留はどの段階からでもよい。それ以外は後戻りさせない）
- 失注なら lost_reason を {lost} から1つ、lost_detail に理由を短く、lost_type を competitor（他社に負けた）か
  no_decision（お客様が何も買わなかった・見送り）で、reapproach_on に再アプローチの目安日（競合負けは約1年後・見送りは約3か月後）
- 失注・受注でなければ、next_action（当社が次にやること1つ・具体的に）と next_action_due（無理のない期限）を必ず提案する。
  期限は資料に約束の日があればそれに合わせ、なければ内容に応じて数日〜2週間で決める。next_reason にその理由を短く
- fields は変えるべき項目だけ
- appo_at はお客様と会う（訪問・来社・打合せ・Web会議）約束の日時。資料に日にちが書かれているときだけ「YYYY-MM-DDTHH:MM」（時間が無ければ YYYY-MM-DD）
- 日付は YYYY-MM-DD（年が無ければ今日に近い年）。amount は税抜の円の整数
- stage は資料からお客様の状態がはっきり進んだ（または失注した）ときだけ。失注なら lost_reason を {lost} から
- checks は資料で確認できた項目だけ。done は確認できたら true、note は分かった内容を短く
- items は見積・仕様の品目が具体的に出てきたときだけ（{item_rule}）
- unclear には聞き取れなかった・判断できなかった点や、次に確かめるべき点を書く
- 資料に【こちらからの回答・直してほしいこと】があれば、それが一番正しい。前回の整理より優先して全体を作り直す。
  回答で分かったことは minutes にも入れ、解決した点は unclear から外す

JSONだけを返してください（コードブロックや説明は不要）:
{{"kind": "meeting|call|email|memo", "title": "記録の件名（20字程度）", "occurred_at": "YYYY-MM-DD or null",
  "situation": "今の状況（1文）", "next_reason": "次のアクションの理由（短く）",
  "vendor_request": null, "group_request": null,
  "minutes": "議事録", "fields": {{"stage": "…", "next_action": "…", "next_action_due": "YYYY-MM-DD"}},
  "checks": [{{"key": "…", "done": true, "note": "…"}}],
  "items": [{{"name": "…", "material": "…", "thickness": "…", "qty": 1, "unit_price": 100000}}],
  "unclear": ["…"]}}
"""


def _choices(prof: str) -> str:
    if prof == "panel":
        return ("# 選択肢\n- segment（分野）: " + " / ".join(profiles.SEGMENTS) +
                "\n- channel（商流）: " + " / ".join(profiles.CHANNELS) +
                "\n- site_name（現場名）・end_user（エンドユーザー）・delivery_on（納期・受電日）も分かれば fields に入れる\n")
    return ("# 選択肢\n- segment（お客様の業種）: " + " / ".join(profiles.SHEET_SEGMENTS) +
            "\n- channel（引合の経路）: " + " / ".join(profiles.SHEET_CHANNELS) +
            "\n- delivery_on（納期）も分かれば fields に入れる\n")


VENDOR_RULE = """
# テレアポ業者への再テレアポ依頼（業者: {names}）
- 資料に「業者に再アポ（再テレアポ）を頼む」「テレアポ会社にもう一度かけてもらう」など、業者に電話を頼む指示があるときだけ vendor_request を出す（なければ null）
- purpose は {purposes} から1つ。note は業者への用件（業者の画面にそのまま出る。金額・社内の事情・悪口は書かない）。due_on は「いつまでに」があれば
- 業者に頼むときは、当社の next_action は「業者の再テレアポの結果を見る」にしてよい
"""


def build_prompt(d: dict, own: str, source: str, brief: str, vendors: Optional[list] = None, group: bool = False) -> str:
    p = sales.pipeline_of(d)
    prof = sales.profile_key(own)
    L = profiles.ITEM_LABELS[prof]
    import vendorreq
    if vendors and (d.get("pipeline") or "sales") == "sales":
        brief = (brief or "") + VENDOR_RULE.format(names="・".join(vendors), purposes=" / ".join(vendorreq.PURPOSES))
    if group:
        brief = (brief or "") + GROUP_RULE
    return PROMPT.format(
        own=own, today=date.today().isoformat(), source=source, brief=brief,
        checks="\n".join(f"- {ck['key']}: {ck['label']}" for ck in p["checks"]) or "（なし）",
        stages="\n".join(f"- {s}: {STAGE_MEANING[s]}" if s in STAGE_MEANING else f"- {s}" for s in p["stages"]),
        cur_stage=d.get("stage") or "", choices=_choices(prof), lost="・".join(sales.LOST_REASONS),
        item_rule=f"name={L['name']}, material={L['material']}, thickness={L['thickness']}, qty={L['qty']}, unit_price=単価",
    )


GROUP_RULE = """
# ワコーメタル（グループの板金会社）への筐体の依頼
- 資料に「ワコーメタルに筐体（板金・箱）を頼む／見積を依頼する」などの指示があるときだけ group_request を {"note": "依頼内容（仕様・面数・材質・図面の予定など）"} で出す（なければ null）
"""


def _appo(v):
    """アポ日時（YYYY-MM-DDTHH:MM か YYYY-MM-DD）"""
    if not isinstance(v, str):
        return None
    v = v.strip().replace(" ", "T").replace("/", "-")
    try:
        if len(v) == 10:
            return date.fromisoformat(v).isoformat()
        from datetime import datetime
        return datetime.fromisoformat(v[:16]).isoformat(timespec="minutes")
    except ValueError:
        return None


def _group_request(v):
    if not isinstance(v, dict):
        return None
    return {"note": _txt(v.get("note"), 1000) or ""}


def _txt(v, n):
    """文字だけ受け付ける（リスト・数値・dict などは捨てる）"""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        v = str(v)
    if not isinstance(v, str):
        return None
    v = v.strip()
    return v[:n] or None


def _int(v, hi=10_000_000_000):
    try:
        if isinstance(v, bool) or v is None or v == "":
            return None
        n = int(float(str(v).replace(",", "").replace("円", "").strip()))
    except (TypeError, ValueError, OverflowError):
        return None
    return n if 0 <= n <= hi else None


def _list(v):
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.strip():
        return [v]
    return []


def _vendor_request(v):
    """AIが出した「業者に再テレアポを頼む」の案（用件が正しいものだけ）"""
    import vendorreq
    if not isinstance(v, dict):
        return None
    purpose = _txt(v.get("purpose"), 40)
    if purpose not in vendorreq.PURPOSES:
        return None
    due = v.get("due_on")
    return {"purpose": purpose, "note": _txt(v.get("note"), 500) or "",
            "due_on": intake._date_or_none(due) if isinstance(due, str) else None}


def clean_result(data: dict, d: dict, own: str, group: bool = False) -> dict:
    """AIの出力を、この案件・この会社で使える値だけにする（型がおかしいものは捨てる）"""
    p = sales.pipeline_of(d)
    prof = sales.profile_key(own)
    pf = profiles.PROFILE_FIELDS[prof]
    kind = data.get("kind")
    out = {"kind": kind if isinstance(kind, str) and kind in intake.KINDS else "meeting",
           "title": _txt(data.get("title"), 120) or "面談の記録",
           "occurred_at": intake._date_or_none(data.get("occurred_at")) if isinstance(data.get("occurred_at"), str) else None,
           "minutes": _txt(data.get("minutes"), 20000) or "",
           "situation": _txt(data.get("situation"), 200) or "", "next_reason": _txt(data.get("next_reason"), 200) or "",
           "vendor_request": _vendor_request(data.get("vendor_request")),
           "group_request": _group_request(data.get("group_request")) if group else None,
           "fields": {}, "checks": [], "items": [],
           "unclear": [u for u in (_txt(x, 300) for x in _list(data.get("unclear"))) if u][:15]}
    f = data.get("fields") if isinstance(data.get("fields"), dict) else {}
    for k in FIELD_KEYS:
        if k in sales.EXTRA_FIELDS and k not in pf["fields"]:
            continue   # この会社では使わない項目
        v = f.get(k)
        if v in (None, ""):
            continue
        if k in ("next_action_due", "expected_close", "delivery_on", "reapproach_on"):
            v = intake._date_or_none(v) if isinstance(v, str) else None
        elif k == "appo_at":
            v = _appo(v)
            if v and d.get(k) and v[:len(d[k])] == d[k][:len(v)]:
                v = None   # 同じ日（時間の書き方が違うだけ）は変えない
        elif k == "amount":
            v = _int(v)
        elif k == "stage":
            v = v if isinstance(v, str) and v in p["stages"] and v != d.get("stage") else None
            lin = p.get("linear") or []
            if v and v in lin and d.get("stage") in lin and lin.index(v) < lin.index(d.get("stage")):
                v = None   # 後戻りはさせない（失注・保留はどこからでも）
        elif k == "lost_reason":
            v = v if isinstance(v, str) and v in sales.LOST_REASONS else None
        elif k == "lost_type":
            v = v if isinstance(v, str) and v in sales.LOST_TYPES else None
        elif k == "lost_detail":
            v = _txt(v, 300)
        elif k == "segment":
            v = v if isinstance(v, str) and v in pf["segments"] else None
        elif k == "channel":
            v = v if isinstance(v, str) and v in pf["channels"] else None
        else:
            v = _txt(v, 200 if k == "next_action" else 120)
        if k == "expected_close" and p["key"] != "sales":
            v = None
        if v is not None and v != d.get(k):
            out["fields"][k] = v
    if out["fields"].get("stage") == p["lost"] and p["reason_required"] and "lost_reason" not in out["fields"]:
        out["fields"]["lost_reason"] = d.get("lost_reason") or "その他"
    final = out["fields"].get("stage") or d.get("stage")
    if final in sales.CLOSED_STAGES:
        out["fields"].pop("appo_at", None)
        # 終わった案件には「次のアクション」を付けない（失注は再アプローチ日で次の一手を決める）
        out["fields"].pop("next_action", None); out["fields"].pop("next_action_due", None)
        out["next_reason"] = ""
    if final != p["lost"]:
        for k in ("lost_reason", "lost_detail", "lost_type", "reapproach_on"):
            out["fields"].pop(k, None)
    keys = {ck["key"]: ck["label"] for ck in p["checks"]}
    seen = set()
    for ck in _list(data.get("checks")):
        if not isinstance(ck, dict):
            continue
        key = ck.get("key")
        if not isinstance(key, str) or key not in keys or key in seen:
            continue
        seen.add(key)
        done = ck.get("done", True)
        done = done is True or (isinstance(done, str) and done.strip().lower() in ("true", "yes", "1", "済", "はい", "done"))
        if not done:
            continue   # 「確認できなかった」は出さない（反映すると確認済みの項目を未確認に戻してしまう）
        out["checks"].append({"key": key, "label": keys[key], "done": True,
                              "note": _txt(ck.get("note"), 300) or ""})
    for it in _list(data.get("items"))[:30]:
        if not isinstance(it, dict) or not _txt(it.get("name"), 120):
            continue
        out["items"].append({"name": _txt(it.get("name"), 120), "material": _txt(it.get("material"), 60),
                             "thickness": _txt(it.get("thickness"), 20),
                             "qty": _int(it.get("qty"), 1_000_000_000), "unit_price": _int(it.get("unit_price"), 1_000_000_000)})
    return out


def run(digest_id: int, d: dict, own: str, prepared: tuple, brief: str, vendors: Optional[list] = None, group: bool = False):
    source, content, body_text = prepared
    try:
        prompt = build_prompt(d, own, source, brief, vendors, group)
        blocks = list(content)
        if body_text:
            text = body_text if len(body_text) <= intake.MAX_TEXT_CHARS else body_text[:intake.MAX_TEXT_CHARS] + "\n（長いため以下省略）"
            blocks.append({"type": "text", "text": f"<資料>\n{text}\n</資料>"})
        blocks.append({"type": "text", "text": prompt})
        data = intake._parse_json(intake.call_ai(blocks, 12000))
        if not data or not (data.get("minutes") or data.get("fields")):
            raise ValueError("AIの応答を読み取れませんでした。もう一度お試しください")
        res = clean_result(data, d, own, group)
        status, err, body = "ready", None, json.dumps(res, ensure_ascii=False)
    except Exception as e:
        log.exception("議事録の読み取りに失敗")
        status, err, body = "error", str(e)[:500], None
    with sales._conn() as c:
        c.execute("UPDATE deal_digests SET status=?, result=?, error=? WHERE id=?", (status, body, err, digest_id))
        c.commit()


class RecordIn(BaseModel):
    kind: str = "meeting"
    title: Optional[str] = None
    body: Optional[str] = None
    occurred_at: Optional[str] = None


class ItemIn(BaseModel):
    name: str
    material: Optional[str] = None
    thickness: Optional[str] = None
    qty: Optional[int] = None
    unit_price: Optional[int] = None


class VendorAsk(BaseModel):
    vendor_id: int
    purpose: str
    note: Optional[str] = None
    due_on: Optional[str] = None
    company: Optional[str] = None
    contact: Optional[str] = None
    department: Optional[str] = None
    phone: Optional[str] = None


class AnswerIn(BaseModel):
    q: Optional[str] = None
    a: Optional[str] = None


class ReviseIn(BaseModel):
    answers: list[AnswerIn] = []
    note: Optional[str] = None


class GroupAsk(BaseModel):
    note: Optional[str] = None


class ApplyBody(BaseModel):
    fields: dict = {}
    checks: dict = {}
    record: Optional[RecordIn] = None
    items: list[ItemIn] = []
    vendor_request: Optional[VendorAsk] = None   # 業者に再テレアポを頼む（一緒に反映）
    make_prod: bool = False                      # 受注にしたら「受注後の進行」も作る
    group_request: Optional[GroupAsk] = None     # ワコーメタルに筐体を依頼する（和宏電機）


def _prepare_many(own: str, raws: list, text: Optional[str]) -> tuple:
    """メモと複数のファイル（図面・写真・資料）をまとめてAIに渡す形にする"""
    if not raws:
        return intake.prepare(own, None, None, text)
    content, bodies, names = [], [], []
    for i, (name, raw) in enumerate(raws):
        _, c_, b_ = intake.prepare(own, name, raw, None)
        content += c_
        if b_:
            bodies.append(f"【ファイル「{name}」】\n{b_}")
        names.append(f"「{name}」")
    memo = (text or "").strip()
    body = "\n\n".join(([f"【メモ（いちばん大事）】\n{memo}"] if memo else []) + bodies)
    return f"メモと添付ファイル{'・'.join(names)}", content, body


LABELS = {"title": "案件名", "next_action": "次のアクション", "next_action_due": "期限", "amount": "金額",
          "expected_close": "受注予定日", "lost_detail": "失注の詳細", "lost_type": "失注の種類", "reapproach_on": "再アプローチ日",
          "segment": "分野", "channel": "商流", "site_name": "現場名", "end_user": "エンドユーザー", "delivery_on": "納期",
          "appo_at": "アポ日時"}


def change_note(before: dict, fields: dict, checks: dict, n_items: int) -> str:
    """AIの提案で変わった項目を1行に（段階・失注理由は段階の記録に出るので省く）"""
    parts = []
    for k, label in LABELS.items():
        if k not in fields or fields[k] == before.get(k):
            continue
        v = fields[k]
        if k == "amount" and v is not None:
            v = f"{int(v):,}円"
        elif k == "lost_type":
            v = sales.LOST_TYPES.get(v, v)
        elif k == "appo_at" and v:
            v = v.replace("T", " ")
        parts.append(f"{label}「{v if v not in (None, '') else 'なし'}」")
    if checks:
        parts.append(f"確認項目{len(checks)}件")
    if n_items:
        parts.append(f"見積明細{n_items}行")
    return "・".join(parts)[:500]


def build_router(check_session: Callable, get_tenant: Callable, load_deal: Callable,
                 update_in_tx: Optional[Callable] = None, sales_router=None) -> APIRouter:
    """sales_router: 受注後の進行・グループ依頼を同じトランザクションで作るのに使う（無ければその2つは使えない）"""
    r = APIRouter(prefix="/api/sales")
    import advisor

    def own_name(t):
        return ((get_tenant(t) if get_tenant else None) or {}).get("display_name") or t

    def get_row(c, t, digest_id):
        row = c.execute("SELECT * FROM deal_digests WHERE id=? AND tenant_id=?", (digest_id, t)).fetchone()
        if not row:
            raise HTTPException(404, "読み取りが見つかりません")
        return row

    @r.post("/deals/{deal_id}/digest")
    async def create(deal_id: int, background: BackgroundTasks, file: Optional[UploadFile] = File(None),
                     files: Optional[list[UploadFile]] = File(None),
                     text: Optional[str] = Form(None), session: dict = Depends(check_session)):
        t = session["tenant_id"]
        ups = [f for f in ([file] if file is not None else []) + list(files or []) if f is not None and f.filename]
        if len(ups) > MAX_FILES:
            raise HTTPException(400, f"ファイルは{MAX_FILES}個までです")
        raws, total = [], 0
        for f in ups:
            raw = await f.read(intake.MAX_FILE_BYTES + 1)
            total += len(raw)
            if total > MAX_TOTAL_BYTES:
                raise HTTPException(400, "ファイルが大きすぎます（合計22MBまで）")
            raws.append((f.filename, raw))
        own = own_name(t)
        d = load_deal(t, deal_id)   # 404 ならここで止まる
        prepared = _prepare_many(own, raws, text)
        filename = "・".join(n for n, _ in raws) if raws else None
        with sales._conn() as c:
            cur = c.execute("""INSERT INTO deal_digests (tenant_id, deal_id, status, filename, source_text, created_at)
                VALUES (?,?,?,?,?,?)""", (t, deal_id, "analyzing", filename,
                                         (prepared[2] or "")[:MAX_SOURCE_CHARS], sales._now()))
            c.commit()
            digest_id = cur.lastrowid
        start(background, t, d, own, digest_id, prepared)
        return {"id": digest_id, "status": "analyzing"}

    def start(background, t, d, own, digest_id, prepared):
        brief = advisor._deal_brief(d, d.get("activities") or [], d.get("company_deals") or [], own)
        with sales._conn() as c:
            vendors = [v["name"] for v in c.execute("SELECT name FROM vendors WHERE tenant_id=? AND active=1 ORDER BY id", (t,))]
        group = bool(sales_router is not None and sales.profile_key(own) == "panel" and sales.group_target_of(t)
                     and not d.get("group_deal_id") and (d.get("pipeline") or "sales") in ("sales", "prod")
                     and d.get("stage") not in sales.CLOSED_STAGES)
        background.add_task(run, digest_id, d, own, prepared, brief, vendors, group)

    @r.post("/digests/{digest_id}/revise")
    def revise(digest_id: int, body: ReviseIn, background: BackgroundTasks, session: dict = Depends(check_session)):
        """「次に確かめること」への回答や直してほしいことを足して、AIにもう一度整理してもらう"""
        t = session["tenant_id"]
        lines = []
        for a in body.answers[:20]:
            q, ans = _txt(a.q, 300), _txt(a.a, 1000)
            if ans:
                lines.append(f"・{q}\n  → {ans}" if q else f"・{ans}")
        note = _txt(body.note, 2000)
        if note:
            lines.append(f"・直してほしいこと: {note}")
        if not lines:
            raise HTTPException(400, "回答か、直してほしいことを書いてください")
        own = own_name(t)
        with sales._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = dict(get_row(c, t, digest_id))
            if row["status"] != "ready":
                raise HTTPException(409, "この整理はもう反映したか、作り直し中です")
            history = ((row.get("answers") or "") + "\n" + "\n".join(lines)).strip()[:20000]
            c.execute("UPDATE deal_digests SET status='superseded' WHERE id=?", (digest_id,))
            cur = c.execute("""INSERT INTO deal_digests (tenant_id, deal_id, status, filename, source_text, answers, created_at)
                VALUES (?,?,?,?,?,?,?)""", (t, row["deal_id"], "analyzing", row["filename"], row["source_text"], history, sales._now()))
            c.commit()
            new_id = cur.lastrowid
        prev = json.loads(row["result"]) if row["result"] else {}
        d = load_deal(t, row["deal_id"])
        text = "\n\n".join(x for x in (
            f"【元のメモ・資料】\n{row['source_text']}" if row["source_text"] else "",
            f"【前回のAIの整理（議事録）】\n{prev.get('minutes') or ''}" if prev.get("minutes") else "",
            f"【こちらからの回答・直してほしいこと（いちばん正しい）】\n{history}") if x)
        start(background, t, d, own, new_id, ("元のメモと、前回の整理への回答", [], text))
        return {"id": new_id, "status": "analyzing"}

    @r.get("/digests/{digest_id}")
    def get(digest_id: int, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with sales._conn() as c:
            row = dict(get_row(c, t, digest_id))
        return {"id": row["id"], "deal_id": row["deal_id"], "status": row["status"], "filename": row["filename"],
                "error": row["error"], "has_source": bool(row["source_text"]), "answers": row.get("answers"),
                "result": json.loads(row["result"]) if row["result"] else None}

    @r.get("/digests/{digest_id}/source")
    def source(digest_id: int, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with sales._conn() as c:
            row = get_row(c, t, digest_id)
        body = row["source_text"] or "（PDF・画像から読み取ったため、元の文字はありません）"
        return Response(content=body, media_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition": "inline"})

    @r.post("/digests/{digest_id}/apply")
    def apply(digest_id: int, body: ApplyBody, session: dict = Depends(check_session)):
        """画面で直した内容を1回でまとめて反映する（途中で失敗したら何も変えない・二重に反映しない）"""
        t = session["tenant_id"]
        fields = {k: v for k, v in (body.fields or {}).items() if k in FIELD_KEYS}
        try:
            data = sales._dump(sales.DealUpdate(**fields, checks=body.checks or None)) if (fields or body.checks) else {}
        except Exception:
            raise HTTPException(400, "更新する項目の値が不正です")
        rec = body.record
        if rec is not None:
            if rec.kind not in intake.KINDS:
                raise HTTPException(400, "記録の種類が不正です")
            title = sales._clean_text(rec.title, 200, "件名") or "面談の記録"
            text = sales._clean_text(rec.body, 20000, "議事録")
            occurred = None
            if rec.occurred_at:
                try:
                    occurred = sales.datetime.fromisoformat(rec.occurred_at[:19]).isoformat(timespec="seconds")
                except ValueError:
                    raise HTTPException(400, "日時が不正です")
        if len(body.items) > 30:
            raise HTTPException(400, "明細が多すぎます")
        if (body.make_prod or body.group_request is not None) and sales_router is None:
            raise HTTPException(400, "この画面からは作れません")
        cards = sales_router.card_index(t) if body.make_prod else None
        prod_id = group_id = None
        with sales._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = get_row(c, t, digest_id)
            if row["status"] == "applied":
                raise HTTPException(409, "この読み取りはもう反映済みです")
            if row["status"] != "ready":
                raise HTTPException(409, "読み取りが終わっていません")
            deal_id = row["deal_id"]
            sales._get_deal_row(c, t, deal_id)
            if data:
                before = dict(sales._get_deal_row(c, t, deal_id))
                update_in_tx(c, t, deal_id, data)
                hist = change_note(before, fields, body.checks, len(body.items))
                if hist:
                    # AIの提案で変えた項目の履歴（段階の変更は別に残る）
                    sales.log_activity(c, t, deal_id, "stage", "AIの提案で更新: " + hist)
            act_id = None
            if rec is not None:
                act_id = sales.log_activity(c, t, deal_id, rec.kind, title, text, occurred)
                c.execute("UPDATE activities SET digest_id=? WHERE id=?", (digest_id, act_id))
            if body.items:
                n = c.execute("SELECT COUNT(*) n FROM deal_items WHERE deal_id=? AND tenant_id=?", (deal_id, t)).fetchone()["n"]
                if n + len(body.items) > 100:
                    raise HTTPException(400, f"明細は100行までです（いま{n}行）")
                for i, it in enumerate(body.items):
                    name = sales._clean_text(it.name, 120, "品名")
                    if not name:
                        continue
                    for v in (it.qty, it.unit_price):
                        if v is not None and not (0 <= v <= 1_000_000_000):
                            raise HTTPException(400, "数量・単価が不正です")
                    c.execute("""INSERT INTO deal_items (tenant_id, deal_id, name, material, thickness, qty, unit_price, note,
                        sort_order, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                              (t, deal_id, name, sales._clean_text(it.material, 60, "材質"), sales._clean_text(it.thickness, 20, "板厚"),
                               it.qty, it.unit_price, None, n + i, sales._now()))
            if body.vendor_request is not None:
                import vendorreq
                va = body.vendor_request
                rq = vendorreq.RequestIn(vendor_id=va.vendor_id, kind="recall", purpose=va.purpose, note=va.note, due_on=va.due_on)
                share = vendorreq._clean_share({"company": va.company, "contact": va.contact, "department": va.department, "phone": va.phone})
                vendorreq.create_request(c, t, sales._get_deal_row(c, t, deal_id), rq,
                                         sales._clean_text(va.note, 2000, "業者へのひとこと"), sales._clean_date(va.due_on), share)
            if body.make_prod:
                prod_id = sales_router.make_prod_in_tx(c, t, deal_id, cards)
            if body.group_request is not None:
                group_id = sales_router.group_request_in_tx(c, t, deal_id, body.group_request.note)
            c.execute("UPDATE deal_digests SET status='applied', applied_at=? WHERE id=?", (sales._now(), digest_id))
            c.commit()
        return {"ok": True, "activity_id": act_id, "prod_id": prod_id, "group_id": group_id}

    return r
