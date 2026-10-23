"""
Slack連携（営業チャンネル）
==========================
- 名刺の画像が投稿された → AIで読み取り → スプレッドシートに登録 → スレッドに結果を返信
- 文章が投稿された → AIで「どの案件の何の報告か」を判定
    確度が高い → 案件に記録（ステージ・次のアクションも）してスレッドに結果を返信
    特定できない → 候補を返信。スレッドに「#12」のように返信すると その案件に反映
- 反映後にスレッドへ「取消」と返信すると元に戻す
- 毎朝の要対応通知:  python slack_bot.py notify

環境変数（/var/meishi/backend/.env）
    SLACK_BOT_TOKEN=xoxb-...
    SLACK_SIGNING_SECRET=...
    SLACK_CHANNEL_TENANTS=C0123456789:wakometal     # チャンネルID:テナントID（カンマ区切りで複数可）
    ANTHROPIC_API_KEY=...                            # 読み取りに必須
    APP_URL=https://meishi.wako-apps.com             # 省略可
"""
import base64
import hashlib
import hmac
import io
import json
import logging
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime
from typing import Callable, Optional

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse

import sales

log = logging.getLogger("slack_bot")

STAGE_WORDS = ", ".join(sales.STAGES)
CARD_FIELDS = ["company", "name", "department_position", "email", "address", "mobile", "phone", "website", "name_kana"]
FIELD_LABELS = {"company": "会社名", "name": "氏名", "department_position": "部署・役職", "email": "メール",
                "address": "住所", "mobile": "携帯", "phone": "電話", "website": "サイト", "name_kana": "ふりがな"}
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
UNDO_WORDS = {"取消", "取り消し", "取消し", "とりけし", "undo"}
ADD_WORDS = {"追加", "ついか", "OK", "ok", "Ok", "おけ", "お願いします"}


def env(k: str, default: str = "") -> str:
    return os.getenv(k, default)


def app_url() -> str:
    return env("APP_URL", "https://meishi.wako-apps.com").rstrip("/")


def channel_tenants() -> dict:
    out = {}
    for part in env("SLACK_CHANNEL_TENANTS").split(","):
        if ":" in part:
            ch, t = part.split(":", 1)
            if ch.strip() and t.strip():
                out[ch.strip()] = t.strip()
    return out


