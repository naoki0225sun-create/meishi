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


# ── 業者に見せる形 ──
STAGE_ARROW = re.compile(r"^(\S+) → (\S+?)(（.*）)?$")


def _contact(row) -> dict:
    try:
        v = json.loads(row["vendor_contact"] or "{}")
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


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
    ct = _contact(row)
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
    share = vendorreq._j(reqs[-1]["share"]) if reqs else {}
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
            body_txt = "\n".join(x for x in ([f"部署・役職: {ct['department']}"] if ct["department"] else []) + ([memo] if memo else []))
            act = sales.log_activity(c, t, deal_id, "call", title_act, body_txt or None, sets["telapo_at"] + ":00"
                                     if len(sets["telapo_at"]) == 16 else None)
            c.execute("UPDATE activities SET vendor_id=?, vendor_visible=1 WHERE id=?", (vendor["id"], act))
            sales.apply_sets(c, t, deal_id, sets)
            if body.result == "資料送付":
                vendorreq.create_docs(c, t, deal_id, vendor, vendorreq._clean_docs(body.docs),
                                      {k: ct.get(k) for k in ("company", "contact", "department", "phone")}, memo, sets.get("telapo_at"))
                sales.apply_sets(c, t, deal_id, {"next_action": vendorreq.SEND_DOCS, "next_action_due": date.today().isoformat()})
            _mark_seen(c, vendor["id"], deal_id)
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
            sets = {"vendor_contact": json.dumps(ct, ensure_ascii=False)}
            if not row["card_id"]:
                # 名刺がつながる前は、こちらの控えも合わせる。ただしこちらで直した項目は上書きしない
                snap = {"company": "company_snapshot", "contact": "contact_snapshot", "email": "email_snapshot", "phone": "phone_snapshot"}
                for f in changed:
                    col = snap.get(f)
                    if col and (row[col] or None) == (prev.get(f) or None):
                        sets[col] = ct.get(f)
            sales.apply_sets(c, vendor["tenant_id"], deal_id, sets)
            c.commit()
        return {"ok": True}

    return admin, partner
