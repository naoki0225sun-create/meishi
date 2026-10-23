"""
営業モジュール
==============
Phase 1:   案件 (deals) / 活動ログ (activities) / 接点 (events)
Phase 2a:  メール作成（用途テンプレート・AI下書き・署名・メーラー起動）
           返信待ちアラート（営業日数は設定で変更可）・期限切れ・放置アラート

- データは sales.db（名刺DBには書き込まない）
- 名刺本体は app.fetch_cards() から card_id で参照
- card_id が変わった場合はメールアドレスの控えで自動的に再接続
- メールはアプリから送信しない（メーラーで開く／コピー → 「送信済みにする」で記録）

app.py への組み込み:
    import sales
    sales.init_db()                                   # startup で
    app.include_router(sales.build_router(check_session, fetch_cards, get_tenant))
"""
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

SALES_DB_PATH = os.getenv("SALES_DB_PATH", "/var/meishi/backend/sales.db")

STAGES = ["名刺交換", "御礼済", "アポ", "ヒアリング", "見積提出", "受注", "失注", "保留"]
CLOSED_STAGES = {"受注", "失注"}
LOST_REASONS = ["価格", "納期", "仕様不一致", "競合", "予算凍結", "連絡途絶", "その他"]
ACTIVITY_KINDS = {
    "meeting": "アポ・打合せ",
    "email": "メール",
    "call": "電話",
    "memo": "メモ",
    "stage": "ステージ変更",  # システムが自動で記録
}
USER_ACTIVITY_KINDS = {"meeting", "email", "call", "memo"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FOLLOWUP_ACTION = "返信確認（なければ電話）"
DEFAULT_SETTINGS = {"reply_wait_days": 3, "stale_days": 14}

TEMPLATE_VARS = ["会社", "名前", "役職", "接点", "接点日", "案件名", "自社"]

DEFAULT_PURPOSES = [
    {
        "label": "とりあえずの御礼",
        "subject_tpl": "{{接点}}ではありがとうございました（{{自社}}）",
        "body_tpl": (
            "{{会社}}\n{{名前}} 様\n\n"
            "お世話になっております。{{自社}}です。\n"
            "先日は{{接点}}でお時間をいただきありがとうございました。\n\n"
            "弊社でお役に立てることがございましたら\n"
            "どうぞお気軽にお声がけください。\n\n"
            "今後ともよろしくお願いいたします。"
        ),
        "instruction": (
            "名刺交換のお礼。短く丁寧に。会話の内容が記録にあれば一言触れる。"
            "売り込みは控えめにし、気軽に相談してほしいと伝える。"
        ),
    },
    {
        "label": "見積もり依頼",
        "subject_tpl": "お見積りのご案内（{{自社}}）",
        "body_tpl": (
            "{{会社}}\n{{名前}} 様\n\n"
            "お世話になっております。{{自社}}です。\n"
            "先日は{{接点}}でありがとうございました。\n\n"
            "お話しいただいた件についてぜひお見積りをさせていただければと存じます。\n"
            "図面（PDF・DXFなど）や仕様・数量・ご希望納期をお送りいただけましたら\n"
            "内容を確認のうえお見積りをご提出いたします。\n\n"
            "どうぞよろしくお願いいたします。"
        ),
        "instruction": (
            "見積りの提出を申し出るメール。図面・仕様・数量・希望納期を送ってほしいと依頼する。"
            "記録にある具体的な製品や数量があれば触れる。"
        ),
    },
    {
        "label": "Blue Print取材の日程調整",
        "only_tenant": "ワコーメタル",   # テナント表示名にこの文字列を含む場合だけ追加
        "subject_tpl": "取材のお願い（ものづくりメディア Blue Print）",
        "body_tpl": (
            "{{会社}}\n{{名前}} 様\n\n"
            "お世話になっております。{{自社}}です。\n"
            "先日は{{接点}}でありがとうございました。\n\n"
            "弊社では大阪のものづくり企業を紹介するメディア「Blue Print」（https://w-blueprint.com）を運営しております。\n"
            "貴社の技術や取り組みをぜひ記事でご紹介させていただきたく、取材のお願いでご連絡いたしました。\n"
            "取材・撮影・記事制作・掲載はすべて無料です。\n\n"
            "ご都合のよい日時の候補をいくつかお知らせいただけますでしょうか。\n\n"
            "どうぞよろしくお願いいたします。"
        ),
        "instruction": (
            "Blue Print（w-blueprint.com・大阪のものづくり企業を紹介するメディア）への取材依頼と日程調整。"
            "取材・撮影・記事制作・掲載はすべて無料であることを必ず伝え、日程候補を聞く。"
        ),
    },
]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def _conn():
    conn = sqlite3.connect(SALES_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def _add_column(c, table: str, col: str, ddl: str):
    cols = [r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
    if col not in cols:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_db():
    os.makedirs(os.path.dirname(SALES_DB_PATH), exist_ok=True)
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            name TEXT NOT NULL,
            event_date TEXT,
            place TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS deals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            card_id TEXT NOT NULL,
            company_snapshot TEXT,
            contact_snapshot TEXT,
            email_snapshot TEXT,
            event_id INTEGER REFERENCES events(id) ON DELETE SET NULL,
            title TEXT NOT NULL,
            stage TEXT NOT NULL,
            amount INTEGER,
            lost_reason TEXT,
            lost_detail TEXT,
            next_action TEXT,
            next_action_due TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            closed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS activities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            deal_id INTEGER NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            title TEXT,
            body TEXT,
            summary TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mail_purposes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            label TEXT NOT NULL,
            instruction TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS sales_settings (
            tenant_id TEXT PRIMARY KEY,
            reply_wait_days INTEGER NOT NULL DEFAULT 3,
            stale_days INTEGER NOT NULL DEFAULT 14,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS signatures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            label TEXT NOT NULL,
            body TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_events_tenant ON events(tenant_id);
        CREATE INDEX IF NOT EXISTS idx_deals_tenant ON deals(tenant_id, stage);
        CREATE INDEX IF NOT EXISTS idx_deals_card ON deals(tenant_id, card_id);
        CREATE INDEX IF NOT EXISTS idx_activities_deal ON activities(deal_id, occurred_at);
        CREATE INDEX IF NOT EXISTS idx_signatures_tenant ON signatures(tenant_id, sort_order);
        """)
        # Phase 2a マイグレーション
        _add_column(c, "activities", "awaiting_reply", "awaiting_reply INTEGER NOT NULL DEFAULT 0")
        _add_column(c, "activities", "replied_at", "replied_at TEXT")
        _add_column(c, "mail_purposes", "subject_tpl", "subject_tpl TEXT NOT NULL DEFAULT ''")
        _add_column(c, "mail_purposes", "body_tpl", "body_tpl TEXT NOT NULL DEFAULT ''")
        c.commit()


# ── 入力モデル ──
class EventCreate(BaseModel):
    name: str
    event_date: Optional[str] = None
    place: Optional[str] = None


class DealCreate(BaseModel):
    card_id: str
    title: Optional[str] = None
    stage: str = "名刺交換"
    amount: Optional[int] = None
    event_id: Optional[int] = None
    next_action: Optional[str] = None
    next_action_due: Optional[str] = None


class DealUpdate(BaseModel):
    title: Optional[str] = None
    stage: Optional[str] = None
    amount: Optional[int] = None
    event_id: Optional[int] = None
    lost_reason: Optional[str] = None
    lost_detail: Optional[str] = None
    next_action: Optional[str] = None
    next_action_due: Optional[str] = None


class RepliedBody(BaseModel):
    note: Optional[str] = None


class ActivityCreate(BaseModel):
    kind: str = "memo"
    occurred_at: Optional[str] = None
    title: Optional[str] = None
    body: Optional[str] = None
    awaiting_reply: bool = False
    next_action: Optional[str] = None
    next_action_due: Optional[str] = None


class SettingsUpdate(BaseModel):
    reply_wait_days: int
    stale_days: int


class SignatureIn(BaseModel):
    label: str
    body: str


class PurposeIn(BaseModel):
    label: str
    subject_tpl: str = ""
    body_tpl: str = ""
    instruction: str = ""


class DraftRequest(BaseModel):
    purpose_id: int
    mode: str = "template"   # template | ai
    note: Optional[str] = None  # AIへの追加指示（任意）


class EmailLog(BaseModel):
    subject: str
    body: str
    advance_stage: Optional[str] = None   # 例: 名刺交換 → 御礼済
    set_followup: bool = True             # 返信確認を次のアクションに入れる


# ── ヘルパー ──
def _dump(model: BaseModel) -> dict:
    fn = getattr(model, "model_dump", None)
    return fn(exclude_unset=True) if fn else model.dict(exclude_unset=True)


def _clean_text(v: Optional[str], limit: int, field: str) -> Optional[str]:
    if v is None:
        return None
    v = v.strip()
    if len(v) > limit:
        raise HTTPException(400, f"{field}が長すぎます（{limit}文字以内）")
    return v or None


def _clean_date(v: Optional[str]) -> Optional[str]:
    if v is None or v == "":
        return None
    if not DATE_RE.match(v):
        raise HTTPException(400, "日付は YYYY-MM-DD 形式で指定してください")
    try:
        date.fromisoformat(v)
    except ValueError:
        raise HTTPException(400, "日付が不正です")
    return v


def business_days_between(start: date, end: date) -> int:
    """start の翌日から end までの平日（土日を除く）の日数"""
    if end <= start:
        return 0
    n, d = 0, start
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def add_business_days(start: date, n: int) -> date:
    d = start
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def compute_alerts(deal: dict, acts: list, settings: dict, today: Optional[date] = None) -> list:
    """acts は occurred_at の新しい順。level: high（要対応）/ mid（要対応）/ info（参考）"""
    today = today or date.today()
    if deal["stage"] in CLOSED_STAGES:
        return []
    alerts = []
    due = deal.get("next_action_due")
    if due and due < today.isoformat():
        alerts.append({"type": "overdue", "level": "high",
                       "message": f"次のアクション「{deal.get('next_action') or '未設定'}」が期限切れ（{due[5:].replace('-', '/')}）"})

    user_acts = [a for a in acts if a["kind"] != "stage"]
    last_email = next((a for a in user_acts if a["kind"] == "email" and a["awaiting_reply"]), None)
    if last_email and not last_email["replied_at"]:
        followed = any(a["occurred_at"] > last_email["occurred_at"] and a["kind"] in ("meeting", "call")
                       for a in user_acts)
        if not followed:
            sent = date.fromisoformat(last_email["occurred_at"][:10])
            n = business_days_between(sent, today)
            wait = settings["reply_wait_days"]
            if n >= wait:
                alerts.append({"type": "no_reply", "level": "high", "activity_id": last_email["id"],
                               "message": f"メール送信から{n}営業日 返信なし → 電話しましょう"})
            else:
                alerts.append({"type": "waiting", "level": "info", "activity_id": last_email["id"],
                               "message": f"返信待ち（あと{wait - n}営業日で電話アラート）"})

    last = user_acts[0]["occurred_at"][:10] if user_acts else deal["created_at"][:10]
    idle = (today - date.fromisoformat(last)).days
    if idle >= settings["stale_days"] and not any(a["type"] in ("no_reply", "waiting") for a in alerts):
        alerts.append({"type": "stale", "level": "mid", "message": f"{idle}日間 記録がありません"})
    return alerts


def render_template(tpl: str, values: dict) -> str:
    out = tpl or ""
    for k in TEMPLATE_VARS:
        out = out.replace("{{" + k + "}}", values.get(k) or "")
    return out


def _card_summary(card: Optional[dict]) -> Optional[dict]:
    if not card:
        return None
    keys = ("id", "name", "company", "department_position", "email", "mobile", "phone", "has_image")
    return {k: card.get(k) for k in keys}


def _check_event(c, tenant_id: str, event_id: Optional[int]):
    if event_id is None:
        return
    row = c.execute("SELECT 1 FROM events WHERE id=? AND tenant_id=?", (event_id, tenant_id)).fetchone()
    if not row:
        raise HTTPException(404, "接点が見つかりません")


def _get_deal_row(c, tenant_id: str, deal_id: int):
    row = c.execute("""
        SELECT d.*, e.name AS event_name, e.event_date AS event_date
        FROM deals d LEFT JOIN events e ON e.id = d.event_id
        WHERE d.id=? AND d.tenant_id=?""", (deal_id, tenant_id)).fetchone()
    if not row:
        raise HTTPException(404, "案件が見つかりません")
    return row


def _get_settings(c, tenant_id: str) -> dict:
    row = c.execute("SELECT reply_wait_days, stale_days FROM sales_settings WHERE tenant_id=?",
                    (tenant_id,)).fetchone()
    return dict(row) if row else dict(DEFAULT_SETTINGS)


def _seed_purposes(c, tenant_id: str, tenant_display: str = ""):
    n = c.execute("SELECT COUNT(*) n FROM mail_purposes WHERE tenant_id=?", (tenant_id,)).fetchone()["n"]
    if n:
        # 対象外のテナントに未編集の初期用途が入っていたら外す（Blue Print など）
        for p in DEFAULT_PURPOSES:
            if p.get("only_tenant") and p["only_tenant"] not in tenant_display:
                c.execute("""DELETE FROM mail_purposes WHERE tenant_id=? AND label=? AND body_tpl=?""",
                          (tenant_id, p["label"], p["body_tpl"]))
        c.commit()
        return
    i = 0
    for p in DEFAULT_PURPOSES:
        if p.get("only_tenant") and p["only_tenant"] not in tenant_display:
            continue
        c.execute("""INSERT INTO mail_purposes (tenant_id, label, instruction, subject_tpl, body_tpl, sort_order)
            VALUES (?,?,?,?,?,?)""", (tenant_id, p["label"], p["instruction"], p["subject_tpl"], p["body_tpl"], i))
        i += 1
    c.commit()


def _deal_dict(row, card: Optional[dict], last_activity_at: Optional[str] = None,
               alerts: Optional[list] = None) -> dict:
    d = dict(row)
    today = date.today().isoformat()
    due = d.get("next_action_due")
    is_open = d["stage"] not in CLOSED_STAGES
    d["is_open"] = is_open
    d["overdue"] = bool(is_open and due and due < today)
    d["due_today"] = bool(is_open and due and due == today)
    if card:
        d["card_id"] = card["id"]  # 再接続後のIDを返す
    d["card"] = _card_summary(card)
    d["card_missing"] = card is None
    d["last_activity_at"] = last_activity_at
    d["alerts"] = alerts or []
    d["needs_action"] = any(a["level"] != "info" for a in d["alerts"])
    return d


def _ai_available() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def _ai_draft(values: dict, purpose: dict, acts: list, note: Optional[str]) -> dict:
    """Claude で件名と本文を作る。署名は付けない（画面側で選んで付ける）"""
    try:
        import anthropic
    except ImportError:
        raise HTTPException(500, "anthropic パッケージが入っていません")
    history = []
    for a in acts[:6]:
        if a["kind"] == "stage":
            continue
        line = f"- {a['occurred_at'][:10]} {ACTIVITY_KINDS.get(a['kind'], a['kind'])}: {a['title'] or ''}"
        if a["body"]:
            line += f"\n  {a['body'][:400]}"
        history.append(line)
    context = "\n".join([
        f"差出人の会社: {values['自社']}",
        f"相手の会社: {values['会社']}",
        f"相手の氏名: {values['名前']}",
        f"相手の役職: {values['役職'] or '不明'}",
        f"出会った場（接点）: {values['接点'] or '不明'} {values['接点日'] or ''}",
        f"案件名: {values['案件名']}",
        "これまでの記録:\n" + ("\n".join(history) if history else "（なし）"),
    ])
    prompt = (
        f"以下の情報をもとに日本語のビジネスメールを1通作成してください。\n\n"
        f"# 目的\n{purpose['label']}\n{purpose['instruction']}\n\n"
        f"# 参考にする雛形（言い回しの参考。そのままでなくてよい）\n件名: {purpose['subject_tpl']}\n{purpose['body_tpl']}\n\n"
        f"# 相手と経緯\n{context}\n\n"
        + (f"# 追加の指示\n{note}\n\n" if note else "")
        + "# 条件\n"
        "- 宛名は「会社名」「氏名 様」の2行で始める\n"
        "- 記録にない事実（製品名・金額・日付など）を作らない\n"
        "- 署名は付けない\n"
        "- 出力は JSON のみ: {\"subject\": \"件名\", \"body\": \"本文\"}"
    )
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    try:
        msg = client.messages.create(
            model=os.getenv("SALES_AI_MODEL", "claude-sonnet-5-5"),
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        raise HTTPException(502, f"AIの呼び出しに失敗しました: {type(e).__name__}: {e}")
    text = "".join(getattr(b, "text", "") for b in msg.content)
    m = re.search(r"\{.*\}", text, re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        data = {}
    if not data.get("body"):
        raise HTTPException(502, "AIの出力を読み取れませんでした。もう一度お試しください")
    return {"subject": (data.get("subject") or "").strip(), "body": data["body"].strip()}


# ── 共通処理（Slack連携からも使う） ──
def log_activity(c, tenant_id, deal_id, kind, title, body=None, occurred_at=None, awaiting_reply=False):
    cur = c.execute("""INSERT INTO activities
        (tenant_id, deal_id, kind, occurred_at, title, body, awaiting_reply, created_at)
        VALUES (?,?,?,?,?,?,?,?)""",
        (tenant_id, deal_id, kind, occurred_at or _now(), title, body, 1 if awaiting_reply else 0, _now()))
    return cur.lastrowid

def deal_acts(c, tenant_id, deal_id):
    return [dict(a) for a in c.execute("""SELECT * FROM activities WHERE deal_id=? AND tenant_id=?
        ORDER BY occurred_at DESC, id DESC""", (deal_id, tenant_id)).fetchall()]

def change_stage(c, tenant_id, deal_id, cur_stage, new_stage, sets, lost_reason=None, lost_detail=None):
    if new_stage not in STAGES:
        raise HTTPException(400, "ステージが不正です")
    if new_stage == "失注":
        if not lost_reason:
            raise HTTPException(400, "失注にするには失注理由を選んでください")
    else:
        sets["lost_reason"] = None
        sets["lost_detail"] = None
    if new_stage in CLOSED_STAGES:
        sets["closed_at"] = _now()
        sets["next_action"] = None
        sets["next_action_due"] = None
    else:
        sets["closed_at"] = None
    sets["stage"] = new_stage
    note = f"{cur_stage} → {new_stage}"
    if new_stage == "失注":
        note += f"（{lost_reason}）"
    log_activity(c, tenant_id, deal_id, "stage", note,
                 body=lost_detail if new_stage == "失注" else None)

def apply_sets(c, tenant_id, deal_id, sets):
    sets["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in sets)
    c.execute(f"UPDATE deals SET {cols} WHERE id=? AND tenant_id=?",
              list(sets.values()) + [deal_id, tenant_id])


def insert_deal(c, tenant_id, card, title=None, stage="名刺交換", amount=None, event_id=None,
                next_action=None, next_action_due=None, created_note=None) -> int:
    """案件を1件作る（画面・取り込み・Slack 共通）"""
    if stage not in STAGES or stage in CLOSED_STAGES:
        raise HTTPException(400, "新規案件のステージが不正です")
    if amount is not None and amount < 0:
        raise HTTPException(400, "金額が不正です")
    _check_event(c, tenant_id, event_id)
    now = _now()
    title = _clean_text(title, 120, "案件名")
    if not title:
        ev = None
        if event_id:
            ev = c.execute("SELECT name FROM events WHERE id=?", (event_id,)).fetchone()
        title = f"{ev['name']} での接点" if ev else "新規案件"
    cur = c.execute("""INSERT INTO deals
        (tenant_id, card_id, company_snapshot, contact_snapshot, email_snapshot,
         event_id, title, stage, amount, next_action, next_action_due,
         created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (tenant_id, card["id"], card.get("company"), card.get("name"), card.get("email"),
         event_id, title, stage, amount,
         _clean_text(next_action, 200, "次のアクション"),
         _clean_date(next_action_due), now, now))
    deal_id = cur.lastrowid
    log_activity(c, tenant_id, deal_id, "stage", created_note or f"案件作成（{stage}）")
    return deal_id


def build_router(check_session: Callable, fetch_cards: Callable,
                 get_tenant: Optional[Callable] = None) -> APIRouter:
    r = APIRouter(prefix="/api/sales")

    def tenant_name(tenant_id: str) -> str:
        if get_tenant:
            t = get_tenant(tenant_id) or {}
            return t.get("display_name") or tenant_id
        return tenant_id

    def card_index(tenant_id: str):
        cards = fetch_cards(tenant_id)
        by_id = {c["id"]: c for c in cards}
        by_email = {}
        for c in cards:
            e = (c.get("email") or "").strip().lower()
            if e:
                by_email.setdefault(e, c)
        return by_id, by_email

    def resolve(c, row, by_id, by_email):
        """card_id で見つからなければメールの控えで再接続する"""
        card = by_id.get(row["card_id"])
        if card:
            return card
        e = (row["email_snapshot"] or "").strip().lower()
        if e and e in by_email:
            card = by_email[e]
            c.execute("UPDATE deals SET card_id=? WHERE id=?", (card["id"], row["id"]))
            return card
        return None

    # ── マスタ ──
    @r.get("/meta")
    def meta(session: dict = Depends(check_session)):
        return {
            "stages": STAGES,
            "closed_stages": sorted(CLOSED_STAGES),
            "lost_reasons": LOST_REASONS,
            "activity_kinds": ACTIVITY_KINDS,
            "template_vars": TEMPLATE_VARS,
            "ai_available": _ai_available(),
            "tenant_name": tenant_name(session["tenant_id"]),
        }

    # ── 設定 ──
    @r.get("/settings")
    def get_settings(session: dict = Depends(check_session)):
        with _conn() as c:
            return _get_settings(c, session["tenant_id"])

    @r.put("/settings")
    def put_settings(body: SettingsUpdate, session: dict = Depends(check_session)):
        if not (1 <= body.reply_wait_days <= 30):
            raise HTTPException(400, "返信待ちの営業日数は1〜30で指定してください")
        if not (1 <= body.stale_days <= 180):
            raise HTTPException(400, "放置アラートの日数は1〜180で指定してください")
        with _conn() as c:
            c.execute("""INSERT INTO sales_settings (tenant_id, reply_wait_days, stale_days, updated_at)
                VALUES (?,?,?,?) ON CONFLICT(tenant_id) DO UPDATE SET
                reply_wait_days=excluded.reply_wait_days, stale_days=excluded.stale_days,
                updated_at=excluded.updated_at""",
                (session["tenant_id"], body.reply_wait_days, body.stale_days, _now()))
            c.commit()
        return {"ok": True}

    # ── 署名 ──
    @r.get("/signatures")
    def list_signatures(session: dict = Depends(check_session)):
        with _conn() as c:
            rows = c.execute("SELECT id, label, body FROM signatures WHERE tenant_id=? ORDER BY sort_order, id",
                             (session["tenant_id"],)).fetchall()
        return {"signatures": [dict(x) for x in rows]}

    def _sig_values(body: SignatureIn):
        label = _clean_text(body.label, 40, "署名名")
        text = (body.body or "").strip("\n")
        if not label or not text.strip():
            raise HTTPException(400, "署名名と署名を入力してください")
        if len(text) > 2000:
            raise HTTPException(400, "署名が長すぎます（2000文字以内）")
        return label, text

    @r.post("/signatures")
    def create_signature(body: SignatureIn, session: dict = Depends(check_session)):
        label, text = _sig_values(body)
        t = session["tenant_id"]
        with _conn() as c:
            order = c.execute("SELECT COALESCE(MAX(sort_order),0)+1 m FROM signatures WHERE tenant_id=?",
                              (t,)).fetchone()["m"]
            cur = c.execute("INSERT INTO signatures (tenant_id, label, body, sort_order, created_at) VALUES (?,?,?,?,?)",
                            (t, label, text, order, _now()))
            c.commit()
        return {"ok": True, "id": cur.lastrowid}

    @r.put("/signatures/{sig_id}")
    def update_signature(sig_id: int, body: SignatureIn, session: dict = Depends(check_session)):
        label, text = _sig_values(body)
        with _conn() as c:
            cur = c.execute("UPDATE signatures SET label=?, body=? WHERE id=? AND tenant_id=?",
                            (label, text, sig_id, session["tenant_id"]))
            c.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "署名が見つかりません")
        return {"ok": True}

    @r.delete("/signatures/{sig_id}")
    def delete_signature(sig_id: int, session: dict = Depends(check_session)):
        with _conn() as c:
            cur = c.execute("DELETE FROM signatures WHERE id=? AND tenant_id=?", (sig_id, session["tenant_id"]))
            c.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "署名が見つかりません")
        return {"ok": True}

    # ── 用途（メールのタブ） ──
    @r.get("/purposes")
    def list_purposes(session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            _seed_purposes(c, t, tenant_name(t))
            rows = c.execute("""SELECT id, label, subject_tpl, body_tpl, instruction FROM mail_purposes
                WHERE tenant_id=? ORDER BY sort_order, id""", (t,)).fetchall()
        return {"purposes": [dict(x) for x in rows]}

    def _purpose_values(body: PurposeIn):
        label = _clean_text(body.label, 30, "用途名")
        if not label:
            raise HTTPException(400, "用途名を入力してください")
        subj = (body.subject_tpl or "").strip()
        tpl = (body.body_tpl or "").strip("\n")
        ins = (body.instruction or "").strip()
        if len(subj) > 200 or len(tpl) > 5000 or len(ins) > 1000:
            raise HTTPException(400, "入力が長すぎます")
        if not tpl.strip() and not ins:
            raise HTTPException(400, "本文の雛形かAIへの指示のどちらかを入力してください")
        return label, subj, tpl, ins

    @r.post("/purposes")
    def create_purpose(body: PurposeIn, session: dict = Depends(check_session)):
        label, subj, tpl, ins = _purpose_values(body)
        t = session["tenant_id"]
        with _conn() as c:
            order = c.execute("SELECT COALESCE(MAX(sort_order),0)+1 m FROM mail_purposes WHERE tenant_id=?",
                              (t,)).fetchone()["m"]
            cur = c.execute("""INSERT INTO mail_purposes (tenant_id, label, subject_tpl, body_tpl, instruction, sort_order)
                VALUES (?,?,?,?,?,?)""", (t, label, subj, tpl, ins, order))
            c.commit()
        return {"ok": True, "id": cur.lastrowid}

    @r.put("/purposes/{pid}")
    def update_purpose(pid: int, body: PurposeIn, session: dict = Depends(check_session)):
        label, subj, tpl, ins = _purpose_values(body)
        with _conn() as c:
            cur = c.execute("""UPDATE mail_purposes SET label=?, subject_tpl=?, body_tpl=?, instruction=?
                WHERE id=? AND tenant_id=?""", (label, subj, tpl, ins, pid, session["tenant_id"]))
            c.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "用途が見つかりません")
        return {"ok": True}

    @r.delete("/purposes/{pid}")
    def delete_purpose(pid: int, session: dict = Depends(check_session)):
        with _conn() as c:
            cur = c.execute("DELETE FROM mail_purposes WHERE id=? AND tenant_id=?", (pid, session["tenant_id"]))
            c.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "用途が見つかりません")
        return {"ok": True}

    # ── 接点 ──
    @r.get("/events")
    def list_events(session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            rows = c.execute("""
                SELECT e.*, COUNT(d.id) AS deal_count,
                       SUM(CASE WHEN d.stage='受注' THEN 1 ELSE 0 END) AS won_count
                FROM events e LEFT JOIN deals d ON d.event_id = e.id
                WHERE e.tenant_id=?
                GROUP BY e.id
                ORDER BY COALESCE(e.event_date, e.created_at) DESC""", (t,)).fetchall()
        return {"events": [dict(x) for x in rows]}

    @r.post("/events")
    def create_event(body: EventCreate, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        name = _clean_text(body.name, 100, "接点名")
        if not name:
            raise HTTPException(400, "接点名を入力してください")
        with _conn() as c:
            cur = c.execute("""INSERT INTO events (tenant_id, name, event_date, place, created_at)
                VALUES (?,?,?,?,?)""",
                (t, name, _clean_date(body.event_date), _clean_text(body.place, 100, "場所"), _now()))
            c.commit()
            return {"ok": True, "id": cur.lastrowid}

    @r.delete("/events/{event_id}")
    def delete_event(event_id: int, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            cur = c.execute("DELETE FROM events WHERE id=? AND tenant_id=?", (event_id, t))
            c.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "接点が見つかりません")
        return {"ok": True}

    # ── 案件 ──
    @r.get("/deals")
    def list_deals(q: str = "", stage: str = "", card_id: str = "",
                   session: dict = Depends(check_session)):
        t = session["tenant_id"]
        by_id, by_email = card_index(t)
        with _conn() as c:
            settings = _get_settings(c, t)
            rows = c.execute("""
                SELECT d.*, e.name AS event_name, e.event_date AS event_date
                FROM deals d LEFT JOIN events e ON e.id = d.event_id
                WHERE d.tenant_id=?
                ORDER BY d.updated_at DESC""", (t,)).fetchall()
            acts_by_deal = {}
            for a in c.execute("""SELECT id, deal_id, kind, occurred_at, awaiting_reply, replied_at
                    FROM activities WHERE tenant_id=? ORDER BY occurred_at DESC, id DESC""", (t,)):
                acts_by_deal.setdefault(a["deal_id"], []).append(dict(a))
            deals = []
            for row in rows:
                card = resolve(c, row, by_id, by_email)
                acts = acts_by_deal.get(row["id"], [])
                last = acts[0]["occurred_at"] if acts else None
                deals.append(_deal_dict(row, card, last, compute_alerts(dict(row), acts, settings)))
            c.commit()  # 再接続があれば保存

        if stage:
            deals = [d for d in deals if d["stage"] == stage]
        if card_id:
            deals = [d for d in deals if d["card_id"] == card_id]
        if q:
            ql = q.lower()

            def hit(d):
                card = d["card"] or {}
                hay = [d["title"], d["company_snapshot"], d["contact_snapshot"], d.get("event_name"),
                       d.get("next_action"), card.get("name"), card.get("company")]
                return any(ql in (h or "").lower() for h in hay)
            deals = [d for d in deals if hit(d)]
        return {"deals": deals, "total": len(deals), "settings": settings}

    @r.post("/deals")
    def create_deal(body: DealCreate, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        by_id, _ = card_index(t)
        card = by_id.get(body.card_id)
        if not card:
            raise HTTPException(404, "名刺が見つかりません")
        if body.stage not in STAGES or body.stage in CLOSED_STAGES:
            raise HTTPException(400, "新規案件のステージが不正です")
        if body.amount is not None and body.amount < 0:
            raise HTTPException(400, "金額が不正です")
        with _conn() as c:
            deal_id = insert_deal(c, t, card, body.title, body.stage, body.amount, body.event_id,
                                  body.next_action, body.next_action_due)
            c.commit()
        return {"ok": True, "id": deal_id}

    @r.get("/deals/{deal_id}")
    def get_deal(deal_id: int, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        by_id, by_email = card_index(t)
        with _conn() as c:
            settings = _get_settings(c, t)
            row = _get_deal_row(c, t, deal_id)
            card = resolve(c, row, by_id, by_email)
            acts = deal_acts(c, t, deal_id)
            c.commit()
        last = acts[0]["occurred_at"] if acts else None
        d = _deal_dict(row, card, last, compute_alerts(dict(row), acts, settings))
        d["activities"] = acts
        return d

    @r.patch("/deals/{deal_id}")
    def update_deal(deal_id: int, body: DealUpdate, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        data = _dump(body)
        if not data:
            return {"ok": True}
        with _conn() as c:
            cur = dict(_get_deal_row(c, t, deal_id))
            sets = {}

            if "title" in data:
                title = _clean_text(data["title"], 120, "案件名")
                if not title:
                    raise HTTPException(400, "案件名を入力してください")
                sets["title"] = title
            if "amount" in data:
                if data["amount"] is not None and data["amount"] < 0:
                    raise HTTPException(400, "金額が不正です")
                sets["amount"] = data["amount"]
            if "event_id" in data:
                _check_event(c, t, data["event_id"])
                sets["event_id"] = data["event_id"]
            if "next_action" in data:
                sets["next_action"] = _clean_text(data["next_action"], 200, "次のアクション")
            if "next_action_due" in data:
                sets["next_action_due"] = _clean_date(data["next_action_due"])
            if "lost_detail" in data:
                sets["lost_detail"] = _clean_text(data["lost_detail"], 1000, "失注の詳細")
            if "lost_reason" in data:
                lr = data["lost_reason"]
                if lr is not None and lr not in LOST_REASONS:
                    raise HTTPException(400, "失注理由が不正です")
                sets["lost_reason"] = lr

            new_stage = data.get("stage")
            if new_stage is not None and new_stage != cur["stage"]:
                change_stage(c, t, deal_id, cur["stage"], new_stage, sets,
                             lost_reason=sets.get("lost_reason", cur["lost_reason"]),
                             lost_detail=sets.get("lost_detail"))

            if not sets:
                return {"ok": True}
            apply_sets(c, t, deal_id, sets)
            c.commit()
        return {"ok": True}

    @r.delete("/deals/{deal_id}")
    def delete_deal(deal_id: int, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            cur = c.execute("DELETE FROM deals WHERE id=? AND tenant_id=?", (deal_id, t))
            c.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "案件が見つかりません")
        return {"ok": True}

    # ── メール作成 ──
    @r.post("/deals/{deal_id}/draft")
    def draft_email(deal_id: int, body: DraftRequest, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        by_id, by_email = card_index(t)
        with _conn() as c:
            _seed_purposes(c, t, tenant_name(t))
            row = _get_deal_row(c, t, deal_id)
            card = resolve(c, row, by_id, by_email) or {}
            p = c.execute("SELECT * FROM mail_purposes WHERE id=? AND tenant_id=?",
                          (body.purpose_id, t)).fetchone()
            if not p:
                raise HTTPException(404, "用途が見つかりません")
            acts = deal_acts(c, t, deal_id)
            c.commit()
        ev_date = row["event_date"]
        values = {
            "会社": card.get("company") or row["company_snapshot"] or "",
            "名前": card.get("name") or row["contact_snapshot"] or "",
            "役職": card.get("department_position") or "",
            "接点": row["event_name"] or "名刺交換の場",
            "接点日": f"{int(ev_date[5:7])}月{int(ev_date[8:10])}日" if ev_date else "",
            "案件名": row["title"],
            "自社": tenant_name(t),
        }
        to = card.get("email") or row["email_snapshot"] or ""
        if body.mode == "ai":
            if not _ai_available():
                raise HTTPException(400, "AIが設定されていません（ANTHROPIC_API_KEY）")
            out = _ai_draft(values, dict(p), acts, _clean_text(body.note, 500, "追加の指示"))
            return {"to": to, "subject": out["subject"], "body": out["body"], "source": "ai"}
        return {"to": to,
                "subject": render_template(p["subject_tpl"], values),
                "body": render_template(p["body_tpl"], values),
                "source": "template"}

    @r.post("/deals/{deal_id}/emails")
    def log_email(deal_id: int, body: EmailLog, session: dict = Depends(check_session)):
        """メーラーで送ったメールを「送信済み」として記録し、返信待ちにする"""
        t = session["tenant_id"]
        subject = _clean_text(body.subject, 300, "件名")
        text = _clean_text(body.body, 20000, "本文")
        if not subject and not text:
            raise HTTPException(400, "件名か本文を入力してください")
        with _conn() as c:
            cur = dict(_get_deal_row(c, t, deal_id))
            if cur["stage"] in CLOSED_STAGES:
                raise HTTPException(400, "完了した案件にはメールを記録できません")
            settings = _get_settings(c, t)
            log_activity(c, t, deal_id, "email", subject or "(件名なし)", text, awaiting_reply=True)
            sets = {}
            if body.advance_stage and body.advance_stage != cur["stage"]:
                if body.advance_stage in CLOSED_STAGES:
                    raise HTTPException(400, "このステージには進められません")
                change_stage(c, t, deal_id, cur["stage"], body.advance_stage, sets)
            if body.set_followup:
                sets["next_action"] = FOLLOWUP_ACTION
                sets["next_action_due"] = add_business_days(date.today(), settings["reply_wait_days"]).isoformat()
            apply_sets(c, t, deal_id, sets)
            c.commit()
        return {"ok": True}

    @r.post("/activities/{activity_id}/replied")
    def mark_replied(activity_id: int, body: Optional[RepliedBody] = None,
                     session: dict = Depends(check_session)):
        """返信あり → 返信待ちアラートを止める"""
        t = session["tenant_id"]
        with _conn() as c:
            a = c.execute("SELECT * FROM activities WHERE id=? AND tenant_id=? AND kind='email'",
                          (activity_id, t)).fetchone()
            if not a:
                raise HTTPException(404, "メールの記録が見つかりません")
            if a["replied_at"]:
                return {"ok": True}
            c.execute("UPDATE activities SET replied_at=? WHERE id=?", (_now(), activity_id))
            note = _clean_text(body.note if body else None, 5000, "メモ")
            text = f"「{a['title']}」への返信を受け取りました"
            if note:
                text += "\n\n" + note
            log_activity(c, t, a["deal_id"], "memo", "返信あり", text)
            deal = dict(_get_deal_row(c, t, a["deal_id"]))
            sets = {}
            if deal["next_action"] == FOLLOWUP_ACTION:
                sets["next_action"] = None
                sets["next_action_due"] = None
            apply_sets(c, t, a["deal_id"], sets)
            c.commit()
        return {"ok": True}

    # ── 活動ログ ──
    @r.post("/deals/{deal_id}/activities")
    def add_activity(deal_id: int, body: ActivityCreate, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        if body.kind not in USER_ACTIVITY_KINDS:
            raise HTTPException(400, "種類が不正です")
        title = _clean_text(body.title, 200, "件名")
        text = _clean_text(body.body, 20000, "内容")
        if not title and not text:
            raise HTTPException(400, "件名か内容を入力してください")
        occurred = (body.occurred_at or "").strip() or _now()
        try:
            datetime.fromisoformat(occurred)
        except ValueError:
            raise HTTPException(400, "日時が不正です")
        with _conn() as c:
            _get_deal_row(c, t, deal_id)
            log_activity(c, t, deal_id, body.kind, title, text, occurred,
                         awaiting_reply=(body.kind == "email" and body.awaiting_reply))
            sets = {}
            data = _dump(body)
            if "next_action" in data:
                sets["next_action"] = _clean_text(data["next_action"], 200, "次のアクション")
            if "next_action_due" in data:
                sets["next_action_due"] = _clean_date(data["next_action_due"])
            apply_sets(c, t, deal_id, sets)
            c.commit()
        return {"ok": True}

    @r.delete("/activities/{activity_id}")
    def delete_activity(activity_id: int, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            cur = c.execute("DELETE FROM activities WHERE id=? AND tenant_id=? AND kind != 'stage'",
                            (activity_id, t))
            c.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "記録が見つからないか削除できない記録です")
        return {"ok": True}

    # ── 集計 ──
    @r.get("/stats")
    def stats(session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            by_stage = {s: 0 for s in STAGES}
            for row in c.execute("SELECT stage, COUNT(*) n FROM deals WHERE tenant_id=? GROUP BY stage", (t,)):
                by_stage[row["stage"]] = row["n"]
            reasons = {lr: 0 for lr in LOST_REASONS}
            for row in c.execute("""SELECT lost_reason, COUNT(*) n FROM deals
                WHERE tenant_id=? AND stage='失注' GROUP BY lost_reason""", (t,)):
                if row["lost_reason"]:
                    reasons[row["lost_reason"]] = row["n"]
            won_amount = c.execute("""SELECT COALESCE(SUM(amount),0) s FROM deals
                WHERE tenant_id=? AND stage='受注'""", (t,)).fetchone()["s"]
        won, lost = by_stage["受注"], by_stage["失注"]
        return {
            "by_stage": by_stage,
            "won": won,
            "lost": lost,
            "win_rate": round(won / (won + lost), 3) if (won + lost) else None,
            "lost_reasons": reasons,
            "won_amount": won_amount,
        }

    return r