# ── DB（sales.db に同居） ──
def init_db():
    with sales._conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS slack_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            channel TEXT NOT NULL,
            thread_ts TEXT NOT NULL,
            kind TEXT NOT NULL,            -- card | report | intake
            status TEXT NOT NULL,          -- applied | pending | undone | failed
            deal_id INTEGER,
            card_id TEXT,
            activity_ids TEXT,             -- JSON
            prev TEXT,                     -- JSON: 反映前の案件の値（取消用）
            parse TEXT,                    -- JSON: AIの判定結果
            message TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_slack_actions_thread ON slack_actions(channel, thread_ts);
        CREATE TABLE IF NOT EXISTS slack_events_seen (
            event_id TEXT PRIMARY KEY,
            seen_at TEXT NOT NULL
        );
        """)
        c.commit()


def _seen(event_id: str) -> bool:
    if not event_id:
        return False
    with sales._conn() as c:
        try:
            c.execute("INSERT INTO slack_events_seen (event_id, seen_at) VALUES (?,?)", (event_id, sales._now()))
            c.execute("DELETE FROM slack_events_seen WHERE seen_at < datetime('now','-7 days')")
            c.commit()
            return False
        except Exception:
            return True


# ── Slack API ──
def slack_api(method: str, payload: Optional[dict] = None, get: bool = False) -> dict:
    token = env("SLACK_BOT_TOKEN")
    if not token:
        log.warning("SLACK_BOT_TOKEN 未設定のため %s を送れません", method)
        return {"ok": False, "error": "no_token"}
    url = f"https://slack.com/api/{method}"
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if get:
        url += "?" + urllib.parse.urlencode(payload or {})
    else:
        data = json.dumps(payload or {}).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method="GET" if get else "POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            res = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        log.warning("Slack API %s 失敗: %s", method, e)
        return {"ok": False, "error": str(e)}
    if not res.get("ok"):
        log.warning("Slack API %s エラー: %s", method, res.get("error"))
    return res


def post(channel: str, thread_ts: Optional[str], text: str) -> dict:
    payload = {"channel": channel, "text": text, "unfurl_links": False, "unfurl_media": False}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    return slack_api("chat.postMessage", payload)


def user_name(user_id: str) -> str:
    if not user_id:
        return ""
    res = slack_api("users.info", {"user": user_id}, get=True)
    u = res.get("user") or {}
    p = u.get("profile") or {}
    return p.get("real_name") or p.get("display_name") or u.get("name") or ""


def download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {env('SLACK_BOT_TOKEN')}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        ctype = r.headers.get("Content-Type", "")
        data = r.read()
    if "text/html" in ctype:
        raise RuntimeError("画像を取得できません（ボットに files:read 権限がない可能性）")
    return data


def verify_signature(headers, body: bytes) -> bool:
    secret = env("SLACK_SIGNING_SECRET")
    ts = headers.get("x-slack-request-timestamp", "")
    sig = headers.get("x-slack-signature", "")
    if not secret or not ts or not sig:
        return False
    try:
        if abs(time.time() - int(ts)) > 300:
            return False
    except ValueError:
        return False
    base = f"v0:{ts}:".encode() + body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


# ── AI ──
def claude(content, max_tokens: int = 1500) -> str:
    if not env("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY が未設定です")
    import anthropic
    client = anthropic.Anthropic(api_key=env("ANTHROPIC_API_KEY"))
    msg = client.messages.create(
        model=env("SALES_AI_MODEL", "claude-sonnet-5-5"),
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": content}],
    )
    return "".join(getattr(b, "text", "") for b in msg.content)


def parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def shrink_image(raw: bytes, max_w: int = 1600):
    """AIに送る用と保存用に JPEG 化して縮小"""
    from PIL import Image, ImageOps
    img = Image.open(io.BytesIO(raw))
    img.load()
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    if img.mode != "RGB":
        img = img.convert("RGB")
    if img.width > max_w:
        img = img.resize((max_w, int(img.height * max_w / img.width)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85, optimize=True)
    return buf.getvalue()


def extract_card(jpeg: bytes) -> dict:
    prompt = (
        "この画像の名刺を読み取ってください。出力は JSON のみ。\n"
        '{"is_card": true/false, "company": "", "name": "", "department_position": "", "email": "",'
        ' "address": "", "mobile": "", "phone": "", "website": "", "name_kana": "", "uncertain": ["自信のない項目名"]}\n'
        "- 名刺でなければ is_card を false\n"
        "- 読めない・載っていない項目は空文字。推測で埋めない\n"
        "- 氏名は姓と名の間に半角スペース。ふりがなは名刺に書いてある場合だけ\n"
        "- phone は会社の代表・固定電話、mobile は携帯（070/080/090）\n"
        "- uncertain には読み取りに自信がない項目のキー名を入れる"
    )
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                     "data": base64.b64encode(jpeg).decode()}},
        {"type": "text", "text": prompt},
    ]
    data = parse_json(claude(content, 800))
    if not data:
        raise RuntimeError("AIの出力を読み取れませんでした")
    for k in CARD_FIELDS:
        data[k] = (data.get(k) or "").strip()
    data["uncertain"] = [u for u in (data.get("uncertain") or []) if u in CARD_FIELDS]
    return data


def parse_report(text: str, deals: list, today: date) -> dict:
    lines = []
    for d in deals:
        lines.append(f"#{d['id']} | {d['company'] or ''} | {d['contact'] or ''} | {d['title']} | {d['stage']}")
    prompt = (
        "営業チャンネルへの投稿を読み、どの案件についての報告かを判定してください。\n"
        f"今日は {today.isoformat()}（{'月火水木金土日'[today.weekday()]}曜日）です。\n\n"
        "# 進行中の案件（#ID | 会社 | 担当者 | 案件名 | ステージ）\n" + ("\n".join(lines) or "（なし）") + "\n\n"
        f"# 投稿\n{text}\n\n"
        "# 出力（JSONのみ）\n"
        '{"is_report": true/false, "deal_id": 数値またはnull, "confidence": "high|medium|low",'
        ' "candidates": [候補の案件ID], "kind": "meeting|call|email|memo", "title": "記録の件名（20字程度）",'
        ' "summary": "要点", "stage": "ステージ名またはnull", "lost_reason": "失注理由またはnull",'
        ' "next_action": "次のアクションまたはnull", "next_action_due": "YYYY-MM-DDまたはnull",'
        ' "unclear": ["判断できなかった点"]}\n\n'
        "# ルール\n"
        "- 雑談・連絡・質問など営業の報告でなければ is_report を false\n"
        "- deal_id は会社名や担当者名がはっきり一致する案件が1つだけのとき。複数ありうる・名前がないときは null にして candidates に候補\n"
        "- confidence は deal_id に確信があるときだけ high\n"
        f"- stage は投稿で明確に進んだときだけ。使える値: {STAGE_WORDS}\n"
        f"- 失注のときは lost_reason を {', '.join(sales.LOST_REASONS)} から選ぶ。分からなければ その他\n"
        "- 「来週火曜」などの日付は今日を基準に YYYY-MM-DD に直す。曖昧なら null にして unclear に書く\n"
        "- 投稿にない事実を作らない"
    )
    data = parse_json(claude(prompt, 1000))
    if not data:
        raise RuntimeError("AIの出力を読み取れませんでした")
    return data


# ── 処理本体 ──
class SlackHandler:
    def __init__(self, fetch_cards: Callable, get_tenant: Callable, get_sheets_service: Callable,
                 make_card_id: Callable, clear_cache: Callable, image_path_for: Callable):
        self.fetch_cards = fetch_cards
        self.get_tenant = get_tenant
        self.get_sheets_service = get_sheets_service
        self.make_card_id = make_card_id
        self.clear_cache = clear_cache
        self.image_path_for = image_path_for

    # 入口
    def handle_event(self, ev: dict):
        tenants = channel_tenants()
        channel = ev.get("channel")
        if channel not in tenants:
            return
        if ev.get("bot_id") or ev.get("subtype") in ("bot_message", "message_changed", "message_deleted",
                                                      "channel_join", "channel_leave"):
            return
        tenant = tenants[channel]
        ts = ev.get("ts")
        thread_ts = ev.get("thread_ts")
        text = (ev.get("text") or "").strip()
        files = ev.get("files") or []
        try:
            if thread_ts and thread_ts != ts:
                self.handle_thread_reply(tenant, channel, thread_ts, text)
            elif files:
                self.handle_files(tenant, channel, ts, files, ev.get("user"), text)
            elif text:
                self.handle_report(tenant, channel, ts, text)
        except Exception as e:
            log.exception("Slack処理エラー")
            post(channel, thread_ts or ts, f":warning: 処理中にエラーが起きました: {e}")

    # 名刺の画像・資料ファイル
    def handle_files(self, tenant, channel, ts, files, user_id, text):
        import intake
        images = [f for f in files if (f.get("mimetype") or "") in IMAGE_TYPES]
        docs = [f for f in files if f not in images
                and os.path.splitext(f.get("name") or "")[1].lower() in intake.SUPPORTED]
        others = [f for f in files if f not in images and f not in docs]
        results = []
        if images:
            poster = user_name(user_id)
            known = {c["id"] for c in self.fetch_cards(tenant)}
            for f in images:
                results.append(self.register_card(tenant, channel, ts, f, poster, known))
        if others:
            old = [f.get("name") for f in others
                   if os.path.splitext(f.get("name") or "")[1].lower() in (".xls", ".doc", ".ppt")]
            if old:
                results.append(f"・{'、'.join(old)} は古い形式なので読めません。xlsx・docx・pptx で保存し直すかPDFにしてください")
            if len(old) < len(others):
                results.append("・読めない形式のファイルは飛ばしました（名刺画像・Excel・Word・PowerPoint・PDF・テキストに対応）")
        if results:
            post(channel, ts, "\n".join(results))
        for f in docs:
            # 資料に添えた文章は補足としてAIに渡す
            self.handle_document(tenant, channel, ts, f, text)
        if not images and not docs and text:
            self.handle_report(tenant, channel, ts, text)

    def handle_document(self, tenant, channel, ts, f, text):
        import intake
        from fastapi import HTTPException
        name = f.get("name") or "資料"
        try:
            raw = download(f.get("url_private_download") or f.get("url_private"))
            own = (self.get_tenant(tenant) or {}).get("display_name") or tenant
            job_id = intake.create_job(tenant, own, "slack", name, raw, text)
            job = intake.job_view(tenant, job_id, self.fetch_cards(tenant))
        except HTTPException as e:
            post(channel, ts, f":warning: 「{name}」を読み取れませんでした: {e.detail}")
            return
        except Exception as e:
            log.exception("資料取り込みエラー")
            post(channel, ts, f":warning: 「{name}」を読み取れませんでした: {e}")
            return
        items = job["items"]
        link = f"{app_url()}/sales.html?intake={job_id}"
        if not items:
            msg = f":page_facing_up: 「{name}」から案件になりそうな内容は見つかりませんでした"
            if job.get("overview"):
                msg += f"\n概要: {job['overview']}"
            post(channel, ts, msg)
            return

        def who(it):
            return " ".join(x for x in (it.get("company"), it.get("person")) if x) or "（不明）"

        def what(it):
            return it.get("title") or (it.get("summary") or "").split("\n")[0][:40]

        def where(it):
            if it["action"] == "existing":
                return f"#{it['deal_id']} に記録"
            return "新規案件"

        ok = [i for i in items if i["status"] == "matched"]
        amb = [i for i in items if i["status"] == "ambiguous"]
        nf = [i for i in items if i["status"] == "not_found"]
        lines = [f":page_facing_up: 「{name}」から {len(items)}件読み取りました"]
        if job.get("overview"):
            lines.append(f"概要: {job['overview']}")
        if ok:
            lines.append(f"\n:white_check_mark: 名刺が確定（{len(ok)}件）")
            for it in ok:
                card = next(c["card"] for c in it["candidates"] if c["card"]["id"] == it["card_id"])
                stage = f"・{it['stage']}" if it.get("stage") else ""
                lines.append(f"• {card['company']} {card['name']} → {where(it)}{stage}：{what(it)}")
        if amb:
            lines.append(f"\n:grey_question: 名刺の候補が複数・確認が必要（{len(amb)}件）")
            lines += [f"• {who(it)}：{what(it)}" for it in amb]
        if nf:
            lines.append(f"\n:warning: 名刺が見つからない（{len(nf)}件）")
            lines += [f"• {who(it)}：{what(it)}" for it in nf]
        if job.get("unclear"):
            lines.append("\n読み取れなかった点: " + " / ".join(job["unclear"][:5]))
        lines.append(f"\n確認して追加: {link}")
        if ok:
            lines.append(f"_名刺が確定した{len(ok)}件だけでよければ、このスレッドに「追加」と返信してください_")
        post(channel, ts, "\n".join(lines))
        with sales._conn() as c:
            c.execute("""INSERT INTO slack_actions (tenant_id, channel, thread_ts, kind, status, parse, message, created_at)
                VALUES (?,?,?,?,?,?,?,?)""", (tenant, channel, ts, "intake", "pending",
                                              json.dumps({"job_id": job_id}), name, sales._now()))
            c.commit()

    def apply_intake(self, tenant, channel, thread_ts, action: dict):
        import intake
        from fastapi import HTTPException
        job_id = json.loads(action["parse"])["job_id"]
        try:
            job = intake.job_view(tenant, job_id, self.fetch_cards(tenant))
            decisions = intake.default_decisions(job["items"])
            if not any(d.action != "skip" for d in decisions):
                post(channel, thread_ts, "名刺が確定したものがありません。画面で確認してください: "
                     f"{app_url()}/sales.html?intake={job_id}")
                return
            res = intake.apply_decisions(tenant, job_id, decisions, self.fetch_cards(tenant))
        except HTTPException as e:
            post(channel, thread_ts, f":warning: {e.detail}")
            return
        with sales._conn() as c:
            c.execute("UPDATE slack_actions SET status='resolved' WHERE id=?", (action["id"],))
            c.commit()
        parts = []
        if res["created"]:
            parts.append("新規 " + " ".join(f"#{i}" for i in res["created"]))
        if res["recorded"]:
            parts.append("記録 " + " ".join(f"#{i}" for i in res["recorded"]))
        msg = ":white_check_mark: 追加しました（" + " / ".join(parts) + "）"
        if res["skipped"]:
            msg += f"\n確認が必要な{res['skipped']}件は追加していません: {app_url()}/sales.html?intake={job_id}"
        if res["errors"]:
            msg += "\n:warning: " + " / ".join(res["errors"])
        post(channel, thread_ts, msg)

    def register_card(self, tenant, channel, ts, f, poster, known) -> str:
        name = f.get("name") or "画像"
        try:
            raw = download(f.get("url_private_download") or f.get("url_private"))
            jpeg = shrink_image(raw)
            data = extract_card(jpeg)
        except Exception as e:
            return f":warning: {name}: 読み取れませんでした（{e}）"
        if not data.get("is_card") or not (data["name"] or data["company"]):
            return f":question: {name}: 名刺として読み取れませんでした"

        company = data["company"] or "不明"
        card_id = self.make_card_id(tenant, company, data["name"])
        label = f"{data['company'] or '(会社名不明)'} {data['name'] or '(氏名不明)'}"
        if card_id in known:
            return f":information_source: {label} は登録済みです → <{app_url()}/sales.html?new={card_id}|案件を作成>"

        t = self.get_tenant(tenant) or {}
        sheet = t.get("sheet_name", "名刺")
        row = [data["company"], data["name"], data["department_position"], data["email"], data["address"],
               data["mobile"], data["phone"], data["website"],
               datetime.now().strftime(env("SLACK_SHEET_DATE_FORMAT", "%Y/%m/%d %H:%M")),
               poster, data["name_kana"]]
        self.get_sheets_service().spreadsheets().values().append(
            spreadsheetId=t["spreadsheet_id"], range=f"'{sheet}'!A:K",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS", body={"values": [row]},
        ).execute()
        known.add(card_id)
        try:
            path = self.image_path_for(tenant, card_id, "front")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path + ".tmp", "wb") as fp:
                fp.write(shrink_image(raw, 1200))
            os.replace(path + ".tmp", path)
        except Exception as e:
            log.warning("名刺画像の保存に失敗: %s", e)
        self.clear_cache(tenant)
        with sales._conn() as c:
            c.execute("""INSERT INTO slack_actions (tenant_id, channel, thread_ts, kind, status, card_id, parse, created_at)
                VALUES (?,?,?,?,?,?,?,?)""", (tenant, channel, ts, "card", "applied", card_id,
                                             json.dumps(data, ensure_ascii=False), sales._now()))
            c.commit()

        lines = [f":white_check_mark: 名刺を登録しました: *{label}*"]
        detail = " / ".join(x for x in [data["department_position"], data["email"], data["mobile"] or data["phone"]] if x)
        if detail:
            lines.append(f"　{detail}")
        missing = [FIELD_LABELS[k] for k in ("company", "name", "email") if not data[k]]
        if missing:
            lines.append(f"　:grey_question: 読み取れなかった項目: {'・'.join(missing)}")
        if data["uncertain"]:
            lines.append(f"　:eyes: 自信がない項目: {'・'.join(FIELD_LABELS[k] for k in data['uncertain'])}（名刺管理で確認してください）")
        lines.append(f"　→ <{app_url()}/sales.html?new={card_id}|この名刺で案件を作成>")
        return "\n".join(lines)

    # 文章の報告
    def open_deals(self, tenant) -> list:
        with sales._conn() as c:
            rows = c.execute("""SELECT id, company_snapshot, contact_snapshot, title, stage FROM deals
                WHERE tenant_id=? AND stage NOT IN ('受注','失注') ORDER BY updated_at DESC LIMIT 300""",
                             (tenant,)).fetchall()
        return [{"id": r["id"], "company": r["company_snapshot"], "contact": r["contact_snapshot"],
                 "title": r["title"], "stage": r["stage"]} for r in rows]

    def handle_report(self, tenant, channel, ts, text):
        deals = self.open_deals(tenant)
        p = parse_report(text, deals, date.today())
        if not p.get("is_report"):
            return  # 雑談などは無視
        ids = {d["id"] for d in deals}
        deal_id = p.get("deal_id")
        if deal_id in ids and p.get("confidence") == "high":
            msg = self.apply_report(tenant, channel, ts, deal_id, p, text)
            post(channel, ts, msg + "\n_違っていたら このスレッドに「取消」と返信してください_")
            return
        # 特定できない → 保留して聞き返す
        with sales._conn() as c:
            c.execute("""INSERT INTO slack_actions (tenant_id, channel, thread_ts, kind, status, parse, message, created_at)
                VALUES (?,?,?,?,?,?,?,?)""", (tenant, channel, ts, "report", "pending",
                                             json.dumps(p, ensure_ascii=False), text, sales._now()))
            c.commit()
        cands = [d for d in deals if d["id"] in (p.get("candidates") or [])][:5]
        lines = [":question: どの案件の報告か特定できませんでした。記録はまだしていません。"]
        if cands:
            lines.append("候補:")
            lines += [f"　`#{d['id']}` {d['company'] or ''} {d['contact'] or ''}｜{d['title']}（{d['stage']}）" for d in cands]
        else:
            lines.append("該当しそうな進行中の案件が見つかりません。新しい相手なら先に名刺から案件を作成してください。")
        lines.append("このスレッドに `#案件番号` を返信すると その案件に記録します（案件番号は営業画面の案件名の左に #12 のように表示されています）")
        if p.get("unclear"):
            lines.append(f"判断できなかった点: {'／'.join(p['unclear'])}")
        post(channel, ts, "\n".join(lines))

    def apply_report(self, tenant, channel, ts, deal_id, p, text) -> str:
        kind = p.get("kind") if p.get("kind") in sales.USER_ACTIVITY_KINDS else "memo"
        title = (p.get("title") or "Slackからの報告")[:200]
        body = (p.get("summary") or "").strip()
        body = f"{body}\n\n— Slackの投稿 —\n{text}" if body else text
        notes = []
        with sales._conn() as c:
            row = dict(sales._get_deal_row(c, tenant, deal_id))
            prev = {k: row[k] for k in ("stage", "next_action", "next_action_due", "lost_reason", "lost_detail", "closed_at")}
            before = c.execute("SELECT COALESCE(MAX(id),0) m FROM activities").fetchone()["m"]
            sales.log_activity(c, tenant, deal_id, kind, title, body[:20000])
            sets = {}
            stage = p.get("stage")
            if stage and stage in sales.STAGES and stage != row["stage"]:
                reason = p.get("lost_reason") if p.get("lost_reason") in sales.LOST_REASONS else None
                if stage == "失注" and not reason:
                    reason = "その他"
                    notes.append("失注理由が分からないため「その他」にしました")
                if stage == "失注":
                    sets["lost_reason"] = reason
                    sets["lost_detail"] = (p.get("summary") or "")[:1000] or None
                sales.change_stage(c, tenant, deal_id, row["stage"], stage, sets,
                                   lost_reason=reason, lost_detail=sets.get("lost_detail"))
            if stage not in sales.CLOSED_STAGES:
                if p.get("next_action"):
                    sets["next_action"] = str(p["next_action"])[:200]
                due = p.get("next_action_due")
                if due:
                    try:
                        sets["next_action_due"] = sales._clean_date(due)
                    except Exception:
                        notes.append(f"日付「{due}」を読み取れませんでした")
            sales.apply_sets(c, tenant, deal_id, sets)
            new_ids = [r["id"] for r in c.execute(
                "SELECT id FROM activities WHERE id>? AND deal_id=?", (before, deal_id)).fetchall()]
            c.execute("""INSERT INTO slack_actions (tenant_id, channel, thread_ts, kind, status, deal_id,
                activity_ids, prev, parse, message, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (tenant, channel, ts, "report", "applied", deal_id, json.dumps(new_ids),
                 json.dumps(prev, ensure_ascii=False), json.dumps(p, ensure_ascii=False), text, sales._now()))
            c.commit()
            after = dict(sales._get_deal_row(c, tenant, deal_id))

        kinds = sales.ACTIVITY_KINDS
        lines = [f":memo: *{after['company_snapshot'] or ''} {after['contact_snapshot'] or ''}｜{after['title']}* に記録しました",
                 f"　{kinds.get(kind, kind)}: {title}"]
        if after["stage"] != prev["stage"]:
            lines.append(f"　ステージ: {prev['stage']} → *{after['stage']}*")
        if after["next_action"] and (after["next_action"], after["next_action_due"]) != (prev["next_action"], prev["next_action_due"]):
            due = f"（{after['next_action_due']}）" if after["next_action_due"] else ""
            lines.append(f"　次のアクション: {after['next_action']}{due}")
        if p.get("unclear"):
            notes += p["unclear"]
        if notes:
            lines.append(f"　:grey_question: {'／'.join(notes)}")
        lines.append(f"　→ <{app_url()}/sales.html?deal={deal_id}|案件を開く>")
        return "\n".join(lines)

    # スレッドへの返信（取消・#番号）
    def handle_thread_reply(self, tenant, channel, thread_ts, text):
        t = text.strip()
        with sales._conn() as c:
            acts = [dict(r) for r in c.execute("""SELECT * FROM slack_actions WHERE channel=? AND thread_ts=?
                AND tenant_id=? ORDER BY id DESC""", (channel, thread_ts, tenant)).fetchall()]
        if not acts:
            return
        if t in ADD_WORDS:
            target = next((a for a in acts if a["kind"] == "intake" and a["status"] == "pending"), None)
            if target:
                self.apply_intake(tenant, channel, thread_ts, target)
            return
        if t in UNDO_WORDS:
            if any(a["kind"] == "intake" and a["status"] == "resolved" for a in acts) and \
                    not any(a["kind"] == "report" and a["status"] == "applied" for a in acts):
                post(channel, thread_ts, "資料からの追加はここからは取り消せません。営業画面で案件を開いて削除してください")
                return
            target = next((a for a in acts if a["kind"] == "report" and a["status"] == "applied"), None)
            if not target:
                if any(a["kind"] == "card" for a in acts):
                    post(channel, thread_ts, "名刺の登録はここからは取り消せません。名刺管理の画面で削除してください")
                return
            self.undo(target)
            post(channel, thread_ts, ":leftwards_arrow_with_hook: 取り消しました（記録を削除し、案件を元の状態に戻しました）")
            return
        m = re.fullmatch(r"#\s*(\d+)", t)
        if m:
            target = next((a for a in acts if a["kind"] == "report" and a["status"] in ("pending", "undone")), None)
            if not target:
                return
            deal_id = int(m.group(1))
            ids = {d["id"] for d in self.open_deals(tenant)}
            if deal_id not in ids:
                post(channel, thread_ts, f"#{deal_id} は進行中の案件に見つかりません")
                return
            msg = self.apply_report(tenant, channel, thread_ts, deal_id, json.loads(target["parse"]), target["message"])
            with sales._conn() as c:
                c.execute("UPDATE slack_actions SET status='resolved' WHERE id=?", (target["id"],))
                c.commit()
            post(channel, thread_ts, msg + "\n_違っていたら「取消」と返信してください_")

    def undo(self, a: dict):
        prev = json.loads(a["prev"] or "{}")
        ids = json.loads(a["activity_ids"] or "[]")
        with sales._conn() as c:
            if ids:
                c.execute(f"DELETE FROM activities WHERE tenant_id=? AND id IN ({','.join('?' * len(ids))})",
                          [a["tenant_id"]] + ids)
            if prev:
                sets = dict(prev)
                sales.apply_sets(c, a["tenant_id"], a["deal_id"], sets)
            c.execute("UPDATE slack_actions SET status='undone' WHERE id=?", (a["id"],))
            c.commit()


