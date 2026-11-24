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
import csv
import io
import json
import os
import secrets
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

import profiles

SALES_DB_PATH = os.getenv("SALES_DB_PATH", "/var/meishi/backend/sales.db")

STAGES = ["テレアポ", "名刺交換", "御礼済", "アポ", "ヒアリング", "見積提出", "受注", "失注", "保留"]
BP_STAGES = ["最初の打ち合わせ", "日程調整", "質問事項送付", "取材", "初稿提出", "アップロード完了", "見送り"]
# 案件の種類（パイプライン）。ブループリントはワコーメタルだけ
PIPELINES = {
    "sales": {"key": "sales", "label": "営業", "stages": STAGES, "first": "名刺交換",
              "won": "受注", "lost": "失注", "reason_required": True},
    "bp": {"key": "bp", "label": "ブループリント", "stages": BP_STAGES, "first": "最初の打ち合わせ",
           "won": "アップロード完了", "lost": "見送り", "reason_required": False, "only_tenant": "ワコーメタル"},
}
# 確認項目（お客様を主語にした「案件が進むための条件」）。from: この段階に入ったら確認しておきたい
PIPELINES["sales"]["checks"] = [
    {"key": "need", "from": "アポ", "label": "お客様が課題・必要性を認識している",
     "ask": "今回なぜ外注・発注を検討しているのか（困りごと・きっかけ）"},
    {"key": "spec", "from": "ヒアリング", "label": "図面・仕様・数量・材質がそろっている",
     "ask": "図面（または手書き）・数量・材質・板厚・塗装や表面処理の有無"},
    {"key": "timeline", "from": "ヒアリング", "label": "いつまでに必要か（納期・発注時期）がわかっている",
     "ask": "希望納期と、発注を決める時期"},
    {"key": "budget", "from": "ヒアリング", "label": "予算感がわかっている",
     "ask": "想定している予算・現在の調達単価"},
    {"key": "authority", "from": "ヒアリング", "label": "決め方（決裁者・稟議）がわかっている",
     "ask": "最終的に誰がどう決めるか（設計・購買・上長のどこで決まるか）"},
    {"key": "competitor", "from": "見積提出", "label": "相見積もり・競合の有無がわかっている",
     "ask": "他社にも見積を依頼しているか・今の仕入先はどこか"},
    {"key": "reason", "from": "見積提出", "label": "当社を選ぶ理由にお客様が納得している",
     "ask": "当社に頼む決め手（品質・納期・大型対応・一貫対応など）"},
    {"key": "keyman", "from": "見積提出", "label": "キーマン（決裁者）が当社案に前向き",
     "ask": "決裁者の反応・懸念点"},
    {"key": "price_ok", "from": "見積提出", "label": "お客様が金額に納得している",
     "ask": "金額は予算内か・高いなら競合比か予算比か"},
    {"key": "next_step", "from": "名刺交換", "label": "次の進め方・日程をお客様と合意している",
     "ask": "次にいつ・何をするか（お客様側の宿題も）"},
]
PIPELINES["bp"]["checks"] = [
    {"key": "theme", "from": "最初の打ち合わせ", "label": "取材テーマ・切り口が決まっている",
     "ask": "どんな技術・人・ものづくりを紹介するか"},
    {"key": "interviewee", "from": "日程調整", "label": "話してくれる人が決まっている",
     "ask": "取材に応じる方の名前・役職"},
    {"key": "date", "from": "日程調整", "label": "取材日時・場所が確定している", "ask": "日時・場所・所要時間"},
    {"key": "photo", "from": "質問事項送付", "label": "撮影の可否・NG箇所がわかっている",
     "ask": "工場内・製品・人物の撮影可否"},
    {"key": "questions", "from": "質問事項送付", "label": "質問事項を先方が確認済み", "ask": "質問への事前回答や追加の希望"},
    {"key": "review_flow", "from": "取材", "label": "初稿の確認者と期限が決まっている", "ask": "誰がいつまでに確認するか"},
    {"key": "publish_ok", "from": "初稿提出", "label": "掲載内容・公開日の最終OKが出ている", "ask": "修正点と公開してよい日"},
]
# 段階ごとの確度（%）。見込み額 = 金額 × 確度
PIPELINES["sales"]["prob"] = {"テレアポ": 3, "名刺交換": 5, "御礼済": 10, "アポ": 20, "ヒアリング": 35, "見積提出": 60,
                              "受注": 100, "失注": 0, "保留": 10}
PIPELINES["bp"]["prob"] = {s: 0 for s in BP_STAGES}
# 一直線に進む段階（段階の後退を見るため）
PIPELINES["sales"]["linear"] = ["テレアポ", "名刺交換", "御礼済", "アポ", "ヒアリング", "見積提出", "受注"]
PIPELINES["bp"]["linear"] = BP_STAGES[:6]
# 受注後の進行（承認図〜据付・試運転）。和宏電機だけ
PIPELINES["prod"] = {"key": "prod", "label": "受注後の進行", "stages": profiles.PROD_STAGES, "first": "承認図作成",
                     "won": "完了", "lost": "中止", "reason_required": False, "only_tenant": "和宏電機",
                     "checks": profiles.PROD_CHECKS, "prob": {s: 0 for s in profiles.PROD_STAGES},
                     "linear": profiles.PROD_STAGES[:-1], "guide": profiles.PROD_GUIDE}
# 和宏電機の営業（段階は同じ。確認項目とやることを盤向けに）
PANEL_SALES = dict(PIPELINES["sales"], checks=profiles.PANEL_CHECKS, guide=profiles.PANEL_GUIDE)
LOST_TYPES = {"competitor": "競合に負けた", "no_decision": "お客様が何も買わなかった（見送り・現状維持）"}
REAPPROACH_DAYS = {"competitor": 365, "no_decision": 90}

CLOSED_STAGES = {p[k] for p in PIPELINES.values() for k in ("won", "lost")}
ALL_STAGES = [s for p in PIPELINES.values() for s in p["stages"]]
# 種類を移すときのステージの読み替え
STAGE_MAP = {
    ("sales", "bp"): {"テレアポ": "最初の打ち合わせ", "名刺交換": "最初の打ち合わせ", "御礼済": "最初の打ち合わせ", "アポ": "日程調整",
                      "ヒアリング": "日程調整", "見積提出": "日程調整", "保留": "日程調整",
                      "受注": "アップロード完了", "失注": "見送り"},
    ("bp", "sales"): {"最初の打ち合わせ": "名刺交換", "日程調整": "アポ", "質問事項送付": "アポ", "取材": "アポ",
                      "初稿提出": "アポ", "アップロード完了": "受注", "失注": "失注", "見送り": "失注"},
    ("sales", "prod"): {**{s: "承認図作成" for s in STAGES}, "失注": "中止"},
    ("prod", "sales"): {**{s: "受注" for s in profiles.PROD_STAGES}, "中止": "失注"},
}
BP_WORDS = re.compile(r"ブループリント|ブルー\s*プリント|blue\s*print|BP取材", re.I)


def pipeline_of(deal_or_key, tenant_display: Optional[str] = None) -> dict:
    """案件（または種類のキー）の定義。案件を渡すと会社に合わせた定義（和宏電機なら盤向けの確認項目）を返す"""
    tid = None
    if isinstance(deal_or_key, str):
        key = deal_or_key
    else:
        key = deal_or_key.get("pipeline") if hasattr(deal_or_key, "get") else deal_or_key["pipeline"]
        try:
            tid = deal_or_key["tenant_id"]
        except (KeyError, IndexError, TypeError):
            tid = None
    p = PIPELINES.get(key or "sales", PIPELINES["sales"])
    if p["key"] == "sales":
        disp = tenant_display if tenant_display is not None else display_of(tid)
        if profile_key(disp) == "panel":
            return PANEL_SALES
    return p


# ── 会社ごとの業種（板金のワコーメタル／盤の和宏電機） ──
_TENANT_FN: Optional[Callable] = None
_DISPLAY_CACHE: dict = {}


def set_tenant_resolver(fn: Optional[Callable]):
    global _TENANT_FN
    _TENANT_FN = fn
    _DISPLAY_CACHE.clear()


def display_of(tenant_id: Optional[str]) -> str:
    """テナントの表示名（60秒キャッシュ）"""
    if not tenant_id or not _TENANT_FN:
        return ""
    hit = _DISPLAY_CACHE.get(tenant_id)
    now = datetime.now().timestamp()
    if hit and now - hit[1] < 60:
        return hit[0]
    try:
        name = (_TENANT_FN(tenant_id) or {}).get("display_name") or ""
    except Exception:
        name = ""
    _DISPLAY_CACHE[tenant_id] = (name, now)
    return name


_GROUP_FN: Optional[Callable] = None   # () -> {tenant_id: {display_name, ...}}（グループ会社への依頼に使う）


def group_links() -> dict:
    """依頼元テナントID → 依頼先テナントID。.env の SALES_GROUP_LINKS=company_b:company_a で決める（既定も同じ）"""
    raw = os.getenv("SALES_GROUP_LINKS", "company_b:company_a")
    out = {}
    for pair in raw.split(","):
        if ":" in pair:
            a, b = (x.strip() for x in pair.split(":", 1))
            if a and b and a != b:
                out[a] = b
    return out


def group_target_of(source_tid: Optional[str]) -> Optional[str]:
    """和宏電機から筐体の板金を頼む先（ワコーメタル）のテナントID。設定で決めたペアで、表示名も合うときだけ"""
    if not _GROUP_FN or not source_tid:
        return None
    target = group_links().get(source_tid)
    if not target:
        return None
    try:
        ts = _GROUP_FN() or {}
    except Exception:
        return None
    src = (ts.get(source_tid) or {}).get("display_name") or ""
    dst = (ts.get(target) or {}).get("display_name") or ""
    if profile_key(src) == "panel" and "ワコーメタル" in dst and profile_key(dst) == "sheet":
        return target
    return None


def profile_key(tenant_display: str) -> str:
    return "panel" if any(w in (tenant_display or "") for w in NOT_WAKOMETAL) else "sheet"


def profile_meta(tenant_display: str) -> dict:
    k = profile_key(tenant_display)
    pf = profiles.PROFILE_FIELDS[k]
    return {"key": k, "items": profiles.ITEM_LABELS[k], "deal_fields": True, "fields": pf["fields"],
            "labels": pf["labels"], "segments": pf["segments"], "channels": pf["channels"],
            "group_target": False}


# 表示名にこれが入っている会社には、ワコーメタル向けの機能（ブループリントなど）を出さない
NOT_WAKOMETAL = ("和宏電機", "和宏 電機")


def tenant_allows(only: Optional[str], tenant_display: str) -> bool:
    """only_tenant の付いた機能をこの会社で使えるか"""
    if not only:
        return True
    d = tenant_display or ""
    if only == "ワコーメタル" and any(w in d for w in NOT_WAKOMETAL):
        return False
    if only == "和宏電機":
        return any(w in d for w in NOT_WAKOMETAL)
    return only in d


def has_bp(tenant_display: str) -> bool:
    return tenant_allows(PIPELINES["bp"].get("only_tenant"), tenant_display)


