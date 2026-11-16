"""
テレアポ業者との連携
  ・こちら側（ログイン済み）: 業者の登録（A社・B社…）、専用URLとパスワードの発行・再発行・停止、業者別の数字
  ・業者側（/partner.html + /api/partner/*）: 自社が記録した案件だけを見る・新しいテレアポを記録・電話メモを追記
    業者が見られるのは「自分が記録した案件」の
      会社名・相手・自分が入力した連絡先・段階・架電日時・アポ日時
      自分の記録と、こちらが「業者にも見せる」にした記録・段階の移り変わり
    だけ。金額・名刺・社内メモ・ほかの案件は一切見えない。
"""
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel

import loginguard
import sales
import vendorreq

log = logging.getLogger("vendor")
COOKIE = "partner_session"
SESSION_DAYS = 14
RESULTS = ["アポ獲得", "資料送付", "再コール", "担当者不在", "見込みなし", "その他"]
DAILY_CREATE_LIMIT = 200
DAILY_CALL_LIMIT = 500          # 業者ごと・1日
DAILY_CALL_PER_DEAL = 30        # 1案件・1日
VENDOR_HOOKS: list = []   # 業者が記録したときに呼ぶ（Slack のお知らせ）。info(dict)


def init_db():
    with sales._conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS vendor_sessions (
            token_hash TEXT PRIMARY KEY,
            vendor_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS vendor_seen (
            vendor_id INTEGER NOT NULL,
            deal_id INTEGER NOT NULL,
            seen_at TEXT NOT NULL,
            last_act_id INTEGER NOT NULL DEFAULT 0,   -- 見た時点で一番新しい記録
            PRIMARY KEY (vendor_id, deal_id)
        )""")
        sales._add_column(c, "deals", "vendor_contact", "vendor_contact TEXT")   # 業者が入力した連絡先（JSON）
        # 業者が直した・電話を足した日時と、こちらが案件を開いて見た日時（「業者の更新あり」の印）
        sales._add_column(c, "deals", "vendor_touched_at", "vendor_touched_at TEXT")
        sales._add_column(c, "deals", "vendor_staff_seen_at", "vendor_staff_seen_at TEXT")
        sales._add_column(c, "deals", "vendor_changed", "vendor_changed TEXT")   # こちらが見たあとに業者が直した項目（JSON）
        _backfill_touched(c)
        c.commit()
    import vendorreq   # 業者とのやりとり（依頼）の表も一緒に作る
    vendorreq.init_db()


# ── パスワード ──
def hash_pw(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000)
    return f"pbkdf2${200_000}${salt.hex()}${dk.hex()}"


def check_pw(pw: str, stored: str) -> bool:
    try:
        _, it, salt, h = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", (pw or "").encode(), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(dk.hex(), h)
    except Exception:
        return False


def new_password() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(12))


def _cookie_secure() -> bool:
    return os.getenv("PARTNER_COOKIE_SECURE", "1") != "0"


def _drop_sessions(c, vendor_id: int):
    c.execute("DELETE FROM vendor_sessions WHERE vendor_id=?", (vendor_id,))


def _backfill_touched(c):
    """すでに業者が入力した案件：最後に業者が入れた日時を入れておく（見たことにして「更新あり」は付けない）"""
    rows = c.execute("""SELECT d.id, d.created_at,
            (SELECT MAX(a.created_at) FROM activities a WHERE a.deal_id=d.id AND a.vendor_id IS NOT NULL) last_act
        FROM deals d WHERE d.vendor_touched_at IS NULL
          AND (d.vendor_id IS NOT NULL OR d.vendor_contact IS NOT NULL
               OR EXISTS (SELECT 1 FROM activities a WHERE a.deal_id=d.id AND a.vendor_id IS NOT NULL))""").fetchall()
    for r in rows:
        at = r["last_act"] or r["created_at"]
        c.execute("UPDATE deals SET vendor_touched_at=?, vendor_staff_seen_at=? WHERE id=?", (at, at, r["id"]))


# ── 空欄の扱い ──
# 業者が「なし」「不明」「—」などと入れた欄は空欄として扱う（「なし 池田 様」のように出さない）
BLANK_WORDS = {"なし", "無し", "ナシ", "特になし", "とくになし", "不明", "わからない", "分からない", "未確認", "未定", "空欄",
               "-", "--", "—", "――", "―", "ー", "‐", "－", "–", "n/a", "na", "none", "null", "×", "x", "・", "?", "？"}


def blank(v):
    """空っぽ扱いの値なら None、そうでなければ前後の空白を除いた文字"""
    if v is None:
        return None
    s = str(v).strip().strip("　").strip()
    if not s:
        return None
    k = re.sub(r"[\s。．.、,]+$", "", s.lower())
    return None if k in BLANK_WORDS else s


def _clean_ct(ct: dict) -> dict:
    return {k: (blank(v) if isinstance(v, str) or v is None else v) for k, v in ct.items()}


# ── 業者に見せる形 ──
STAGE_ARROW = re.compile(r"^(\S+) → (\S+?)(（.*）)?$")


def _contact(row) -> dict:
    try:
        v = json.loads(row["vendor_contact"] or "{}")
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


# ── こちら側に見せる形（業者の入力そのもの。コピーではなく案件の vendor_contact と業者の記録を毎回読む） ──
STAFF_FIELDS = (("company", "会社名"), ("department", "部署・役職"), ("contact", "担当者"), ("phone", "電話"),
                ("mobile", "携帯"), ("email", "メール"), ("fax", "FAX"), ("zip", "郵便番号"), ("address", "住所"),
                ("industry", "業種"), ("web", "公式サイト"))
# 名刺のどの項目と比べるか（名刺がない案件は、こちらの控え＝案件の会社名・担当者・電話・メールと比べる）
CARD_KEY = {"company": "company", "department": "department_position", "contact": "name", "phone": "phone",
            "mobile": "mobile", "email": "email", "address": "address", "web": "website"}
SNAP_KEY = {"company": "company_snapshot", "contact": "contact_snapshot", "phone": "phone_snapshot", "email": "email_snapshot"}
RESULT_RE = re.compile(r"：(.+)$")


def _same(key: str, a, b) -> bool:
    import unicodedata

    def n(x):
        s = unicodedata.normalize("NFKC", str(x or "")).lower()
        if key == "web":
            s = re.sub(r"^https?://(www\.)?", "", s).rstrip("/")
        if key in ("phone", "mobile", "fax", "zip"):
            return re.sub(r"\D", "", s)
        if key == "contact":
            s = re.sub(r"(様|さま|さん|殿)$", "", re.sub(r"\s+", "", s))
            s = re.sub(r"[（(].*?[)）]", "", s)
        if key == "company":
            s = re.sub(r"(株式会社|有限会社|合同会社|\(株\)|\(有\)|㈱|㈲)", "", s)
        return re.sub(r"[\s　・,、。.\-－ー‐]", "", s)
    return n(a) == n(b)


def _clean_body(body: Optional[str]) -> Optional[str]:
    """記録の本文から「部署・役職: なし」のような空っぽの行を外し、「相手: なし 池田」を「相手: 池田」にする"""
    if not body:
        return body
    out = []
    for line in body.split("\n"):
        m = re.match(r"^(部署・役職|相手|担当者?)\s*[:：]\s*(.*)$", line)
        if m:
            words = [w for w in re.split(r"[ 　]+", m.group(2)) if blank(w)]
            if m.group(1) == "部署・役職":
                if not blank(m.group(2)):
                    continue
            elif not words:
                continue
            else:
                line = f"{m.group(1)}: {' '.join(words)}"
        out.append(line)
    return "\n".join(out).strip() or None


def contact_line(department, contact) -> str:
    """部署と担当者をつないだ1行（空っぽ扱いの値は飛ばす）"""
    return " ".join(x for x in (blank(department), blank(contact)) if x)


def staff_view(c, tenant_id: str, row, card: Optional[dict]) -> Optional[dict]:
    """案件の画面（こちら側）に出す「業者の入力」。業者が記録した案件でなく、業者の記録もなければ None"""
    row = dict(row)
    calls = []
    for a in c.execute("""SELECT id, kind, occurred_at, created_at, title, body, vendor_id FROM activities
            WHERE tenant_id=? AND deal_id=? AND vendor_id IS NOT NULL ORDER BY occurred_at DESC, id DESC""",
                       (tenant_id, row["id"])):
        m = RESULT_RE.search(a["title"] or "")
        calls.append({"id": a["id"], "kind": a["kind"], "occurred_at": a["occurred_at"], "created_at": a["created_at"],
                      "title": a["title"], "result": m.group(1) if m else None, "body": _clean_body(a["body"]),
                      "vendor_id": a["vendor_id"]})
    if not row.get("vendor_id") and not calls and not row.get("vendor_contact"):
        return None
    ct = _clean_ct(_contact(row))
    fields = []
    for key, label in STAFF_FIELDS:
        v = ct.get(key)
        if not v:
            continue
        f = {"key": key, "label": label, "value": v}
        ours, ours_label = None, None
        if card and blank(card.get(CARD_KEY.get(key, ""))):
            ours, ours_label = blank(card.get(CARD_KEY[key])), "名刺"
        elif not card and key in SNAP_KEY and blank(row.get(SNAP_KEY[key])):
            ours, ours_label = blank(row.get(SNAP_KEY[key])), "案件の控え"
        if ours and not _same(key, v, ours):
            f.update(ours=ours, ours_label=ours_label)   # 食い違い：両方並べる（どちらも上書きしない）
        fields.append(f)
    names = sales.vendor_names(c, tenant_id)
    vid = row.get("vendor_id") or next((x["vendor_id"] for x in calls), None)
    try:
        changed = json.loads(row.get("vendor_changed") or "[]")
    except (TypeError, ValueError):
        changed = []
    touched, seen = row.get("vendor_touched_at"), row.get("vendor_staff_seen_at")
    return {"vendor_id": vid, "vendor_name": names.get(vid) if vid else None, "owned": bool(row.get("vendor_id")),
            "fields": fields, "conflicts": sum(1 for f in fields if f.get("ours")),
            "telapo_at": row.get("telapo_at") if row.get("vendor_id") else None,
            "appo_at": row.get("appo_at") if row.get("vendor_id") else None,
            "calls": calls, "touched_at": touched,
            "new": bool(touched and (not seen or touched > seen)),
            "changed": changed if isinstance(changed, list) else []}


def touch(c, deal_id: int, changed: Optional[list] = None):
    """業者が入力した（直した・電話を足した）。こちらの案件に「業者の更新あり」を付ける"""
    row = c.execute("SELECT vendor_changed, vendor_touched_at, vendor_staff_seen_at FROM deals WHERE id=?", (deal_id,)).fetchone()
    prev = []
    if row and row["vendor_touched_at"] and row["vendor_staff_seen_at"] and row["vendor_touched_at"] > row["vendor_staff_seen_at"]:
        try:
            prev = json.loads(row["vendor_changed"] or "[]") or []   # まだこちらが見ていない分に足す
        except (TypeError, ValueError):
            prev = []
    keys = list(dict.fromkeys([*prev, *(changed or [])]))
    c.execute("UPDATE deals SET vendor_touched_at=?, vendor_changed=? WHERE id=?",
              (sales._now(), json.dumps(keys, ensure_ascii=False), deal_id))


def mark_staff_seen(c, tenant_id: str, deal_id: int):
    c.execute("UPDATE deals SET vendor_staff_seen_at=?, vendor_changed=NULL WHERE id=? AND tenant_id=? AND vendor_touched_at IS NOT NULL",
              (sales._now(), deal_id, tenant_id))


def visible_acts(c, vendor_id: int, tenant_id: str, deal_id: int) -> list:
    """業者に見せてよい記録だけ（自分の記録・見せる設定の記録・段階の移り変わり）"""
    out = []
    for a in c.execute("""SELECT id, kind, occurred_at, title, body, vendor_id, vendor_visible, created_at FROM activities
            WHERE tenant_id=? AND deal_id=? ORDER BY occurred_at DESC, id DESC""", (tenant_id, deal_id)):
        if a["kind"] == "stage":
            m = STAGE_ARROW.match(a["title"] or "")
            if not m or m.group(1) not in sales.ALL_STAGES or m.group(2) not in sales.ALL_STAGES:
                continue   # 名刺・担当者・金額などの社内の変更は見せない
            out.append({"id": a["id"], "kind": "stage", "occurred_at": a["occurred_at"], "created_at": a["created_at"],
                        "title": f"段階: {m.group(1)} → {m.group(2)}", "body": None, "mine": False})
        elif a["vendor_id"] == vendor_id:
            out.append({"id": a["id"], "kind": a["kind"], "occurred_at": a["occurred_at"], "created_at": a["created_at"],
                        "title": a["title"], "body": a["body"], "mine": True})
        elif a["vendor_visible"] and not a["vendor_id"]:
            out.append({"id": a["id"], "kind": a["kind"], "occurred_at": a["occurred_at"], "created_at": a["created_at"],
                        "title": a["title"], "body": a["body"], "mine": False})
    return out


def deal_view(c, vendor: dict, row, detail: bool = False, reqs: Optional[list] = None) -> dict:
    """業者に見せる案件。owned=業者が記録した案件。そうでなければ「依頼された会社」（渡した連絡先と依頼のやりとりだけ）"""
    import vendorreq
    owned = row["vendor_id"] == vendor["id"]
    if not owned:
        return _requested_view(c, vendor, row, detail, reqs or [])
    acts = visible_acts(c, vendor["id"], vendor["tenant_id"], row["id"])
    theirs = [a for a in acts if not a["mine"]]
    last_update = max((a["created_at"] or "" for a in theirs), default="")
    last_id = max((a["id"] for a in theirs), default=0)
    seen = c.execute("SELECT last_act_id FROM vendor_seen WHERE vendor_id=? AND deal_id=?", (vendor["id"], row["id"])).fetchone()
    ct = _clean_ct(_contact(row))
    # 会社名は業者が入れたもの。なければ（社内で業者を付けた案件）会社名だけ出す。人・連絡先は出さない
    company = ct.get("company") or (row["company_snapshot"] if "company_snapshot" in row.keys() else "") or ""
    out = {"id": row["id"], "title": ct.get("title") or company, "stage": row["stage"],
           "company": company, "contact": ct.get("contact"),   # 業者が入れたものだけ（名刺の情報は出さない）
           "department": ct.get("department"), "phone": ct.get("phone"), "email": ct.get("email"),
           **{f: ct.get(f) for f, _, _ in EXTRA_FIELDS},
           "telapo_at": row["telapo_at"], "appo_at": row["appo_at"], "created_at": row["created_at"],
           "closed": row["stage"] in sales.CLOSED_STAGES,
           "last_update": last_update or None,
           "updated": bool(last_id and (not seen or last_id > seen["last_act_id"])),
           "latest": next(({"title": a["title"], "occurred_at": a["occurred_at"], "kind": a["kind"]} for a in theirs), None)}
    out["owned"] = True
    out.update(vendorreq.vendor_list_info(c, vendor, row["id"], reqs or []))
    if detail:
        out["activities"] = acts
        out["requests"] = [vendorreq.view(c, r, "vendor") for r in (reqs or [])]
    return out


def _requested_view(c, vendor: dict, row, detail: bool, reqs: list) -> dict:
    import vendorreq
    share = _clean_ct(vendorreq._j(reqs[-1]["share"]) if reqs else {})
    mine = [a for a in visible_acts(c, vendor["id"], vendor["tenant_id"], row["id"]) if a["mine"]]
    last = max([r["updated_at"] for r in reqs] + [a["created_at"] or "" for a in mine], default="")
    out = {"id": row["id"], "title": share.get("company") or "", "stage": None, "owned": False,
           "company": share.get("company") or "", "contact": share.get("contact"), "department": share.get("department"),
           "phone": share.get("phone"), "email": None, "telapo_at": None, "appo_at": None, "created_at": reqs[0]["created_at"] if reqs else row["created_at"],
           "closed": False, "last_update": last or None, "updated": False, "latest": None}
    out.update(vendorreq.vendor_list_info(c, vendor, row["id"], reqs))
    if detail:
        out["activities"] = mine
        out["requests"] = [vendorreq.view(c, r, "vendor") for r in reqs]
    return out


# ── 入力 ──
class LoginIn(BaseModel):
    k: str
    password: str


# 業者が入れる会社の情報（住所など）。こちらの案件にも「業者が入れた会社情報」として出る
EXTRA_FIELDS = (("zip", 12, "郵便番号"), ("address", 200, "住所"), ("industry", 80, "業種"), ("web", 300, "公式サイト"),
                ("fax", 40, "FAX"), ("mobile", 40, "携帯"))


def _extra(body, ct: dict, only_given: bool = False) -> list:
    changed = []
    for f, n, label in EXTRA_FIELDS:
        v = getattr(body, f, None)
        if v is None and only_given:
            continue
        v = sales._clean_text(v, n, label)
        if f == "web" and v and not re.match(r"^https?://", v, re.I):
            v = "https://" + v.lstrip("/")
        ct[f] = v
        changed.append(f)
    return changed


class TelapoIn(BaseModel):
    company: str
    zip: Optional[str] = None
    address: Optional[str] = None
    industry: Optional[str] = None
    web: Optional[str] = None
    fax: Optional[str] = None
    mobile: Optional[str] = None
    contact: Optional[str] = None
    department: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    title: Optional[str] = None
    telapo_at: Optional[str] = None
    result: Optional[str] = None
    appo_at: Optional[str] = None
    next_call_on: Optional[str] = None
    memo: Optional[str] = None
    docs: Optional[vendorreq.DocsIn] = None        # 結果が「資料送付」のとき（宛先・送り方・送り先・資料）


class CallIn(BaseModel):
    telapo_at: Optional[str] = None
    result: Optional[str] = None
    appo_at: Optional[str] = None
    next_call_on: Optional[str] = None
    memo: Optional[str] = None
    docs: Optional[vendorreq.DocsIn] = None


class ContactIn(BaseModel):
    company: Optional[str] = None
    zip: Optional[str] = None
    address: Optional[str] = None
    industry: Optional[str] = None
    web: Optional[str] = None
    fax: Optional[str] = None
    mobile: Optional[str] = None
    contact: Optional[str] = None
    department: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None


class VendorIn(BaseModel):
    name: str


class VendorUpdate(BaseModel):
    name: Optional[str] = None
    active: Optional[bool] = None


def _fire(info: dict):
    for h in VENDOR_HOOKS:
        try:
            h(info)
        except Exception:
            log.exception("業者のお知らせに失敗")


def _mark_seen(c, vendor_id: int, deal_id: int):
    top = c.execute("SELECT COALESCE(MAX(id), 0) m FROM activities WHERE deal_id=?", (deal_id,)).fetchone()["m"]
    c.execute("""INSERT INTO vendor_seen (vendor_id, deal_id, seen_at, last_act_id) VALUES (?,?,?,?)
        ON CONFLICT(vendor_id, deal_id) DO UPDATE SET seen_at=excluded.seen_at, last_act_id=excluded.last_act_id""",
              (vendor_id, deal_id, sales._now(), top))


def _apply_call(c, vendor: dict, row, body, sets: dict) -> str:
    """電話の結果を案件に反映し、記録の件名を返す"""
    result = sales._clean_text(body.result, 20, "結果")
    if result and result not in RESULTS:
        raise HTTPException(400, "結果が不正です")
    appo = sales._clean_dt(body.appo_at, "アポ日時")
    if appo:
        sets["appo_at"] = appo
    tel = sales._clean_dt(body.telapo_at, "架電日時")
    if tel:
        sets["telapo_at"] = tel
    nxt = sales._clean_date(body.next_call_on)
    stage = row["stage"]
    if (result == "アポ獲得" or appo) and stage == "テレアポ":
        sales.change_stage(c, vendor["tenant_id"], row["id"], stage, "アポ", sets, pipeline="sales")
        stage = "アポ"
    if appo and stage == "アポ":
        sets["next_action"] = f"アポ訪問（{vendor['name']}経由）"
        sets["next_action_due"] = appo[:10]
    elif nxt and stage == "テレアポ":
        sets["next_action"] = f"再コール（{vendor['name']}）"
        sets["next_action_due"] = nxt
    return f"テレアポ（{vendor['name']}）" + (f"：{result}" if result else "")


def current_vendor(token: Optional[str] = Cookie(None, alias=COOKIE)) -> dict:
    if not token:
        raise HTTPException(401, "ログインしてください")
    th = hashlib.sha256(token.encode()).hexdigest()
    with sales._conn() as c:
        s = c.execute("SELECT vendor_id, expires_at FROM vendor_sessions WHERE token_hash=?", (th,)).fetchone()
        if not s or s["expires_at"] < sales._now():
            raise HTTPException(401, "ログインし直してください")
        v = c.execute("SELECT * FROM vendors WHERE id=? AND active=1", (s["vendor_id"],)).fetchone()
        if not v:
            raise HTTPException(401, "この入口は使えなくなりました")
    return dict(v)


CURRENT_VENDOR = current_vendor


def build_router(check_session: Callable, get_tenant: Callable) -> tuple:
    admin = APIRouter(prefix="/api/sales/vendors")
    partner = APIRouter(prefix="/api/partner")

    # ── こちら側 ──
    def _get_vendor(c, t, vid):
        row = c.execute("SELECT * FROM vendors WHERE id=? AND tenant_id=?", (vid, t)).fetchone()
        if not row:
            raise HTTPException(404, "業者が見つかりません")
        return row

    @admin.get("")
    def list_vendors(session: dict = Depends(check_session)):
        t = session["tenant_id"]
        out = []
        with sales._conn() as c:
            for v in c.execute("SELECT * FROM vendors WHERE tenant_id=? ORDER BY active DESC, id", (t,)).fetchall():
                ds = c.execute("SELECT stage, amount, appo_at FROM deals WHERE tenant_id=? AND vendor_id=?", (t, v["id"])).fetchall()
                lin = sales.PIPELINES["sales"]["linear"]
                appo = [d for d in ds if d["appo_at"] or (d["stage"] in lin and lin.index(d["stage"]) >= lin.index("アポ"))]
                won = [d for d in ds if d["stage"] == "受注"]
                out.append({"id": v["id"], "name": v["name"], "slug": v["slug"], "active": bool(v["active"]),
                            "created_at": v["created_at"], "last_login_at": v["last_login_at"],
                            "stats": {"total": len(ds), "appo": len(appo), "won": len(won),
                                      "won_amount": sum(d["amount"] or 0 for d in won)}})
        return {"vendors": out}

    @admin.post("")
    def create_vendor(body: VendorIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        name = sales._clean_text(body.name, 60, "業者名")
        if not name:
            raise HTTPException(400, "業者名を入れてください")
        pw = new_password()
        with sales._conn() as c:
            if c.execute("SELECT 1 FROM vendors WHERE tenant_id=? AND name=?", (t, name)).fetchone():
                raise HTTPException(409, "同じ名前の業者があります")
            n = c.execute("SELECT COUNT(*) n FROM vendors WHERE tenant_id=?", (t,)).fetchone()["n"]
            if n >= 50:
                raise HTTPException(400, "業者は50社までです")
            cur = c.execute("""INSERT INTO vendors (tenant_id, name, slug, pw_hash, created_at) VALUES (?,?,?,?,?)""",
                            (t, name, secrets.token_urlsafe(12), hash_pw(pw), sales._now()))
            c.commit()
            row = _get_vendor(c, t, cur.lastrowid)
        return {"id": row["id"], "name": name, "slug": row["slug"], "password": pw}

    @admin.put("/{vid}")
    def update_vendor(vid: int, body: VendorUpdate, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with sales._conn() as c:
            _get_vendor(c, t, vid)
            if body.name is not None:
                name = sales._clean_text(body.name, 60, "業者名")
                if not name:
                    raise HTTPException(400, "業者名を入れてください")
                if c.execute("SELECT 1 FROM vendors WHERE tenant_id=? AND name=? AND id<>?", (t, name, vid)).fetchone():
                    raise HTTPException(409, "同じ名前の業者があります")
                c.execute("UPDATE vendors SET name=? WHERE id=?", (name, vid))
            if body.active is not None:
                c.execute("UPDATE vendors SET active=? WHERE id=?", (1 if body.active else 0, vid))
                if not body.active:
                    _drop_sessions(c, vid)
            c.commit()
        return {"ok": True}

    @admin.post("/{vid}/password")
    def reset_password(vid: int, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        pw = new_password()
        with sales._conn() as c:
            _get_vendor(c, t, vid)
            c.execute("UPDATE vendors SET pw_hash=? WHERE id=?", (hash_pw(pw), vid))
            _drop_sessions(c, vid)
            c.commit()
        return {"password": pw}

    @admin.post("/{vid}/url")
    def reset_url(vid: int, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        slug = secrets.token_urlsafe(12)
        with sales._conn() as c:
            _get_vendor(c, t, vid)
            c.execute("UPDATE vendors SET slug=? WHERE id=?", (slug, vid))
            _drop_sessions(c, vid)
            c.commit()
        return {"slug": slug}

    # ── 業者側 ──
    def own_deal(c, vendor: dict, deal_id: int):
        row = c.execute("SELECT * FROM deals WHERE id=? AND tenant_id=? AND vendor_id=?",
                        (deal_id, vendor["tenant_id"], vendor["id"])).fetchone()
        if not row:
            raise HTTPException(404, "記録が見つかりません")
        return row

    @partner.post("/login")
    def login(body: LoginIn, request: Request, response: Response):
        key = (body.k or "")[:64]
        ip = loginguard.client_ip(request)
        locked, wait = loginguard.check("partner", key, ip, reserve=True)
        if locked:
            raise HTTPException(429, f"パスワードの間違いが続いたため、あと{wait}分ほど待ってからお試しください")
        try:
            return _login(body, response, key, ip)
        finally:
            loginguard.release("partner", key, ip)

    def _login(body: LoginIn, response: Response, key: str, ip: str):
        with sales._conn() as c:
            v = c.execute("SELECT * FROM vendors WHERE slug=? AND active=1", (key,)).fetchone()
            ok = bool(v) and check_pw(body.password, v["pw_hash"])
            if not ok:
                loginguard.fail("partner", key, ip, tenant_id=v["tenant_id"] if v else "",
                                label=f"業者画面（{v['name']}）" if v else "")
                time.sleep(0.4)
                raise HTTPException(401, "URLかパスワードが違います")
            token = secrets.token_urlsafe(32)
            c.execute("DELETE FROM vendor_sessions WHERE expires_at < ?", (sales._now(),))
            c.execute("INSERT INTO vendor_sessions (token_hash, vendor_id, expires_at) VALUES (?,?,?)",
                      (hashlib.sha256(token.encode()).hexdigest(), v["id"],
                       (datetime.now() + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")))
            c.execute("UPDATE vendors SET last_login_at=? WHERE id=?", (sales._now(), v["id"]))
            c.commit()
        loginguard.success("partner", key, ip)
        response.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax",
                            secure=_cookie_secure(), path="/")
        return {"ok": True}

    @partner.post("/logout")
    def logout(response: Response, token: Optional[str] = Cookie(None, alias=COOKIE)):
        if token:
            with sales._conn() as c:
                c.execute("DELETE FROM vendor_sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
                c.commit()
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @partner.get("/me")
    def me(vendor: dict = Depends(current_vendor)):
        disp = ((get_tenant(vendor["tenant_id"]) if get_tenant else None) or {}).get("display_name") or ""
        return {"vendor": vendor["name"], "client": disp, "results": RESULTS,
                "stages": [s for s in sales.PIPELINES["sales"]["stages"]]}

    @partner.get("/calendar")
    def calendar(frm: Optional[str] = None, to: Optional[str] = None, vendor: dict = Depends(current_vendor)):
        """予定が入っている時間帯（名前と時間）。自分が取ったアポは会社名と住所、ほかのアポは場所の目安（市区町村）だけ"""
        import teamcal
        f, t_ = teamcal._range(frm, to)
        with sales._conn() as c:
            return {"events": teamcal.busy(c, vendor["tenant_id"], f, t_, detail=False, vendor_id=vendor["id"]),
                    "people": [{"name": p["name"], "color": p["color"]} for p in teamcal.people(c, vendor["tenant_id"])],
                    "slots": teamcal.SLOTS}

    @partner.get("/deals")
    def deals(vendor: dict = Depends(current_vendor)):
        import vendorreq
        with sales._conn() as c:
            reqs = vendorreq.vendor_deal_ids(c, vendor)
            rows = c.execute("""SELECT * FROM deals WHERE tenant_id=? AND vendor_id=? ORDER BY created_at DESC LIMIT 1000""",
                             (vendor["tenant_id"], vendor["id"])).fetchall()
            have = {r["id"] for r in rows}
            extra = [i for i in reqs if i not in have]
            if extra:
                rows += c.execute(f"SELECT * FROM deals WHERE tenant_id=? AND id IN ({','.join('?' * len(extra))})",
                                  [vendor["tenant_id"], *extra]).fetchall()
            out = [deal_view(c, vendor, r, reqs=reqs.get(r["id"])) for r in rows]
        return {"deals": out}

    def seen_deal(c, vendor: dict, deal_id: int):
        """業者が記録した案件か、依頼を受けている案件"""
        import vendorreq
        row = c.execute("SELECT * FROM deals WHERE id=? AND tenant_id=?", (deal_id, vendor["tenant_id"])).fetchone()
        reqs = vendorreq.vendor_deal_ids(c, vendor).get(deal_id) if row else None
        if not row or (row["vendor_id"] != vendor["id"] and not reqs):
            raise HTTPException(404, "記録が見つかりません")
        return row, reqs or []

    @partner.get("/deals/{deal_id}")
    def deal(deal_id: int, vendor: dict = Depends(current_vendor)):
        with sales._conn() as c:
            row, reqs = seen_deal(c, vendor, deal_id)
            out = deal_view(c, vendor, row, detail=True, reqs=reqs)
            if out["owned"]:
                _mark_seen(c, vendor["id"], deal_id)
            c.commit()
        out["updated"] = False
        return out

    @partner.post("/deals")
    def create(body: TelapoIn, vendor: dict = Depends(current_vendor)):
        t = vendor["tenant_id"]
        company = sales._clean_text(body.company, 120, "会社名")
        if not company:
            raise HTTPException(400, "会社名を入れてください")
        ct = {"company": company, "contact": sales._clean_text(body.contact, 60, "相手の名前"),
              "department": sales._clean_text(body.department, 80, "部署・役職"),
              "phone": sales._clean_text(body.phone, 40, "電話"), "email": sales._clean_text(body.email, 200, "メール")}
        _extra(body, ct)
        memo = sales._clean_text(body.memo, 5000, "電話のメモ")
        with sales._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            today = date.today().isoformat()
            n = c.execute("SELECT COUNT(*) n FROM deals WHERE tenant_id=? AND vendor_id=? AND created_at >= ?",
                          (t, vendor["id"], today)).fetchone()["n"]
            if n >= DAILY_CREATE_LIMIT:
                raise HTTPException(429, "1日に記録できる件数を超えました")
            card = {"id": "", "company": company, "name": ct["contact"], "email": ct["email"], "phone": ct["phone"]}
            title = sales._clean_text(body.title, 120, "件名") or f"{company} テレアポ"
            ct["title"] = title
            deal_id = sales.insert_deal(c, t, card, title, "テレアポ", None, None, None, None,
                                        created_note=f"案件作成（テレアポ・{vendor['name']}）", pipeline="sales", owner=None)
            row = c.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()
            sets = {"vendor_id": vendor["id"], "vendor_contact": json.dumps(ct, ensure_ascii=False)}
            title_act = _apply_call(c, vendor, row, body, sets)
            if "telapo_at" not in sets:
                sets["telapo_at"] = datetime.now().isoformat(timespec="minutes")
            body_txt = "\n".join(x for x in ([f"部署・役職: {ct['department']}"] if blank(ct["department"]) else []) + ([memo] if memo else []))
            act = sales.log_activity(c, t, deal_id, "call", title_act, body_txt or None, sets["telapo_at"] + ":00"
                                     if len(sets["telapo_at"]) == 16 else None)
            c.execute("UPDATE activities SET vendor_id=?, vendor_visible=1 WHERE id=?", (vendor["id"], act))
            sales.apply_sets(c, t, deal_id, sets)
            if body.result == "資料送付":
                vendorreq.create_docs(c, t, deal_id, vendor, vendorreq._clean_docs(body.docs),
                                      {k: ct.get(k) for k in ("company", "contact", "department", "phone")}, memo, sets.get("telapo_at"))
                sales.apply_sets(c, t, deal_id, {"next_action": vendorreq.SEND_DOCS, "next_action_due": date.today().isoformat()})
            _mark_seen(c, vendor["id"], deal_id)
            touch(c, deal_id, ["new"])
            c.commit()
            final = c.execute("SELECT stage, appo_at FROM deals WHERE id=?", (deal_id,)).fetchone()
        _fire({"tenant_id": t, "vendor": vendor["name"], "deal_id": deal_id, "company": company, "new": True,
               "title": title_act, "stage": final["stage"], "appo_at": final["appo_at"]})
        return {"ok": True, "id": deal_id}

    @partner.post("/deals/{deal_id}/calls")
    def add_call(deal_id: int, body: CallIn, vendor: dict = Depends(current_vendor)):
        t = vendor["tenant_id"]
        memo = sales._clean_text(body.memo, 5000, "電話のメモ")
        with sales._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = own_deal(c, vendor, deal_id)
            if row["stage"] in sales.CLOSED_STAGES:
                raise HTTPException(400, "この案件は終わっています")
            today = date.today().isoformat()
            n_all = c.execute("SELECT COUNT(*) n FROM activities WHERE tenant_id=? AND vendor_id=? AND created_at >= ?",
                              (t, vendor["id"], today)).fetchone()["n"]
            n_deal = c.execute("SELECT COUNT(*) n FROM activities WHERE deal_id=? AND vendor_id=? AND created_at >= ?",
                               (deal_id, vendor["id"], today)).fetchone()["n"]
            if n_all >= DAILY_CALL_LIMIT or n_deal >= DAILY_CALL_PER_DEAL:
                raise HTTPException(429, "1日に記録できる件数を超えました")
            before_stage = row["stage"]
            sets = {}
            title_act = _apply_call(c, vendor, row, body, sets)
            if not memo and not body.result and not sets:
                raise HTTPException(400, "結果かメモを入れてください")
            when = sets.get("telapo_at")
            act = sales.log_activity(c, t, deal_id, "call", title_act, memo, (when + ":00") if when and len(when) == 16 else None)
            c.execute("UPDATE activities SET vendor_id=?, vendor_visible=1 WHERE id=?", (vendor["id"], act))
            sales.apply_sets(c, t, deal_id, sets)
            docs_new = False
            if body.result == "資料送付":
                ct = _contact(row)
                vendorreq.create_docs(c, t, deal_id, vendor, vendorreq._clean_docs(body.docs),
                                      {k: ct.get(k) for k in ("company", "contact", "department", "phone")}, memo, when)
                sales.apply_sets(c, t, deal_id, {"next_action": vendorreq.SEND_DOCS, "next_action_due": date.today().isoformat()})
                docs_new = True
            _mark_seen(c, vendor["id"], deal_id)
            touch(c, deal_id, ["call"])
            c.commit()
            final = c.execute("SELECT stage, appo_at, company_snapshot FROM deals WHERE id=?", (deal_id,)).fetchone()
        if final["stage"] != before_stage or docs_new:   # アポが取れたときだけお知らせ（電話の追記ごとには鳴らさない）
            _fire({"tenant_id": t, "vendor": vendor["name"], "deal_id": deal_id, "company": final["company_snapshot"], "new": False,
                   "title": title_act, "stage": final["stage"], "appo_at": final["appo_at"]})
        return {"ok": True}

    @partner.put("/deals/{deal_id}/contact")
    def update_contact(deal_id: int, body: ContactIn, vendor: dict = Depends(current_vendor)):
        """自分が入力した連絡先を直す（こちらの名刺・案件名は変えない）"""
        with sales._conn() as c:
            row = own_deal(c, vendor, deal_id)
            ct = _contact(row)
            prev = dict(ct)
            changed = []
            for f, n, label in (("company", 120, "会社名"), ("contact", 60, "相手の名前"), ("department", 80, "部署・役職"),
                                ("phone", 40, "電話"), ("email", 200, "メール")):
                v = getattr(body, f)
                if v is not None:
                    ct[f] = sales._clean_text(v, n, label)
                    changed.append(f)
            changed += _extra(body, ct, only_given=True)
            if not ct.get("company"):
                raise HTTPException(400, "会社名は空にできません")
            diff = [f for f in dict.fromkeys(changed) if (blank(prev.get(f)) or None) != (blank(ct.get(f)) or None)]
            sets = {"vendor_contact": json.dumps(ct, ensure_ascii=False)}
            if not row["card_id"]:
                # 名刺がつながる前は、こちらの控えも合わせる。ただしこちらで直した項目は上書きしない
                snap = {"company": "company_snapshot", "contact": "contact_snapshot", "email": "email_snapshot", "phone": "phone_snapshot"}
                for f in changed:
                    col = snap.get(f)
                    if col and (row[col] or None) == (prev.get(f) or None):
                        sets[col] = ct.get(f)
            sales.apply_sets(c, vendor["tenant_id"], deal_id, sets)
            if diff:
                # 何をどう直したかを案件の記録に残す（こちらの案件の「業者の入力」にも、業者の画面にも出る）
                labels = dict(STAFF_FIELDS)
                lines = [f"{labels.get(f, f)}: {blank(prev.get(f)) or '（空欄）'} → {blank(ct.get(f)) or '（空欄）'}" for f in diff]
                act = sales.log_activity(c, vendor["tenant_id"], deal_id, "memo",
                                         f"連絡先・会社の情報を直しました（{vendor['name']}）", "\n".join(lines))
                c.execute("UPDATE activities SET vendor_id=?, vendor_visible=1 WHERE id=?", (vendor["id"], act))
                _mark_seen(c, vendor["id"], deal_id)
                touch(c, deal_id, diff)
            c.commit()
        return {"ok": True}

    return admin, partner