def build_router(handler: SlackHandler) -> APIRouter:
    r = APIRouter()

    @r.post("/api/slack/events")
    async def events(request: Request, background: BackgroundTasks):
        body = await request.body()
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return JSONResponse({"error": "bad json"}, status_code=400)
        # 初回のURL確認（署名シークレット未設定でも応答できるようにする）
        if payload.get("type") == "url_verification" and (
                not env("SLACK_SIGNING_SECRET") or verify_signature(request.headers, body)):
            return {"challenge": payload.get("challenge")}
        if not verify_signature(request.headers, body):
            return JSONResponse({"error": "invalid signature"}, status_code=401)
        if request.headers.get("x-slack-retry-num"):
            return {"ok": True}  # 再送は無視（処理は1回目で実行中）
        if payload.get("type") == "event_callback":
            ev = payload.get("event") or {}
            if ev.get("type") == "message" and not _seen(payload.get("event_id", "")):
                background.add_task(handler.handle_event, ev)
        return {"ok": True}

    return r


# ── 毎朝の要対応通知 ──
def notify_alerts(today: Optional[date] = None) -> int:
    today = today or date.today()
    sent = 0
    for channel, tenant in channel_tenants().items():
        with sales._conn() as c:
            settings = sales._get_settings(c, tenant)
            deals = [dict(r) for r in c.execute("""SELECT * FROM deals WHERE tenant_id=?
                AND stage NOT IN ('受注','失注')""", (tenant,)).fetchall()]
            items = []
            for d in deals:
                acts = sales.deal_acts(c, tenant, d["id"])
                alerts = [a for a in sales.compute_alerts(d, acts, settings, today) if a["level"] != "info"]
                if alerts:
                    items.append((d, alerts))
        if not items:
            continue
        items.sort(key=lambda x: (0 if any(a["type"] == "no_reply" for a in x[1]) else 1, x[0]["id"]))
        lines = [f":bell: *要対応の案件が {len(items)} 件あります*（{today.month}/{today.day}）"]
        for d, alerts in items[:30]:
            msg = " ／ ".join(a["message"] for a in alerts)
            lines.append(f"• <{app_url()}/sales.html?deal={d['id']}|{d['company_snapshot'] or ''} {d['contact_snapshot'] or ''}>"
                         f"｜{d['title']} — {msg}")
        if len(items) > 30:
            lines.append(f"ほか {len(items) - 30} 件は営業画面で確認してください")
        post(channel, None, "\n".join(lines))
        sent += 1
    return sent


if __name__ == "__main__":
    # 例: cd /var/meishi/backend && venv/bin/python slack_bot.py notify
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) > 1 and sys.argv[1] == "notify":
        if date.today().weekday() >= 5:
            sys.exit(0)
        sales.init_db()
        init_db()
        print(f"通知したチャンネル: {notify_alerts()}")
    else:
        print("使い方: python slack_bot.py notify")