def pipelines_for(tenant_display: str) -> list:
    out = []
    for p in PIPELINES.values():
        if not tenant_allows(p.get("only_tenant"), tenant_display):
            continue
        out.append(pipeline_of(p["key"], tenant_display) if p["key"] == "sales" else p)
    return out
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
DEFAULT_SETTINGS = {"reply_wait_days": 3, "stale_days": 14, "stall_days": 30, "monthly_target": None,
                    "strictness": "gentle"}
STRICTNESS = {"gentle": "ゆるめ（少人数の会社向け・既定）", "normal": "ふつう", "strict": "きびしめ"}

TEMPLATE_VARS = ["会社", "名前", "役職", "接点", "接点日", "案件名", "自社", "見積明細", "現場名", "納期"]

DEFAULT_PURPOSES_BASE = [
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
# 和宏電機（盤）向けの雛形は、既存の会社にも1回だけ追加する（消したら戻さない）
DEFAULT_PURPOSES = DEFAULT_PURPOSES_BASE + [dict(x, only_tenant="和宏電機", seed_once=True) for x in profiles.PANEL_PURPOSES]


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
        pending = _PENDING_HOOKS.pop(id(conn), [])
        try:
            if pending and not conn.in_transaction:
                _fire_hooks(conn, pending)   # 保存が確定した段階変更だけ知らせる
        finally:
            conn.close()


_PENDING_HOOKS: dict = {}


def _fire_hooks(conn, pending: list):
    for tenant_id, deal_id, old_stage, new_stage in pending:
        row = conn.execute("""SELECT id, title, company_snapshot, contact_snapshot, amount, owner, pipeline, stage
            FROM deals WHERE id=? AND tenant_id=?""", (deal_id, tenant_id)).fetchone()
        if not row or row["stage"] != new_stage:
            continue   # 取り消された・巻き戻された
        p = pipeline_of(row)
        info = dict(row)
        info.update(tenant_id=tenant_id, old_stage=old_stage, new_stage=new_stage,
                    won=new_stage == p["won"], lost=new_stage == p["lost"])
        for hook in STAGE_HOOKS:
            try:
                hook(info)
            except Exception:
                pass


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
        -- 外した名刺のつながり（自動でつなぎ直さない）
        CREATE TABLE IF NOT EXISTS card_link_skip (
            tenant_id TEXT NOT NULL,
            deal_id INTEGER NOT NULL,
            card_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (tenant_id, deal_id, card_id)
        );
        """)
        # Phase 2a マイグレーション
        _add_column(c, "activities", "awaiting_reply", "awaiting_reply INTEGER NOT NULL DEFAULT 0")
        _add_column(c, "activities", "replied_at", "replied_at TEXT")
        _add_column(c, "mail_purposes", "subject_tpl", "subject_tpl TEXT NOT NULL DEFAULT ''")
        _add_column(c, "mail_purposes", "body_tpl", "body_tpl TEXT NOT NULL DEFAULT ''")
        # Phase 4: 案件の種類（営業／ブループリント）
        _add_column(c, "deals", "pipeline", "pipeline TEXT NOT NULL DEFAULT 'sales'")
        # Phase 5: 確認項目・受注予定日・失注の種類・感想戦・名刺なし案件の連絡先
        for col, ddl in (("expected_close", "expected_close TEXT"),
                         ("close_changes", "close_changes INTEGER NOT NULL DEFAULT 0"),
                         ("stage_changed_at", "stage_changed_at TEXT"),
                         ("max_stage_idx", "max_stage_idx INTEGER NOT NULL DEFAULT 0"),
                         ("lost_type", "lost_type TEXT"),
                         ("lost_from_stage", "lost_from_stage TEXT"),
                         ("reapproach_on", "reapproach_on TEXT"),
                         ("review", "review TEXT"),
                         ("checks", "checks TEXT"),
                         ("phone_snapshot", "phone_snapshot TEXT")):
            _add_column(c, "deals", col, ddl)
        c.execute("UPDATE deals SET stage_changed_at=COALESCE(updated_at, created_at) WHERE stage_changed_at IS NULL")
        # 既存の案件に「これまで進んだ一番先の段階」を入れる（1回だけ）
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='migr_max_stage'").fetchone():
            for key, pl in PIPELINES.items():
                for i, st in enumerate(pl["linear"]):
                    c.execute("UPDATE deals SET max_stage_idx=? WHERE pipeline=? AND stage=? AND max_stage_idx=0",
                              (i, key, st))
            c.execute("CREATE TABLE migr_max_stage (done INTEGER)")
            c.execute("CREATE TABLE IF NOT EXISTS migr_telapo_idx (done INTEGER)")   # いまの段階で計算済み
        # 段階の先頭に「テレアポ」を足したので、営業の案件の「一番先の段階」の番号を1つずらす（1回だけ）
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='migr_telapo_idx'").fetchone():
            c.execute("UPDATE deals SET max_stage_idx=max_stage_idx+1 WHERE COALESCE(pipeline,'sales')='sales'")
            c.execute("CREATE TABLE migr_telapo_idx (done INTEGER)")
        _add_column(c, "sales_settings", "stall_days", "stall_days INTEGER NOT NULL DEFAULT 30")
        _add_column(c, "sales_settings", "monthly_target", "monthly_target INTEGER")
        # Phase 6: 指摘の強さ・リピート・見積明細
        _add_column(c, "sales_settings", "strictness", "strictness TEXT NOT NULL DEFAULT 'gentle'")
        _add_column(c, "deals", "repeat_on", "repeat_on TEXT")
        _add_column(c, "deals", "repeat_deal_id", "repeat_deal_id INTEGER")
        # Phase 7: 担当者・連携
        _add_column(c, "deals", "owner", "owner TEXT")          # NULL=未設定（名刺の所有者から自動で入れる） ''=担当なし
        _add_column(c, "sales_settings", "company_info", "company_info TEXT")
        _add_column(c, "sales_settings", "notify_won", "notify_won INTEGER NOT NULL DEFAULT 1")
        _add_column(c, "sales_settings", "ics_token", "ics_token TEXT")
        # Phase 8: 盤の営業（分野・商流・現場・納期）と受注後の進行
        for col, ddl in (("segment", "segment TEXT"), ("channel", "channel TEXT"), ("site_name", "site_name TEXT"),
                         ("end_user", "end_user TEXT"), ("delivery_on", "delivery_on TEXT"),
                         ("prod_deal_id", "prod_deal_id INTEGER"), ("source_deal_id", "source_deal_id INTEGER"),
                         ("group_tenant", "group_tenant TEXT"), ("group_deal_id", "group_deal_id INTEGER"),
                         # Phase 10: テレアポ業者
                         ("vendor_id", "vendor_id INTEGER"), ("telapo_at", "telapo_at TEXT"), ("appo_at", "appo_at TEXT")):
            _add_column(c, "deals", col, ddl)
        _add_column(c, "activities", "vendor_id", "vendor_id INTEGER")              # 業者が書いた記録
        _add_column(c, "activities", "vendor_visible", "vendor_visible INTEGER NOT NULL DEFAULT 0")
        c.execute("CREATE INDEX IF NOT EXISTS idx_deals_vendor ON deals(tenant_id, vendor_id)")
        c.execute("""CREATE TABLE IF NOT EXISTS vendors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            name TEXT NOT NULL,
            slug TEXT NOT NULL UNIQUE,          -- 専用URLの合言葉
            pw_hash TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_login_at TEXT,
            UNIQUE (tenant_id, name)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS purpose_seeded (
            tenant_id TEXT NOT NULL, label TEXT NOT NULL, PRIMARY KEY (tenant_id, label))""")
        c.execute("""CREATE TABLE IF NOT EXISTS members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            name TEXT NOT NULL,
            slack_name TEXT,
            slack_id TEXT,
            email TEXT,
            ics_token TEXT,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            UNIQUE (tenant_id, name)
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_deals_owner ON deals(tenant_id, owner)")
        c.execute("""CREATE TABLE IF NOT EXISTS deal_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            deal_id INTEGER NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            material TEXT,
            thickness TEXT,
            qty INTEGER,
            unit_price INTEGER,
            note TEXT,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_items_deal ON deal_items(tenant_id, deal_id, sort_order)")
        c.execute("""CREATE TABLE IF NOT EXISTS deal_advice (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            deal_id INTEGER,              -- NULL ならチーム全体への総評
            created_at TEXT NOT NULL,
            body TEXT NOT NULL            -- JSON
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_advice ON deal_advice(tenant_id, deal_id, id)")
        c.commit()


# ── 入力モデル ──
class EventCreate(BaseModel):
    name: str
    event_date: Optional[str] = None
    place: Optional[str] = None


class DealCreate(BaseModel):
    card_id: Optional[str] = None      # 空なら名刺なしの案件（下の控えを使う）
    owner: Optional[str] = None        # 自社の担当者（メンバー名）
    pipeline: str = "sales"
    company: Optional[str] = None
    contact: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    expected_close: Optional[str] = None
    title: Optional[str] = None
    stage: Optional[str] = None
    amount: Optional[int] = None
    event_id: Optional[int] = None
    next_action: Optional[str] = None
    next_action_due: Optional[str] = None
    segment: Optional[str] = None
    channel: Optional[str] = None
    site_name: Optional[str] = None
    end_user: Optional[str] = None
    delivery_on: Optional[str] = None


class DealMove(BaseModel):
    ids: list[int]
    pipeline: str


class BulkStageIn(BaseModel):
    ids: list[int]
    stage: Optional[str] = None
    stages: Optional[dict[str, str]] = None   # 元に戻すとき: {id: 前の段階}


class DealUpdate(BaseModel):
    pipeline: Optional[str] = None
    owner: Optional[str] = None         # "" で担当なし
    card_id: Optional[str] = None       # 名刺をつなぐ／付け替える（"" で外す）
    company_snapshot: Optional[str] = None
    contact_snapshot: Optional[str] = None
    email_snapshot: Optional[str] = None
    phone_snapshot: Optional[str] = None
    expected_close: Optional[str] = None
    lost_type: Optional[str] = None
    reapproach_on: Optional[str] = None
    review: Optional[str] = None
    checks: Optional[dict] = None
    repeat_on: Optional[str] = None
    telapo_at: Optional[str] = None     # 架電日時（テレアポ）
    appo_at: Optional[str] = None       # アポ日時
    segment: Optional[str] = None
    channel: Optional[str] = None
    site_name: Optional[str] = None
    end_user: Optional[str] = None
    delivery_on: Optional[str] = None
    undo_max_stage_idx: Optional[int] = None   # 「元に戻す」で段階を戻したとき、後退扱いにしない
    title: Optional[str] = None
    stage: Optional[str] = None
    amount: Optional[int] = None
    event_id: Optional[int] = None
    lost_reason: Optional[str] = None
    lost_detail: Optional[str] = None
    next_action: Optional[str] = None
    next_action_due: Optional[str] = None


class MemberIn(BaseModel):
    name: str
    slack_name: Optional[str] = None
    email: Optional[str] = None


class SettingsExtra(BaseModel):
    company_info: Optional[str] = None
    notify_won: Optional[bool] = None


class ItemIn(BaseModel):
    name: str
    material: Optional[str] = None
    thickness: Optional[str] = None
    qty: Optional[int] = None
    unit_price: Optional[int] = None
    note: Optional[str] = None


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
    vendor_visible: Optional[bool] = None   # テレアポ業者にも見せる（業者の案件だけ）


class ActivityVisibility(BaseModel):
    vendor_visible: bool


class SettingsUpdate(BaseModel):
    reply_wait_days: int
    stale_days: int
    stall_days: Optional[int] = None
    monthly_target: Optional[int] = None
    strictness: Optional[str] = None


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


def _jp_date(v: Optional[str]) -> str:
    try:
        d = date.fromisoformat((v or "")[:10])
        return f"{d.month}月{d.day}日"
    except ValueError:
        return ""


def norm_site(v) -> str:
    """現場名の比較用（空白・記号・「現場」「新築工事」などの語尾を落とす）"""
    n = unicodedata.normalize("NFKC", v or "").lower()
    n = re.sub(r"[\s()（）・.,、。「」『』\-ー－_/]", "", n)
    n = re.sub(r"(新築|増築|改修|更新)?(工事|現場|物件)$", "", n)
    return n if len(n) >= 3 else ""


def _clean_dt(v: Optional[str], label: str = "日時") -> Optional[str]:
    """日時（YYYY-MM-DDTHH:MM）。日付だけでもよい"""
    v = (v or "").strip()
    if not v:
        return None
    try:
        if len(v) == 10:
            return date.fromisoformat(v).isoformat()
        return datetime.fromisoformat(v[:16]).isoformat(timespec="minutes")
    except ValueError:
        raise HTTPException(400, f"{label}の形式が不正です")


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
        lvl = "info" if settings.get("strictness", "gentle") == "gentle" else "mid"
        alerts.append({"type": "stale", "level": lvl, "message": f"{idle}日間 記録がありません"})
    return alerts


def render_template(tpl: str, values: dict) -> str:
    out = tpl or ""
    for k in TEMPLATE_VARS:
        out = out.replace("{{" + k + "}}", values.get(k) or "")
    return out


def _card_summary(card: Optional[dict]) -> Optional[dict]:
    if not card:
        return None
    keys = ("id", "name", "company", "department_position", "email", "mobile", "phone", "has_image", "has_back_image",
            "website", "address")
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
    row = c.execute("""SELECT reply_wait_days, stale_days, stall_days, monthly_target, strictness
        FROM sales_settings WHERE tenant_id=?""", (tenant_id,)).fetchone()
    return dict(row) if row else dict(DEFAULT_SETTINGS)


def _seed_once(c, tenant_id: str, tenant_display: str):
    """あとから増えた雛形（seed_once）を、その会社に1回だけ追加する"""
    for p in DEFAULT_PURPOSES:
        if not p.get("seed_once") or not tenant_allows(p.get("only_tenant"), tenant_display):
            continue
        if c.execute("SELECT 1 FROM purpose_seeded WHERE tenant_id=? AND label=?", (tenant_id, p["label"])).fetchone():
            continue
        if not c.execute("SELECT 1 FROM mail_purposes WHERE tenant_id=? AND label=?", (tenant_id, p["label"])).fetchone():
            n = c.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 n FROM mail_purposes WHERE tenant_id=?", (tenant_id,)).fetchone()["n"]
            c.execute("""INSERT INTO mail_purposes (tenant_id, label, instruction, subject_tpl, body_tpl, sort_order)
                VALUES (?,?,?,?,?,?)""", (tenant_id, p["label"], p["instruction"], p["subject_tpl"], p["body_tpl"], n))
        c.execute("INSERT OR IGNORE INTO purpose_seeded (tenant_id, label) VALUES (?,?)", (tenant_id, p["label"]))


def _seed_purposes(c, tenant_id: str, tenant_display: str = ""):
    n = c.execute("SELECT COUNT(*) n FROM mail_purposes WHERE tenant_id=?", (tenant_id,)).fetchone()["n"]
    if n:
        _seed_once(c, tenant_id, tenant_display)
        # 対象外のテナントに未編集の初期用途が入っていたら外す（Blue Print など）
        for p in DEFAULT_PURPOSES:
            if p.get("seed_once"):
                continue   # 一般的な名前なので、その会社が自分で作った雛形を消さない
            if not tenant_allows(p.get("only_tenant"), tenant_display):
                c.execute("""DELETE FROM mail_purposes WHERE tenant_id=? AND label=?""",
                          (tenant_id, p["label"]))
        c.commit()
        return
    i = 0
    for p in DEFAULT_PURPOSES:
        if not tenant_allows(p.get("only_tenant"), tenant_display):
            continue
        c.execute("""INSERT INTO mail_purposes (tenant_id, label, instruction, subject_tpl, body_tpl, sort_order)
            VALUES (?,?,?,?,?,?)""", (tenant_id, p["label"], p["instruction"], p["subject_tpl"], p["body_tpl"], i))
        if p.get("seed_once"):
            c.execute("INSERT OR IGNORE INTO purpose_seeded (tenant_id, label) VALUES (?,?)", (tenant_id, p["label"]))
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
    d["pipeline"] = d.get("pipeline") or "sales"
    d["card"] = _card_summary(card)
    d["no_card"] = card is None and not d.get("card_id")
    d["card_missing"] = card is None and not d["no_card"]
    d["last_activity_at"] = last_activity_at
    d["alerts"] = alerts or []
    d["needs_action"] = any(a["level"] != "info" for a in d["alerts"])
    p = pipeline_of(d)
    d["checks"] = parse_checks(d.get("checks"))
    d["prob"] = p["prob"].get(d["stage"], 0)
    d["weighted"] = round((d.get("amount") or 0) * d["prob"] / 100) if d.get("amount") else 0
    since = (d.get("stage_changed_at") or d.get("created_at") or "")[:10]
    try:
        d["days_in_stage"] = (date.today() - date.fromisoformat(since)).days if since else 0
    except ValueError:
        d["days_in_stage"] = 0
    order = p["stages"]
    cur_idx = order.index(d["stage"]) if d["stage"] in order else 0
    due_checks = [ck for ck in p["checks"] if ck["from"] in order and order.index(ck["from"]) <= cur_idx]
    d["checks_missing"] = [ck["key"] for ck in due_checks if not d["checks"].get(ck["key"], {}).get("done")] if is_open else []
    d["checks_due"] = len(due_checks)
    lin = p["linear"]
    d["stage_down"] = bool(is_open and d["stage"] in lin and lin.index(d["stage"]) < (d.get("max_stage_idx") or 0))
    return d


def _norm_company(name) -> str:
    n = unicodedata.normalize("NFKC", name or "")
    n = re.sub(r"(株式会社|有限会社|合同会社|\(株\)|\(有\)|㈱|㈲)", "", n)
    return re.sub(r"[\s()（）・.,]", "", n).lower()


def _norm_name(name) -> str:
    return re.sub(r"[\s・.]|さん$|様$", "", unicodedata.normalize("NFKC", name or "")).lower()


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

def change_stage(c, tenant_id, deal_id, cur_stage, new_stage, sets, lost_reason=None, lost_detail=None,
                 pipeline=None):
    if pipeline is None:
        row = c.execute("SELECT pipeline FROM deals WHERE id=?", (deal_id,)).fetchone()
        pipeline = row["pipeline"] if row else "sales"
    p = pipeline_of(pipeline)
    if new_stage not in p["stages"]:
        raise HTTPException(400, f"{p['label']}のステージではありません: {new_stage}")
    if new_stage == p["lost"]:
        # 理由はあとからでもよい（ボタンひとつで失注にできるように）
        if lost_reason:
            sets["lost_reason"] = lost_reason
    else:
        sets["lost_reason"] = None
        sets["lost_detail"] = None
    if new_stage in CLOSED_STAGES:
        sets["closed_at"] = _now()
        sets["next_action"] = None
        sets["next_action_due"] = None
    else:
        sets["closed_at"] = None
    if new_stage == p["lost"]:
        sets["lost_from_stage"] = cur_stage
    sets["stage"] = new_stage
    sets["stage_changed_at"] = _now()
    if new_stage in p["linear"]:
        if "max_stage_idx" in sets:
            prev_max = sets["max_stage_idx"]  # 同じリクエストで種類を変えたとき
        else:
            row = c.execute("SELECT max_stage_idx FROM deals WHERE id=?", (deal_id,)).fetchone()
            prev_max = row["max_stage_idx"] if row else 0
        sets["max_stage_idx"] = max(prev_max, p["linear"].index(new_stage))
    note = f"{cur_stage} → {new_stage}"
    if new_stage == p["lost"] and lost_reason:
        note += f"（{lost_reason}）"
    log_activity(c, tenant_id, deal_id, "stage", note,
                 body=lost_detail if new_stage == p["lost"] else None)
    if STAGE_HOOKS:
        _PENDING_HOOKS.setdefault(id(c), []).append((tenant_id, deal_id, cur_stage, new_stage))

def apply_sets(c, tenant_id, deal_id, sets):
    sets["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in sets)
    c.execute(f"UPDATE deals SET {cols} WHERE id=? AND tenant_id=?",
              list(sets.values()) + [deal_id, tenant_id])


EXTRA_FIELDS = ("segment", "channel", "site_name", "end_user", "delivery_on")


def clean_extra(data: dict, prof: str = "panel") -> dict:
    """会社ごとの案件の項目（分野/業種・商流/経路・現場名・エンドユーザー・納期）。届いた分だけ返す"""
    pf = profiles.PROFILE_FIELDS[prof]
    out = {}
    for f in EXTRA_FIELDS:
        if f not in data or f not in pf["fields"]:
            continue
        v = data[f]
        if f == "delivery_on":
            out[f] = _clean_date(v)
        elif f in ("segment", "channel"):
            label = pf["labels"][f]
            v = _clean_text(v, 40, label)
            choices = pf["segments"] if f == "segment" else pf["channels"]
            if v and v not in choices:
                raise HTTPException(400, f"{label}が不正です: {v}")
            out[f] = v
        else:
            out[f] = _clean_text(v, 120, "現場名" if f == "site_name" else "エンドユーザー")
    return out


def insert_deal(c, tenant_id, card, title=None, stage=None, amount=None, event_id=None,
                next_action=None, next_action_due=None, created_note=None, pipeline="sales",
                expected_close=None, owner=None, extra: Optional[dict] = None) -> int:
    """案件を1件作る（画面・取り込み・Slack 共通）。card が None なら名刺なしの案件（会社名・氏名の控えだけ）"""
    if pipeline not in PIPELINES:
        raise HTTPException(400, "案件の種類が不正です")
    p = PIPELINES[pipeline]
    stage = stage or p["first"]
    if stage not in p["stages"] or stage in CLOSED_STAGES:
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
        (tenant_id, card_id, company_snapshot, contact_snapshot, email_snapshot, phone_snapshot,
         event_id, title, stage, amount, next_action, next_action_due,
         created_at, updated_at, pipeline, expected_close, stage_changed_at, max_stage_idx, owner)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (tenant_id, card.get("id") or "", card.get("company"), card.get("name"), card.get("email"),
         card.get("mobile") or card.get("phone"),
         event_id, title, stage, amount,
         _clean_text(next_action, 200, "次のアクション"),
         _clean_date(next_action_due), now, now, pipeline, _clean_date(expected_close), now,
         p["linear"].index(stage) if stage in p["linear"] else 0,
         owner if owner is not None else default_owner(c, tenant_id, card)))
    deal_id = cur.lastrowid
    ex = clean_extra(extra or {}, profile_key(display_of(tenant_id)))
    if (extra or {}).get("source_deal_id"):
        ex["source_deal_id"] = int(extra["source_deal_id"])
    ex = {k: v for k, v in ex.items() if v is not None}
    if ex:
        cols = ", ".join(f"{k}=?" for k in ex)
        c.execute(f"UPDATE deals SET {cols} WHERE id=?", list(ex.values()) + [deal_id])
    log_activity(c, tenant_id, deal_id, "stage", created_note or f"案件作成（{stage}）")
    return deal_id


def deal_items(tenant_id: str, deal_id: int) -> list:
    with _conn() as c:
        rows = c.execute("""SELECT id, name, material, thickness, qty, unit_price, note FROM deal_items
            WHERE tenant_id=? AND deal_id=? ORDER BY sort_order, id""", (tenant_id, deal_id)).fetchall()
    out = []
    for r_ in rows:
        x = dict(r_)
        x["subtotal"] = (x["qty"] or 0) * (x["unit_price"] or 0) if x["qty"] and x["unit_price"] else None
        out.append(x)
    return out


def items_text(items: list, profile: str = "sheet") -> str:
    """メール本文に差し込む見積明細"""
    lines = []
    unit = profiles.ITEM_LABELS.get(profile, profiles.ITEM_LABELS["sheet"])["unit"]
    for it in items:
        th = it.get("thickness")
        th = (f"t{th}" if profile == "sheet" else th) if th else None
        spec = " ".join(x for x in (it.get("material"), th) if x)
        qty = f" {it['qty']}{unit}" if it.get("qty") else ""
        price = f" 単価{it['unit_price']:,}円" if it.get("unit_price") else ""
        lines.append(f"・{it['name']}{('（' + spec + '）') if spec else ''}{qty}{price}")
    return "\n".join(lines)


STAGE_HOOKS: list = []   # 段階が変わったときに呼ぶ（Slack の受注通知など）。info(dict) を受け取る


def vendor_names(c, tenant_id: str) -> dict:
    return {r["id"]: r["name"] for r in c.execute("SELECT id, name FROM vendors WHERE tenant_id=?", (tenant_id,))}


def member_names(c, tenant_id: str) -> list:
    return [r["name"] for r in c.execute("SELECT name FROM members WHERE tenant_id=? ORDER BY sort_order, id", (tenant_id,))]


def default_owner(c, tenant_id: str, card: dict):
    """名刺の所有者のうちメンバーに登録されている人。いなければ未設定（NULL）"""
    names = set(member_names(c, tenant_id))
    return next((x for x in card.get("assigned_to_list") or [] if x in names), None)


def check_owner(c, tenant_id: str, owner):
    if owner is None or owner == "":
        return owner
    owner = owner.strip()
    if owner not in member_names(c, tenant_id):
        raise HTTPException(400, f"担当者「{owner}」はメンバーに登録されていません（設定 → メンバー）")
    return owner


def parse_checks(raw) -> dict:
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def clean_checks(cur: dict, incoming: dict) -> dict:
    """確認項目を更新する。incoming は {key: {done, note}}。届いた分だけ上書き"""
    keys = {ck["key"] for ck in pipeline_of(cur)["checks"]}
    out = parse_checks(cur.get("checks"))
    for k, v in (incoming or {}).items():
        if k not in keys:
            raise HTTPException(400, f"確認項目が不正です: {k}")
        if v is None:
            out.pop(k, None)
            continue
        if not isinstance(v, dict):
            raise HTTPException(400, "確認項目の形式が不正です")
        note = _clean_text(v.get("note"), 300, "確認項目のメモ")
        out[k] = {"done": bool(v.get("done")), "note": note or "", "at": _now()}
    return out


def link_card(c, tenant_id, cur: dict, card_id: str, by_id: dict, sets: dict, how: str = ""):
    """案件に名刺をつなぐ（または外す）。控えも名刺の内容に合わせる"""
    if card_id == (cur.get("card_id") or ""):
        return
    if cur.get("card_id"):
        import cardmatch   # 外した（付け替えた）名刺は、自動でつなぎ直さない
        cardmatch.remember_skip(c, tenant_id, cur["id"], cur["card_id"])
    if not card_id:
        sets["card_id"] = ""
        log_activity(c, tenant_id, cur["id"], "stage", "名刺のつながりを外しました")
        return
    card = by_id.get(card_id)
    if not card:
        raise HTTPException(404, "名刺が見つかりません")
    sets.update({"card_id": card["id"], "company_snapshot": card.get("company"),
                 "contact_snapshot": card.get("name"), "email_snapshot": card.get("email"),
                 "phone_snapshot": card.get("mobile") or card.get("phone")})
    log_activity(c, tenant_id, cur["id"], "stage",
                 f"名刺をつなぎました{f'（{how}）' if how else ''}: {card.get('company') or ''} {card.get('name') or ''}")


def move_pipeline(c, tenant_id, cur: dict, new_pipeline: str, sets: dict, tenant_display: str = ""):
    """案件の種類を変える。ステージは対応表で読み替える"""
    if new_pipeline not in [p["key"] for p in pipelines_for(tenant_display)]:
        raise HTTPException(400, "案件の種類が不正です")
    old = cur.get("pipeline") or "sales"
    stage = STAGE_MAP.get((old, new_pipeline), {}).get(cur["stage"], PIPELINES[new_pipeline]["first"])
    sets["pipeline"] = new_pipeline
    sets["stage"] = stage
    sets["stage_changed_at"] = _now()
    lin = PIPELINES[new_pipeline]["linear"]
    sets["max_stage_idx"] = lin.index(stage) if stage in lin else 0
    if stage in CLOSED_STAGES:
        sets["closed_at"] = cur.get("closed_at") or _now()
        if stage == "失注" and not cur.get("lost_reason"):
            sets["lost_reason"] = "その他"
    else:
        sets["closed_at"] = None
    log_activity(c, tenant_id, cur["id"], "stage",
                 f"種類を変更: {PIPELINES[old]['label']} → {PIPELINES[new_pipeline]['label']}（{cur['stage']} → {stage}）")


class GroupRequest(BaseModel):
    note: Optional[str] = None


def group_link(c, tenant_id: str, row) -> Optional[dict]:
    """グループ会社の相手の案件（お互いに指し合っているときだけ。見せるのは件名と段階だけ）"""
    gt, gid = row["group_tenant"], row["group_deal_id"]
    if not gt or not gid:
        return None
    other = c.execute("SELECT id, title, stage, next_action_due, delivery_on, group_tenant, group_deal_id FROM deals WHERE id=? AND tenant_id=?",
                      (gid, gt)).fetchone()
    if not other or other["group_tenant"] != tenant_id or other["group_deal_id"] != row["id"]:
        return None
    return {"tenant": display_of(gt) or gt, "id": other["id"], "title": other["title"], "stage": other["stage"],
            "next_action_due": other["next_action_due"], "delivery_on": other["delivery_on"]}


# ── 新着 ──
NEWS_SKIP_TITLES = ("AIの事前調査", "リストでの判定")
_REG_DATE_RE = re.compile(r"(\d{4})[/\-.年](\d{1,2})[/\-.月](\d{1,2})日?(?:[ T]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?")
SLACK_MARK = "— Slackの投稿 —"


def parse_reg_date(s) -> Optional[datetime]:
    """名刺の登録日（シートのI列。2026/11/24 11:02 など）を読む。読めなければ None"""
    m = _REG_DATE_RE.search(str(s or ""))
    if not m:
        return None
    y, mo, d, hh, mm, ss = m.groups()
    try:
        return datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), int(ss or 0))
    except ValueError:
        return None


def _news_snippet(body: Optional[str]) -> str:
    b = (body or "").strip()
    if SLACK_MARK in b:
        head = b.split(SLACK_MARK, 1)[0].strip()
        b = head or b.split(SLACK_MARK, 1)[1].strip()
    b = re.sub(r"\s+", " ", b)
    return b[:90] + ("…" if len(b) > 90 else "")


def _max_activity_id(tenant_id: str) -> int:
    with _conn() as c:
        return c.execute("SELECT COALESCE(MAX(id),0) m FROM activities WHERE tenant_id=?", (tenant_id,)).fetchone()["m"]


def news_items(c_cards: list, tenant_id: str, days: int = 7, after: int = 0) -> list:
    """新着の一覧（新しい順）。after を渡すとその番号より後の記録だけ（自分が書いた記録を既読にするため）"""
    days = max(1, min(int(days or 7), 30))
    since_dt = datetime.now() - timedelta(days=days)
    since = since_dt.isoformat(timespec="seconds")
    cards = {x["id"]: x for x in (c_cards or [])}
    items = []
    with _conn() as c:
        deals_by_card = {}
        for d in c.execute("SELECT id, card_id FROM deals WHERE tenant_id=? AND card_id<>''", (tenant_id,)):
            deals_by_card.setdefault(d["card_id"], []).append(d["id"])
        new_cards = set()
        if not after:
            for cid, x in cards.items():
                at = parse_reg_date(x.get("registered_date"))
                if not at or at < since_dt or at > datetime.now() + timedelta(days=1):
                    continue
                new_cards.add(cid)
                items.append({"key": f"c:{cid}", "type": "card", "at": at.isoformat(timespec="seconds"),
                              "card_id": cid, "company": x.get("company") or "", "name": x.get("name") or "",
                              "position": x.get("department_position") or "",
                              "owners": x.get("assigned_to_list") or [],
                              "deal_ids": sorted(deals_by_card.get(cid, []), reverse=True)})
        vnames = {v["id"]: v["name"] for v in c.execute("SELECT id, name FROM vendors WHERE tenant_id=?", (tenant_id,))} \
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='vendors'").fetchone() else {}
        rows = c.execute("""SELECT a.id, a.deal_id, a.kind, a.title, a.body, a.created_at, a.vendor_id,
                d.company_snapshot, d.title AS deal_title, d.card_id, d.owner, d.pipeline, d.stage
            FROM activities a JOIN deals d ON d.id=a.deal_id AND d.tenant_id=a.tenant_id
            WHERE a.tenant_id=? AND a.created_at>=? AND a.id>? ORDER BY a.id DESC LIMIT 300""",
                         (tenant_id, since, int(after or 0))).fetchall()
        for a in rows:
            title = a["title"] or ""
            kind = a["kind"]
            if kind == "stage":
                if title.startswith("名刺をつなぎました"):
                    if a["card_id"] in new_cards:
                        continue          # 名刺の登録の行にまとめて出す
                    typ = "link"
                elif title.startswith("案件作成"):
                    typ = "deal"
                else:
                    continue              # 段階の変更などは新着にしない
            elif kind in USER_ACTIVITY_KINDS:
                if title.startswith(NEWS_SKIP_TITLES):
                    continue
                typ = "act"
            else:
                continue
            card = cards.get(a["card_id"]) if a["card_id"] else None
            items.append({"key": f"a:{a['id']}", "type": typ, "id": a["id"], "kind": kind,
                          "at": a["created_at"], "deal_id": a["deal_id"],
                          "company": (card or {}).get("company") or a["company_snapshot"] or "",
                          "deal_title": a["deal_title"] or "", "owner": a["owner"] or "",
                          "title": title, "snippet": _news_snippet(a["body"]),
                          "source": "slack" if SLACK_MARK in (a["body"] or "") else
                                    ("vendor" if a["vendor_id"] else ""),
                          "vendor": vnames.get(a["vendor_id"]) if a["vendor_id"] else None})
    items.sort(key=lambda x: x["at"] or "", reverse=True)
    return items


def build_router(check_session: Callable, fetch_cards: Callable,
                 get_tenant: Optional[Callable] = None, list_tenants: Optional[Callable] = None) -> APIRouter:
    r = APIRouter(prefix="/api/sales")
    set_tenant_resolver(get_tenant)
    global _GROUP_FN
    _GROUP_FN = list_tenants

    def tenant_name(tenant_id: str) -> str:
        if get_tenant:
            t = get_tenant(tenant_id) or {}
            return t.get("display_name") or tenant_id
        return tenant_id

    def by_id_for(tenant_id: str) -> dict:
        return card_index(tenant_id)[0]

    def card_index(tenant_id: str):
        """名刺の索引。by_email には (会社, 氏名) の索引を "_cn" キーで同居させる（テナントごと・リクエストごとに作る）"""
        cards = fetch_cards(tenant_id)
        by_id = {c["id"]: c for c in cards}
        by_email = {}
        by_cn = {}
        by_email["\0cn"] = by_cn
        for c in cards:
            e = (c.get("email") or "").strip().lower()
            if e:
                by_email.setdefault(e, c)
            if c.get("name"):
                by_cn.setdefault((_norm_company(c.get("company")), _norm_name(c.get("name"))), c)
        import cardmatch   # 電話・ドメイン・会社名の索引（名刺のない案件をあとから自動でつなぐ）
        by_email["\0ix"] = cardmatch.CardIndex(cards)
        return by_id, by_email

    def resolve(c, row, by_id, by_email):
        by_cn = by_email.get("\0cn", {})
        """card_id で見つからなければメールの控えで再接続する"""
        card = by_id.get(row["card_id"])
        if card:
            return card
        e = (row["email_snapshot"] or "").strip().lower()
        if e and e in by_email:
            card = by_email[e]
            c.execute("UPDATE deals SET card_id=? WHERE id=? AND tenant_id=?", (card["id"], row["id"], row["tenant_id"]))
            return card
        if not row["card_id"] and row["contact_snapshot"]:
            # 名刺なしで作った案件：あとから同じ会社・氏名の名刺が登録されたらつなぐ
            key = (_norm_company(row["company_snapshot"]), _norm_name(row["contact_snapshot"]))
            card = by_cn.get(key)
            if card and not cardmatch_skipped(c, row, card):
                c.execute("UPDATE deals SET card_id=? WHERE id=? AND tenant_id=?", (card["id"], row["id"], row["tenant_id"]))
                return card
        if not row["card_id"] and by_email.get("\0ix"):
            # 業者が登録した案件など：電話・ドメイン・会社名が確実に一致する名刺が1枚だけならつなぐ
            import cardmatch
            try:
                card = cardmatch.auto_link_deal(c, row["tenant_id"], row, by_email["\0ix"])
            except Exception:
                __import__("logging").getLogger("sales").exception("名刺の自動つなぎに失敗")
                card = None
            if card:
                return card
        return None

    def cardmatch_skipped(c, row, card) -> bool:
        import cardmatch
        return cardmatch._skipped(c, row["tenant_id"], row["id"], card["id"])

    # ── マスタ ──
    @r.get("/meta")
    def meta(session: dict = Depends(check_session)):
        return {
            "pipelines": [{k: v for k, v in p.items() if k != "only_tenant"}
                          for p in pipelines_for(tenant_name(session["tenant_id"]))],
            "has_bp": has_bp(tenant_name(session["tenant_id"])),
            "profile": dict(profile_meta(tenant_name(session["tenant_id"])),
                            group_target=bool(group_target_of(session["tenant_id"]))),
            "lost_types": LOST_TYPES,
            "strictness": STRICTNESS,
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
        stall = body.stall_days if body.stall_days is not None else DEFAULT_SETTINGS["stall_days"]
        if not (7 <= stall <= 180):
            raise HTTPException(400, "滞留の日数は7〜180で指定してください")
        if body.monthly_target is not None and not (0 <= body.monthly_target <= 10_000_000_000):
            raise HTTPException(400, "月の受注目標が不正です")
        strictness = body.strictness or "gentle"
        if strictness not in STRICTNESS:
            raise HTTPException(400, "指摘の強さが不正です")
        with _conn() as c:
            c.execute("""INSERT INTO sales_settings (tenant_id, reply_wait_days, stale_days, stall_days, monthly_target,
                    strictness, updated_at)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT(tenant_id) DO UPDATE SET
                reply_wait_days=excluded.reply_wait_days, stale_days=excluded.stale_days,
                stall_days=excluded.stall_days, monthly_target=excluded.monthly_target,
                strictness=excluded.strictness, updated_at=excluded.updated_at""",
                (session["tenant_id"], body.reply_wait_days, body.stale_days, stall,
                 body.monthly_target or None, strictness, _now()))
            c.commit()
        return {"ok": True}

    # ── メンバー（自社の担当者） ──
    @r.get("/members")
    def list_members(session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            rows = [dict(x) for x in c.execute("""SELECT id, name, slack_name, slack_id, email, ics_token FROM members
                WHERE tenant_id=? ORDER BY sort_order, id""", (t,))]
            counts = {r_["owner"]: r_["n"] for r_ in c.execute("""SELECT owner, COUNT(*) n FROM deals WHERE tenant_id=?
                AND stage NOT IN ({}) GROUP BY owner""".format(",".join("?" * len(CLOSED_STAGES))), [t] + sorted(CLOSED_STAGES))}
        names = {m["name"] for m in rows}
        for m in rows:
            m["open_deals"] = counts.get(m["name"], 0)
        # 名刺の「所有者」に出てくる名前で、まだメンバーでない人を候補に
        sugg = {}
        for card in fetch_cards(t):
            for a in card.get("assigned_to_list") or []:
                if a not in names:
                    sugg[a] = sugg.get(a, 0) + 1
        return {"members": rows, "suggestions": [k for k, _ in sorted(sugg.items(), key=lambda x: -x[1])][:20]}

    def _member_vals(body: MemberIn):
        name = _clean_text(body.name, 30, "名前")
        if not name:
            raise HTTPException(400, "名前を入力してください")
        if "," in name:
            raise HTTPException(400, "名前に「,」は使えません")
        return name, _clean_text(body.slack_name, 60, "Slackの表示名"), _clean_text(body.email, 200, "メール")

    @r.post("/members")
    def add_member(body: MemberIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        name, slack_name, email = _member_vals(body)
        with _conn() as c:
            if c.execute("SELECT 1 FROM members WHERE tenant_id=? AND name=?", (t, name)).fetchone():
                raise HTTPException(400, "同じ名前のメンバーがいます")
            n = c.execute("SELECT COUNT(*) n FROM members WHERE tenant_id=?", (t,)).fetchone()["n"]
            if n >= 50:
                raise HTTPException(400, "メンバーは50人までです")
            cur = c.execute("""INSERT INTO members (tenant_id, name, slack_name, email, ics_token, sort_order, created_at)
                VALUES (?,?,?,?,?,?,?)""", (t, name, slack_name, email, secrets.token_urlsafe(24), n, _now()))
            # 担当未設定の案件のうち、この人が名刺の所有者のものは次の一覧表示でつながる
            c.commit()
        return {"ok": True, "id": cur.lastrowid}

    @r.put("/members/{member_id}")
    def update_member(member_id: int, body: MemberIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        name, slack_name, email = _member_vals(body)
        with _conn() as c:
            row = c.execute("SELECT * FROM members WHERE id=? AND tenant_id=?", (member_id, t)).fetchone()
            if not row:
                raise HTTPException(404, "メンバーが見つかりません")
            if name != row["name"]:
                if c.execute("SELECT 1 FROM members WHERE tenant_id=? AND name=?", (t, name)).fetchone():
                    raise HTTPException(400, "同じ名前のメンバーがいます")
                c.execute("UPDATE deals SET owner=? WHERE tenant_id=? AND owner=?", (name, t, row["name"]))
            c.execute("""UPDATE members SET name=?, slack_name=?, email=?,
                slack_id=CASE WHEN COALESCE(slack_name,'')=COALESCE(?,'') THEN slack_id ELSE NULL END
                WHERE id=?""", (name, slack_name, email, slack_name, member_id))
            c.commit()
        return {"ok": True}

    @r.delete("/members/{member_id}")
    def delete_member(member_id: int, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            row = c.execute("SELECT name FROM members WHERE id=? AND tenant_id=?", (member_id, t)).fetchone()
            if not row:
                raise HTTPException(404, "メンバーが見つかりません")
            c.execute("UPDATE deals SET owner='' WHERE tenant_id=? AND owner=?", (t, row["name"]))
            c.execute("DELETE FROM members WHERE id=?", (member_id,))
            c.commit()
        return {"ok": True}

    @r.post("/members/{member_id}/token")
    def reset_member_token(member_id: int, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            cur = c.execute("UPDATE members SET ics_token=? WHERE id=? AND tenant_id=?",
                            (secrets.token_urlsafe(24), member_id, t))
            c.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "メンバーが見つかりません")
        return {"ok": True}

    # ── 会社情報（見積書）・通知・カレンダー ──
    @r.get("/settings/extra")
    def get_extra(session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            row = c.execute("SELECT company_info, notify_won, ics_token FROM sales_settings WHERE tenant_id=?", (t,)).fetchone()
            if not row or not row["ics_token"]:
                tok = secrets.token_urlsafe(24)
                c.execute("""INSERT INTO sales_settings (tenant_id, updated_at, ics_token) VALUES (?,?,?)
                    ON CONFLICT(tenant_id) DO UPDATE SET ics_token=excluded.ics_token""", (t, _now(), tok))
                c.commit()
                row = c.execute("SELECT company_info, notify_won, ics_token FROM sales_settings WHERE tenant_id=?", (t,)).fetchone()
        return {"company_info": row["company_info"] or "", "notify_won": bool(row["notify_won"]),
                "ics_token": row["ics_token"], "tenant_name": tenant_name(t)}

    @r.put("/settings/extra")
    def put_extra(body: SettingsExtra, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        get_extra(session)  # 行がなければ作る
        sets = {}
        if body.company_info is not None:
            sets["company_info"] = _clean_text(body.company_info, 1000, "会社情報") or ""
        if body.notify_won is not None:
            sets["notify_won"] = 1 if body.notify_won else 0
        if sets:
            with _conn() as c:
                c.execute(f"UPDATE sales_settings SET {', '.join(k + '=?' for k in sets)} WHERE tenant_id=?",
                          list(sets.values()) + [t])
                c.commit()
        return {"ok": True}

    @r.post("/settings/extra/token")
    def reset_team_token(session: dict = Depends(check_session)):
        t = session["tenant_id"]
        get_extra(session)
        with _conn() as c:
            c.execute("UPDATE sales_settings SET ics_token=? WHERE tenant_id=?", (secrets.token_urlsafe(24), t))
            c.commit()
        return {"ok": True}

    # ── CSV（Excelで開ける） ──
    @r.get("/export.csv")
    def export_csv(pipeline: str = "", owner: str = "", session: dict = Depends(check_session)):
        t = session["tenant_id"]
        deals = list_deals(q="", stage="", card_id="", pipeline=pipeline, owner=owner, session=session)["deals"]
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["番号", "種類", "会社", "先方担当", "メール", "案件名", "段階", "金額", "確度%", "見込み額",
                    "受注予定日", "次のアクション", "期限", "自社担当", "失注理由", "失注の種類", "登録日", "完了日", "営業部長の指摘",
                    "分野", "商流", "現場名", "エンドユーザー", "納期"])
        def safe(v):
            # Excel で数式として動かないように（OCR・Slack から入った文字列対策）
            return "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v

        for d in deals:
            card = d.get("card") or {}
            w.writerow(map(safe, [d["id"], pipeline_of(d)["label"], card.get("company") or d.get("company_snapshot") or "",
                        card.get("name") or d.get("contact_snapshot") or "", card.get("email") or d.get("email_snapshot") or "",
                        d["title"], d["stage"], d.get("amount") or "", d.get("prob"), d.get("weighted") or "",
                        d.get("expected_close") or "", d.get("next_action") or "", d.get("next_action_due") or "",
                        d.get("owner") or "", d.get("lost_reason") or "", LOST_TYPES.get(d.get("lost_type") or "", ""),
                        (d.get("created_at") or "")[:10], (d.get("closed_at") or "")[:10],
                        " / ".join(a["title"] for a in d.get("advice") or []),
                        d.get("segment") or "", d.get("channel") or "", d.get("site_name") or "",
                        d.get("end_user") or "", d.get("delivery_on") or ""]))
        data = "\ufeff" + buf.getvalue()   # Excel で文字化けしないよう BOM をつける
        fname = f"deals_{date.today().strftime('%Y%m%d')}.csv"
        return Response(content=data.encode("utf-8"), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename={fname}"})

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
    def list_deals(q: str = "", stage: str = "", card_id: str = "", pipeline: str = "", owner: str = "",
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
            import advisor  # 循環 import を避けるためここで読む
            deals = []
            vnames = vendor_names(c, t)
            members = set(member_names(c, t))
            allowed = {p["key"] for p in pipelines_for(tenant_name(t))}
            for row in rows:
                if row["pipeline"] == "bp" and "bp" not in allowed:
                    # ブループリントを使わない会社（和宏電機）に入ってしまった取材案件は営業に戻す
                    sets = {}
                    move_pipeline(c, t, dict(row), "sales", sets, tenant_name(t))
                    apply_sets(c, t, row["id"], sets)
                    row = c.execute("""SELECT d.*, e.name AS event_name, e.event_date AS event_date
                        FROM deals d LEFT JOIN events e ON e.id = d.event_id WHERE d.id=?""", (row["id"],)).fetchone()
                card = resolve(c, row, by_id, by_email)
                if row["owner"] is None and card and members:
                    # 担当者が未設定なら、名刺の所有者（メンバーに登録済みの人）を入れる
                    cand = next((x for x in card.get("assigned_to_list") or [] if x in members), None)
                    if cand:
                        c.execute("UPDATE deals SET owner=? WHERE id=? AND tenant_id=?", (cand, row["id"], t))
                        row = dict(row); row["owner"] = cand
                acts = acts_by_deal.get(row["id"], [])
                last = acts[0]["occurred_at"] if acts else None
                d = _deal_dict(row, card, last, compute_alerts(dict(row), acts, settings))
                d["vendor_name"] = vnames.get(d.get("vendor_id")) if d.get("vendor_id") else None
                deals.append((d, acts))
            c.commit()  # 再接続・担当者の自動設定があれば保存（部長の計算の前に書き込みを終える）
        # 同じ現場の進行中の引合（別の商社・メーカーからも来ていることがある）
        by_site = {}
        for d, _ in deals:
            k = norm_site(d.get("site_name"))
            if k and d["is_open"] and d["pipeline"] == "sales":
                by_site.setdefault(k, []).append(d["id"])
        for d, _ in deals:
            k = norm_site(d.get("site_name"))
            d["same_site"] = [x for x in by_site.get(k, []) if x != d["id"]] if k and d["pipeline"] == "sales" else []
        for d, acts in deals:
            d["advice"] = advisor.deal_advice(d, acts, settings)
        deals = [d for d, _ in deals]
        try:
            import vendorreq  # 業者とのやりとり（新着・依頼中）
            with _conn() as c:
                vs = vendorreq.deal_summary(c, t)
            for d in deals:
                d["vreq"] = vs.get(d["id"])
        except Exception:
            __import__("logging").getLogger("sales").exception("業者とのやりとりの読み込みに失敗")

        if pipeline:
            deals = [d for d in deals if d["pipeline"] == pipeline]
        if owner:
            deals = [d for d in deals if (d.get("owner") or "") == ("" if owner == "-" else owner)]
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

    # ── 新着（今日やることの一番上）──
    #   最近の営業メモ・電話・アポ・メールの記録、名刺の登録、名刺のつながり、新しい案件
    #   既読は端末ごと（ブラウザに保存）。ここでは「最近のもの」を返すだけ
    @r.get("/news")
    def news(days: int = 7, after: int = 0, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        return {"items": news_items(c_cards=fetch_cards(t), tenant_id=t, days=days, after=after),
                "max_activity_id": _max_activity_id(t), "now": _now()}

    @r.get("/cards/{card_id}/deal-candidates")
    def card_deal_candidates(card_id: str, session: dict = Depends(check_session)):
        """この名刺をつなげそうな、名刺のない進行中の案件（新規案件の画面で先に出す）"""
        import cardmatch
        t = session["tenant_id"]
        card = by_id_for(t).get(card_id)
        if not card:
            raise HTTPException(404, "名刺が見つかりません")
        with _conn() as c:
            cands = cardmatch.candidates(c, t, card)
        return {"candidates": cands[:5], "auto": (cardmatch.auto_target(cands) or {}).get("id")}

    @r.post("/deals")
    def create_deal(body: DealCreate, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        by_id, _ = card_index(t)
        if body.card_id:
            card = by_id.get(body.card_id)
            if not card:
                raise HTTPException(404, "名刺が見つかりません")
        else:
            company = _clean_text(body.company, 120, "会社名")
            contact = _clean_text(body.contact, 60, "氏名")
            if not company and not contact:
                raise HTTPException(400, "名刺を選ぶか、会社名か氏名を入力してください")
            card = {"id": "", "company": company, "name": contact,
                    "email": _clean_text(body.email, 200, "メール"), "phone": _clean_text(body.phone, 40, "電話")}
        if body.pipeline not in [p["key"] for p in pipelines_for(tenant_name(t))]:
            raise HTTPException(400, "案件の種類が不正です")
        with _conn() as c:
            owner = check_owner(c, t, body.owner)
            deal_id = insert_deal(c, t, card, body.title, body.stage, body.amount, body.event_id,
                                  body.next_action, body.next_action_due, pipeline=body.pipeline,
                                  expected_close=body.expected_close, owner=owner,
                                  extra={f: getattr(body, f) for f in EXTRA_FIELDS},
                                  created_note=None if body.card_id else f"案件作成（{body.stage or pipeline_of(body.pipeline)['first']}・名刺なし）")
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
        # 同じ会社の人（関係者・キーマン候補）と、名刺なし案件のつなぎ先候補
        key = _norm_company(card.get("company") if card else row["company_snapshot"])
        same = []
        if key:
            for x in by_id.values():
                k2 = _norm_company(x.get("company"))
                if k2 and (k2 == key or (len(key) >= 3 and len(k2) >= 3 and (key in k2 or k2 in key))):
                    if not card or x["id"] != card["id"]:
                        same.append(_card_summary(x))
        d["company_cards"] = same[:20]
        with _conn() as c:
            d["company_deals"] = [dict(x) for x in c.execute("""SELECT id, title, stage, pipeline, amount, lost_reason, closed_at
                FROM deals WHERE tenant_id=? AND id<>? AND (card_id IN ({}) OR company_snapshot=?)
                ORDER BY updated_at DESC LIMIT 20""".format(",".join("?" * (len(same) + 1))),
                [t, deal_id] + [x["id"] for x in same] + [(card or {}).get("id") or "-", row["company_snapshot"] or "-"]).fetchall()]
            adv = c.execute("""SELECT created_at, body FROM deal_advice WHERE tenant_id=? AND deal_id=?
                ORDER BY id DESC LIMIT 1""", (t, deal_id)).fetchone()
        d["ai_advice"] = {"created_at": adv["created_at"], **json.loads(adv["body"])} if adv else None
        d["items"] = deal_items(t, deal_id)
        try:
            import vendorreq  # 業者とのやりとり（一覧の札と同じもの）
            with _conn() as c:
                d["vreq"] = vendorreq.deal_summary(c, t, deal_id).get(deal_id)
        except Exception:
            __import__("logging").getLogger("sales").exception("業者とのやりとりの読み込みに失敗")
        if d.get("vendor_id"):
            with _conn() as c:
                d["vendor_name"] = vendor_names(c, t).get(d["vendor_id"])
        # テレアポ業者の入力（業者の画面と同じデータをそのまま読む）。開いたら「業者の更新あり」を見たことにする
        import vendor
        with _conn() as c:
            d["vendor_entry"] = vendor.staff_view(c, t, row, card)
            if d["vendor_entry"] and d["vendor_entry"]["new"]:
                vendor.mark_staff_seen(c, t, deal_id)
                c.commit()
        d["same_site"] = []
        k = norm_site(d.get("site_name"))
        with _conn() as c:
            if k and d["pipeline"] == "sales":
                for x in c.execute("""SELECT id, site_name, stage FROM deals WHERE tenant_id=? AND id<>? AND site_name IS NOT NULL
                        AND COALESCE(pipeline,'sales')='sales'""", (t, deal_id)).fetchall():
                    if norm_site(x["site_name"]) == k and x["stage"] not in CLOSED_STAGES:
                        d["same_site"].append(x["id"])
            d["group_link"] = group_link(c, t, row)
            d.pop("group_tenant", None)
            links = [x for x in (d.get("prod_deal_id"), d.get("source_deal_id")) if x]
            d["linked_deals"] = [dict(x) for x in c.execute(
                f"SELECT id, title, stage, pipeline FROM deals WHERE tenant_id=? AND id IN ({','.join('?' * len(links))})",
                [t, *links]).fetchall()] if links else []
        import advisor
        d["advice"] = advisor.deal_advice(d, acts, settings)
        return d

    def update_in_tx(c, t: str, deal_id: int, data: dict) -> bool:
        """案件の更新（画面のPATCH・AI議事録の反映で共通）。commit はしない"""
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

        if "owner" in data:
            sets["owner"] = check_owner(c, t, data["owner"]) or ""
            if sets["owner"] != (cur.get("owner") or ""):
                log_activity(c, t, deal_id, "stage", f"担当者: {cur.get('owner') or 'なし'} → {sets['owner'] or 'なし'}")
        if "card_id" in data:
            link_card(c, t, cur, data["card_id"] or "", by_id_for(t), sets)
        for f, n, label in (("company_snapshot", 120, "会社名"), ("contact_snapshot", 60, "氏名"),
                            ("email_snapshot", 200, "メール"), ("phone_snapshot", 40, "電話")):
            if f in data and not data.get("card_id"):
                if cur["card_id"] and "card_id" not in data:
                    raise HTTPException(400, "名刺がつながっている案件の連絡先は名刺管理で直してください")
                sets[f] = _clean_text(data[f], n, label)
        if "expected_close" in data:
            newd = _clean_date(data["expected_close"])
            if cur.get("expected_close") and newd != cur["expected_close"]:
                sets["close_changes"] = (cur.get("close_changes") or 0) + 1
                log_activity(c, t, deal_id, "stage",
                             f"受注予定日を変更: {cur['expected_close']} → {newd or '未定'}")
            sets["expected_close"] = newd
        if "review" in data:
            sets["review"] = _clean_text(data["review"], 5000, "振り返り")
        if "checks" in data:
            sets["checks"] = json.dumps(clean_checks({**cur, "pipeline": data.get("pipeline") or cur["pipeline"]},
                                                     data["checks"]), ensure_ascii=False)
        if "lost_type" in data:
            lt = data["lost_type"]
            if lt is not None and lt not in LOST_TYPES:
                raise HTTPException(400, "失注の種類が不正です")
            sets["lost_type"] = lt
            if lt and "reapproach_on" not in data and not cur.get("reapproach_on"):
                sets["reapproach_on"] = (date.today() + timedelta(days=REAPPROACH_DAYS[lt])).isoformat()
        if "reapproach_on" in data:
            sets["reapproach_on"] = _clean_date(data["reapproach_on"])
        if "repeat_on" in data:
            sets["repeat_on"] = _clean_date(data["repeat_on"])
        for f, label in (("telapo_at", "架電日時"), ("appo_at", "アポ日時")):
            if f in data:
                sets[f] = _clean_dt(data[f], label)
        ex = clean_extra(data, profile_key(tenant_name(t)))
        if "delivery_on" in ex and cur.get("delivery_on") and ex["delivery_on"] != cur["delivery_on"]:
            log_activity(c, t, deal_id, "stage", f"納期を変更: {cur['delivery_on']} → {ex['delivery_on'] or '未定'}")
        sets.update(ex)
        pipeline = cur["pipeline"] or "sales"
        if data.get("pipeline") and data["pipeline"] != pipeline:
            move_pipeline(c, t, cur, data["pipeline"], sets, tenant_name(t))
            pipeline = data["pipeline"]
            cur["stage"] = sets.get("stage", cur["stage"])
        new_stage = data.get("stage")
        if new_stage is not None and new_stage != cur["stage"]:
            change_stage(c, t, deal_id, cur["stage"], new_stage, sets,
                         lost_reason=sets.get("lost_reason", cur["lost_reason"]),
                         lost_detail=sets.get("lost_detail"), pipeline=pipeline)

        if data.get("undo_max_stage_idx") is not None and new_stage is not None:
            sets["max_stage_idx"] = max(0, int(data["undo_max_stage_idx"]))
        if not sets:
            return False
        apply_sets(c, t, deal_id, sets)
        return True

    @r.patch("/deals/{deal_id}")
    def update_deal(deal_id: int, body: DealUpdate, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        data = _dump(body)
        if not data:
            return {"ok": True}
        with _conn() as c:
            if update_in_tx(c, t, deal_id, data):
                c.commit()
        return {"ok": True}

    @r.post("/deals/bulk-stage")
    def bulk_stage(body: BulkStageIn, session: dict = Depends(check_session)):
        """まとめて段階を変える（保留・失注・ほかの段階）。段階が合わない案件（別の種類）は飛ばす"""
        t = session["tenant_id"]
        ids = list(dict.fromkeys(body.ids))[:300]
        if not ids:
            raise HTTPException(400, "案件を選んでください")
        done, skipped, before = [], [], []
        with _conn() as c:
            c.execute("BEGIN IMMEDIATE")
            for i in ids:
                row = c.execute("SELECT id, stage, pipeline FROM deals WHERE id=? AND tenant_id=?", (i, t)).fetchone()
                if not row:
                    skipped.append(i)
                    continue
                stage = body.stages.get(str(i)) if body.stages else body.stage
                if not stage or row["stage"] == stage:
                    continue
                if stage not in pipeline_of(row["pipeline"])["stages"]:
                    skipped.append(i)
                    continue
                update_in_tx(c, t, i, {"stage": stage})
                done.append(i)
                before.append({"id": i, "stage": row["stage"]})
            c.commit()
        return {"updated": len(done), "ids": done, "skipped": skipped, "before": before}

    # ── 見積明細（品名・材質・板厚・数量・単価） ──
    def _item_body(body: "ItemIn") -> tuple:
        name = _clean_text(body.name, 120, "品名")
        if not name:
            raise HTTPException(400, "品名を入力してください")
        for v, label in ((body.qty, "数量"), (body.unit_price, "単価")):
            if v is not None and not (0 <= v <= 1_000_000_000):
                raise HTTPException(400, f"{label}が不正です")
        return (name, _clean_text(body.material, 60, "材質"), _clean_text(body.thickness, 20, "板厚"),
                body.qty, body.unit_price, _clean_text(body.note, 300, "備考"))

    @r.post("/deals/{deal_id}/items")
    def add_item(deal_id: int, body: ItemIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        vals = _item_body(body)
        with _conn() as c:
            _get_deal_row(c, t, deal_id)
            n = c.execute("SELECT COUNT(*) n FROM deal_items WHERE deal_id=?", (deal_id,)).fetchone()["n"]
            if n >= 100:
                raise HTTPException(400, "明細は100行までです")
            cur = c.execute("""INSERT INTO deal_items (tenant_id, deal_id, name, material, thickness, qty, unit_price, note,
                sort_order, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""", (t, deal_id, *vals, n, _now()))
            c.commit()
        return {"ok": True, "id": cur.lastrowid}

    @r.put("/items/{item_id}")
    def update_item(item_id: int, body: ItemIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        vals = _item_body(body)
        with _conn() as c:
            cur = c.execute("""UPDATE deal_items SET name=?, material=?, thickness=?, qty=?, unit_price=?, note=?
                WHERE id=? AND tenant_id=?""", (*vals, item_id, t))
            c.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "明細が見つかりません")
        return {"ok": True}

    @r.delete("/items/{item_id}")
    def delete_item(item_id: int, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            cur = c.execute("DELETE FROM deal_items WHERE id=? AND tenant_id=?", (item_id, t))
            c.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "明細が見つかりません")
        return {"ok": True}

    @r.post("/deals/{deal_id}/items/apply")
    def apply_items(deal_id: int, session: dict = Depends(check_session)):
        """明細の合計を案件の金額にする"""
        t = session["tenant_id"]
        total = sum(x["subtotal"] or 0 for x in deal_items(t, deal_id))
        with _conn() as c:
            _get_deal_row(c, t, deal_id)
            apply_sets(c, t, deal_id, {"amount": total})
            c.commit()
        return {"ok": True, "amount": total}

    # ── リピート（受注生産の次回分） ──
    @r.post("/deals/{deal_id}/repeat")
    def make_repeat(deal_id: int, session: dict = Depends(check_session)):
        """受注した案件から次回分の案件を作る（名刺・明細を引き継ぐ）"""
        t = session["tenant_id"]
        by_id, by_email = card_index(t)
        with _conn() as c:
            row = dict(_get_deal_row(c, t, deal_id))
            if row["stage"] != pipeline_of(row)["won"] or (row["pipeline"] or "sales") != "sales":
                raise HTTPException(400, "受注した営業案件からだけ作れます")
            card = resolve(c, _get_deal_row(c, t, deal_id), by_id, by_email) or {
                "id": "", "company": row["company_snapshot"], "name": row["contact_snapshot"],
                "email": row["email_snapshot"], "phone": row["phone_snapshot"]}
            title = re.sub(r"^【リピート】", "", row["title"])
            new_id = insert_deal(c, t, card, f"【リピート】{title}"[:120], "ヒアリング", row["amount"], row["event_id"],
                                 "次回分の数量・納期を確認", (date.today() + timedelta(days=3)).isoformat(),
                                 created_note=f"案件作成（ヒアリング・#{deal_id} のリピート）")
            for it in c.execute("""SELECT name, material, thickness, qty, unit_price, note, sort_order FROM deal_items
                    WHERE tenant_id=? AND deal_id=? ORDER BY sort_order, id""", (t, deal_id)).fetchall():
                c.execute("""INSERT INTO deal_items (tenant_id, deal_id, name, material, thickness, qty, unit_price, note,
                    sort_order, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""", (t, new_id, *tuple(it), _now()))
            log_activity(c, t, deal_id, "stage", f"リピート案件 #{new_id} を作成")
            apply_sets(c, t, deal_id, {"repeat_on": None, "repeat_deal_id": new_id})
            c.commit()
        return {"ok": True, "id": new_id}

    # ── 受注後の進行（和宏電機） ──
    def make_prod_in_tx(c, t: str, deal_id: int, cards=None) -> int:
        """受注した営業案件から「受注後の進行」の案件を作る（トランザクションの中で呼ぶ。commit はしない）"""
        if "prod" not in [p["key"] for p in pipelines_for(tenant_name(t))]:
            raise HTTPException(400, "この会社では使えません")
        by_id, by_email = cards or card_index(t)
        if True:
            row = dict(_get_deal_row(c, t, deal_id))
            if (row["pipeline"] or "sales") != "sales" or row["stage"] != "受注":
                raise HTTPException(400, "受注した営業案件からだけ作れます")
            if row.get("prod_deal_id") and c.execute("SELECT 1 FROM deals WHERE id=? AND tenant_id=?",
                                                     (row["prod_deal_id"], t)).fetchone():
                raise HTTPException(409, f"受注後の進行 #{row['prod_deal_id']} がすでにあります")
            card = resolve(c, _get_deal_row(c, t, deal_id), by_id, by_email) or {
                "id": "", "company": row["company_snapshot"], "name": row["contact_snapshot"],
                "email": row["email_snapshot"], "phone": row["phone_snapshot"]}
            extra = {f: row.get(f) for f in EXTRA_FIELDS}
            extra["source_deal_id"] = deal_id
            new_id = insert_deal(c, t, card, row["title"], "承認図作成", row["amount"], row["event_id"],
                                 "承認図の提出日と返却期限を決める", (date.today() + timedelta(days=3)).isoformat(),
                                 created_note=f"受注後の進行を開始（#{deal_id} の受注から）", pipeline="prod",
                                 owner=row.get("owner"), extra=extra)
            for it in c.execute("""SELECT name, material, thickness, qty, unit_price, note, sort_order FROM deal_items
                    WHERE tenant_id=? AND deal_id=? ORDER BY sort_order, id""", (t, deal_id)).fetchall():
                c.execute("""INSERT INTO deal_items (tenant_id, deal_id, name, material, thickness, qty, unit_price, note,
                    sort_order, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""", (t, new_id, *tuple(it), _now()))
            log_activity(c, t, deal_id, "stage", f"受注後の進行 #{new_id} を作成")
            apply_sets(c, t, deal_id, {"prod_deal_id": new_id})
        return new_id

    @r.post("/deals/{deal_id}/to-prod")
    def make_prod(deal_id: int, session: dict = Depends(check_session)):
        """受注した営業案件から「受注後の進行」の案件を作る（名刺・明細・現場・納期を引き継ぐ）"""
        t = session["tenant_id"]
        if "prod" not in [p["key"] for p in pipelines_for(tenant_name(t))]:
            raise HTTPException(400, "この会社では使えません")
        cards = card_index(t)
        with _conn() as c:
            c.execute("BEGIN IMMEDIATE")   # 二度押し・同時の要求で2件できないように
            new_id = make_prod_in_tx(c, t, deal_id, cards)
            c.commit()
        return {"ok": True, "id": new_id}

    # ── グループ会社への依頼（和宏電機 → ワコーメタルに筐体の板金） ──
    def group_request_in_tx(c, t: str, deal_id: int, note_raw: Optional[str]) -> int:
        """和宏電機の案件からワコーメタルに筐体を依頼する（トランザクションの中で呼ぶ。commit はしない）"""
        own = tenant_name(t)
        if profile_key(own) != "panel":
            raise HTTPException(400, "この会社では使えません")
        target = group_target_of(t)
        if not target:
            raise HTTPException(400, "依頼先（ワコーメタル）が見つかりません")
        note = _clean_text(note_raw, 2000, "依頼内容")
        if True:
            row = dict(_get_deal_row(c, t, deal_id))
            if row.get("group_deal_id") and group_link(c, t, row):
                raise HTTPException(409, f"ワコーメタルへの依頼 #{row['group_deal_id']} がすでにあります")
            requester = row.get("owner") or ""
            card = {"id": "", "company": own, "name": requester or None, "email": None, "phone": None}
            title = re.sub(r"^【和宏電機】", "", row["title"])
            due = (date.today() + timedelta(days=3)).isoformat()
            extra = {"segment": "電機・制御盤メーカー", "channel": "グループ（和宏電機）", "delivery_on": row.get("delivery_on")}
            new_id = insert_deal(c, target, card, f"【和宏電機】{title} 筐体"[:120], "ヒアリング", None, None,
                                 "図面・仕様を受け取り見積を出す", due,
                                 created_note=f"案件作成（ヒアリング・和宏電機 #{deal_id} からの依頼）", owner=None, extra=extra)
            for it in c.execute("""SELECT name, qty, sort_order FROM deal_items WHERE tenant_id=? AND deal_id=?
                    ORDER BY sort_order, id""", (t, deal_id)).fetchall():
                c.execute("""INSERT INTO deal_items (tenant_id, deal_id, name, qty, sort_order, created_at)
                    VALUES (?,?,?,?,?,?)""", (target, new_id, f"{it['name']} 筐体"[:120], it["qty"], it["sort_order"], _now()))
            lines = [f"和宏電機 #{deal_id}「{row['title']}」からの筐体（板金）の依頼です。"]
            for k, label in (("site_name", "現場名"), ("end_user", "エンドユーザー"), ("delivery_on", "納期（受電日）")):
                if row.get(k):
                    lines.append(f"{label}: {row[k]}")
            if requester:
                lines.append(f"和宏電機の担当: {requester}")
            if note:
                lines.append("\n" + note)
            log_activity(c, target, new_id, "memo", "和宏電機からの依頼", "\n".join(lines))
            c.execute("UPDATE deals SET group_tenant=?, group_deal_id=? WHERE id=? AND tenant_id=?", (t, deal_id, new_id, target))
            apply_sets(c, t, deal_id, {"group_tenant": target, "group_deal_id": new_id})
            log_activity(c, t, deal_id, "stage", f"ワコーメタルに筐体を依頼（先方の案件 #{new_id}）", note)
        return new_id

    @r.post("/deals/{deal_id}/group-request")
    def group_request(deal_id: int, body: GroupRequest, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            c.execute("BEGIN IMMEDIATE")
            new_id = group_request_in_tx(c, t, deal_id, body.note)
            c.commit()
        return {"ok": True, "id": new_id}

    @r.get("/bp-candidates")
    def bp_candidates(session: dict = Depends(check_session)):
        """営業に入っているブループリントらしい案件（案件名・メールの件名で判定）"""
        t = session["tenant_id"]
        if "bp" not in [p["key"] for p in pipelines_for(tenant_name(t))]:
            return {"deals": []}
        with _conn() as c:
            rows = c.execute("""SELECT d.id, d.title, d.stage, d.company_snapshot, d.contact_snapshot
                FROM deals d WHERE d.tenant_id=? AND d.pipeline='sales' ORDER BY d.id""", (t,)).fetchall()
            out = []
            for r_ in rows:
                hit = BP_WORDS.search(r_["title"] or "")
                if not hit:
                    hit = c.execute("""SELECT 1 FROM activities WHERE deal_id=? AND kind IN ('email','meeting','memo')
                        AND (title LIKE '%Blue Print%' OR title LIKE '%ブループリント%' OR title LIKE '%BP取材%')
                        LIMIT 1""", (r_["id"],)).fetchone()
                if hit:
                    d = dict(r_)
                    d["new_stage"] = STAGE_MAP[("sales", "bp")].get(d["stage"], "最初の打ち合わせ")
                    out.append(d)
        return {"deals": out}

    @r.post("/deals/move")
    def move_deals(body: DealMove, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        if not body.ids:
            raise HTTPException(400, "案件を選んでください")
        with _conn() as c:
            for deal_id in body.ids:
                cur = dict(_get_deal_row(c, t, deal_id))
                if (cur["pipeline"] or "sales") == body.pipeline:
                    continue
                sets = {}
                move_pipeline(c, t, cur, body.pipeline, sets, tenant_name(t))
                apply_sets(c, t, deal_id, sets)
            c.commit()
        return {"ok": True, "moved": len(body.ids)}

    @r.delete("/deals/{deal_id}")
    def delete_deal(deal_id: int, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            cur = c.execute("DELETE FROM deals WHERE id=? AND tenant_id=?", (deal_id, t))
            if cur.rowcount:
                try:
                    c.execute("DELETE FROM deal_digests WHERE deal_id=? AND tenant_id=?", (deal_id, t))
                except sqlite3.OperationalError:
                    pass   # 議事録の表がまだない
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
            "見積明細": items_text(deal_items(t, deal_id), profile_key(tenant_name(t))),
            "現場名": row["site_name"] or "",
            "納期": _jp_date(row["delivery_on"]),
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
            dt = datetime.fromisoformat(occurred.replace("Z", "+00:00"))
            if dt.tzinfo:
                dt = dt.astimezone(timezone(timedelta(hours=9))).replace(tzinfo=None)
            occurred = dt.isoformat(timespec="seconds")
        except ValueError:
            raise HTTPException(400, "日時の形式が不正です")
        try:
            datetime.fromisoformat(occurred)
        except ValueError:
            raise HTTPException(400, "日時が不正です")
        with _conn() as c:
            _get_deal_row(c, t, deal_id)
            row = _get_deal_row(c, t, deal_id)
            act_id = log_activity(c, t, deal_id, body.kind, title, text, occurred,
                                  awaiting_reply=(body.kind == "email" and body.awaiting_reply))
            if row["vendor_id"]:
                vis = body.vendor_visible if body.vendor_visible is not None else body.kind == "meeting"
                c.execute("UPDATE activities SET vendor_visible=? WHERE id=?", (1 if vis else 0, act_id))
            sets = {}
            data = _dump(body)
            if "next_action" in data:
                sets["next_action"] = _clean_text(data["next_action"], 200, "次のアクション")
            if "next_action_due" in data:
                sets["next_action_due"] = _clean_date(data["next_action_due"])
            apply_sets(c, t, deal_id, sets)
            c.commit()
        return {"ok": True, "id": act_id}

    @r.patch("/activities/{activity_id}")
    def set_visibility(activity_id: int, body: ActivityVisibility, session: dict = Depends(check_session)):
        """テレアポ業者に見せる・見せないを切り替える"""
        t = session["tenant_id"]
        with _conn() as c:
            row = c.execute("""SELECT a.id, a.kind, a.vendor_id AS author, d.vendor_id FROM activities a
                JOIN deals d ON d.id=a.deal_id AND d.tenant_id=a.tenant_id WHERE a.id=? AND a.tenant_id=?""",
                            (activity_id, t)).fetchone()
            if not row:
                raise HTTPException(404, "記録が見つかりません")
            if not row["vendor_id"]:
                raise HTTPException(400, "テレアポ業者の案件ではありません")
            if row["author"] or row["kind"] == "stage":
                raise HTTPException(400, "この記録は業者にいつも見えます")
            c.execute("UPDATE activities SET vendor_visible=? WHERE id=?", (1 if body.vendor_visible else 0, activity_id))
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

    # 部長（advisor）から使う読み込み
    def load_all(tenant_id: str):
        res = list_deals(q="", stage="", card_id="", pipeline="", owner="", session={"tenant_id": tenant_id})
        acts_by_deal = {}
        with _conn() as c:
            for a in c.execute("""SELECT id, deal_id, kind, occurred_at FROM activities WHERE tenant_id=?
                    ORDER BY occurred_at DESC, id DESC""", (tenant_id,)):
                acts_by_deal.setdefault(a["deal_id"], []).append(dict(a))
        return res["deals"], acts_by_deal, res["settings"]

    r.load_all = load_all
    r.update_in_tx = update_in_tx
    r.make_prod_in_tx = make_prod_in_tx
    r.group_request_in_tx = group_request_in_tx
    r.card_index = card_index
    r.load_deal = lambda tenant_id, deal_id: get_deal(deal_id, session={"tenant_id": tenant_id})

    # ── 集計 ──
    @r.get("/stats")
    def stats(pipeline: str = "sales", session: dict = Depends(check_session)):
        t = session["tenant_id"]
        p = pipeline_of(pipeline)
        with _conn() as c:
            by_stage = {s: 0 for s in p["stages"]}
            for row in c.execute("""SELECT stage, COUNT(*) n FROM deals WHERE tenant_id=? AND pipeline=?
                    GROUP BY stage""", (t, p["key"])):
                by_stage[row["stage"]] = row["n"]
            reasons = {lr: 0 for lr in LOST_REASONS}
            for row in c.execute("""SELECT lost_reason, COUNT(*) n FROM deals
                WHERE tenant_id=? AND pipeline=? AND stage=? GROUP BY lost_reason""", (t, p["key"], p["lost"])):
                if row["lost_reason"]:
                    reasons[row["lost_reason"]] = row["n"]
            won_amount = c.execute("""SELECT COALESCE(SUM(amount),0) s FROM deals
                WHERE tenant_id=? AND pipeline=? AND stage=?""", (t, p["key"], p["won"])).fetchone()["s"]
        won, lost = by_stage[p["won"]], by_stage[p["lost"]]
        return {
            "by_stage": by_stage,
            "won": won,
            "lost": lost,
            "win_rate": round(won / (won + lost), 3) if (won + lost) else None,
            "lost_reasons": reasons,
            "won_amount": won_amount,
        }

    return r
