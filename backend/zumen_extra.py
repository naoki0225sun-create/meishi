"""
図面管理の追加機能（phase29）
============================
- 書き込み・スタンプ（原本は変えず、重ねる層として保存）
- QR（印刷した図面から最新版を開く。古い紙なら赤で知らせる）
- 製作指示書のデータ
- 社外への期限つき共有リンク（ログインなしで見られる。期限と取り消しあり）
- 絵柄から似た図面を探す
- AIに質問（書類・図面・単価・メモから答える。根拠の書類を返す）
- 納期から逆算した工程の予定（会社ごとのひな形）
- メールからの取り込み（IMAP。.env に受信箱の設定があるときだけ）
"""

import email
import email.header
import email.utils
import hashlib
import imaplib
import io
import json
import logging
import os
import re
import secrets
import time
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

import sales
import storage
import zumen
import zumen_ai as za
import zumen_match as zm

log = logging.getLogger("zumen_extra")
_conn = sales._conn
_now = zumen._now
_j = zumen._j
_l = zumen._l

STAMPS = ["検図済", "承認", "確認済", "旧版", "社外秘", "製作OK", "保留", "要確認"]
MARK_KINDS = ("stamp", "text", "pen", "rect", "arrow")
SHARE_MAX_DAYS = 90

DEFAULT_STEPS = {
    "prod": [{"label": "承認図提出", "days": 40}, {"label": "部材手配", "days": 30}, {"label": "製作開始", "days": 15},
             {"label": "立会検査", "days": 5}, {"label": "出荷", "days": 1}],
    "sheet": [{"label": "材料手配", "days": 10}, {"label": "加工開始", "days": 6}, {"label": "出荷", "days": 1}],
}


def init_db():
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS dz_marks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, file_id INTEGER NOT NULL, page_no INTEGER NOT NULL,
            kind TEXT NOT NULL, data_json TEXT NOT NULL, author TEXT, created_at TEXT NOT NULL, deleted_at TEXT);
        CREATE INDEX IF NOT EXISTS idx_dz_marks ON dz_marks(file_id, page_no);
        CREATE TABLE IF NOT EXISTS dz_shares (
            id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, token TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
            target_id INTEGER NOT NULL, note TEXT, allow_download INTEGER NOT NULL DEFAULT 0, expires_at TEXT NOT NULL,
            created_by TEXT, created_at TEXT NOT NULL, revoked_at TEXT, views INTEGER NOT NULL DEFAULT 0, last_view_at TEXT);
        CREATE TABLE IF NOT EXISTS dz_templates (
            tenant_id TEXT NOT NULL, company_id INTEGER NOT NULL DEFAULT 0, steps_json TEXT NOT NULL, updated_at TEXT,
            PRIMARY KEY (tenant_id, company_id));
        CREATE TABLE IF NOT EXISTS dz_mail_seen (
            tenant_id TEXT NOT NULL, message_id TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY (tenant_id, message_id));
        """)
        c.commit()


# ════════════════════════════════════════
# 絵柄の特徴（似た図面を探す）
# ════════════════════════════════════════
def feature_of(im) -> str:
    """図面の絵柄を 512 ビットの指紋にする。外枠と余白を除き、線の濃さの配置（16×16）と向き（17×16の差分）を見る"""
    from PIL import Image, ImageOps
    g = ImageOps.grayscale(im)
    g.thumbnail((800, 800))
    inv = ImageOps.invert(g).point(lambda v: 255 if v > 70 else 0)
    box = inv.getbbox()
    if box:
        x0, y0, x1, y1 = box
        mx, my = (x1 - x0) * 0.06, (y1 - y0) * 0.06   # 外枠の線を外す
        g = g.crop((int(x0 + mx), int(y0 + my), int(x1 - mx), int(y1 - my)))
    small = g.resize((16, 16), Image.BILINEAR)
    px = list(small.getdata())
    avg = sum(px) / len(px)
    a = "".join("1" if v < avg else "0" for v in px)
    d = g.resize((17, 16), Image.BILINEAR)
    dp = list(d.getdata())
    b = "".join("1" if dp[r * 17 + c] > dp[r * 17 + c + 1] else "0" for r in range(16) for c in range(16))
    return "%0128x" % int(a + b, 2)


def distance(f1: str, f2: str) -> int:
    return bin(int(f1, 16) ^ int(f2, 16)).count("1")


def ensure_feature(c, file_id: int, page_no: int) -> Optional[str]:
    row = c.execute("SELECT p.feat, f.sha256 FROM dz_pages p JOIN dz_files f ON f.id=p.file_id WHERE p.file_id=? AND p.page_no=?",
                    (file_id, page_no)).fetchone()
    if not row:
        return None
    if row["feat"]:
        return row["feat"]
    raw = storage.get_store().get(storage.page_key(row["sha256"], page_no, "view"))
    if raw is None:
        return None
    from PIL import Image
    f = feature_of(Image.open(io.BytesIO(raw)))
    c.execute("UPDATE dz_pages SET feat=? WHERE file_id=? AND page_no=?", (f, file_id, page_no))
    return f


def similar_by_feature(c, t: str, feat: str, exclude_drawing: int = 0, limit: int = 12) -> list:
    rows = c.execute("""SELECT dr.id, dr.drawing_no, dr.name, dr.material, dr.thickness, co.name company, r.rev, r.file_id, r.page_no
        FROM dz_drawings dr JOIN dz_companies co ON co.id=dr.company_id JOIN dz_revisions r ON r.id=dr.current_rev_id
        WHERE dr.tenant_id=? AND dr.merged_into IS NULL AND dr.id<>?""", (t, exclude_drawing)).fetchall()
    out, budget = [], 400
    for r in rows:
        p = c.execute("SELECT feat FROM dz_pages WHERE file_id=? AND page_no=?", (r["file_id"], r["page_no"] or 1)).fetchone()
        f = p["feat"] if p else None
        if not f and budget > 0:
            budget -= 1
            f = ensure_feature(c, r["file_id"], r["page_no"] or 1)
        if not f:
            continue
        dist = distance(feat, f)
        out.append(dict(r) | {"score": max(0, round(100 - dist / 512 * 200))})
    c.commit()
    out.sort(key=lambda x: -x["score"])
    return [x for x in out if x["score"] >= 40][:limit]


# ════════════════════════════════════════
# 逆算の工程
# ════════════════════════════════════════
def back_business_days(d: date, n: int) -> date:
    while n > 0:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def steps_for(c, t: str, company_id: Optional[int], kind: str) -> list:
    for cid in ([company_id] if company_id else []) + [0]:
        row = c.execute("SELECT steps_json FROM dz_templates WHERE tenant_id=? AND company_id=?", (t, cid)).fetchone()
        if row:
            return _l(row["steps_json"], []) or []
    return DEFAULT_STEPS[kind]


def add_milestones(c, t: str, rows: list, kind: str):
    """納期から逆算した工程の予定日を各行に足す（済みの印は dz_schedule）"""
    today = date.today()
    for x in rows:
        x["milestones"] = []
        if not x.get("due") or not x.get("deal_id"):
            continue
        due = date.fromisoformat(x["due"])
        done = {r["stage"]: r["done_on"] for r in c.execute("SELECT stage, done_on FROM dz_schedule WHERE tenant_id=? AND deal_id=?", (t, x["deal_id"]))}
        for st in steps_for(c, t, x.get("company_id"), kind):
            plan = back_business_days(due, int(st.get("days") or 0))
            dn = done.get(st["label"])
            x["milestones"].append({"label": st["label"], "plan": plan.isoformat(), "done": dn,
                                    "late": not dn and plan < today and x.get("state") != "shipped"})


# ════════════════════════════════════════
# メールからの取り込み
# ════════════════════════════════════════
def mail_accounts() -> dict:
    """.env の DZ_MAIL_<会社ID>_HOST / _USER / _PASS（_PORT・_FOLDER は省略可）"""
    out = {}
    for k, v in os.environ.items():
        m = re.fullmatch(r"DZ_MAIL_(\w+?)_HOST", k)
        if m and v:
            t = m.group(1)
            out[t] = {"host": v, "user": os.getenv(f"DZ_MAIL_{t}_USER", ""), "password": os.getenv(f"DZ_MAIL_{t}_PASS", ""),
                      "port": int(os.getenv(f"DZ_MAIL_{t}_PORT", "993")), "folder": os.getenv(f"DZ_MAIL_{t}_FOLDER", "INBOX")}
    return out


def _dec(v) -> str:
    try:
        return str(email.header.make_header(email.header.decode_header(v or "")))
    except Exception:
        return v or ""


def attachments_of(raw: bytes) -> tuple:
    """メール → (差出人, 件名, Message-ID, [(ファイル名, 中身)])。PDF・画像・CAD だけ"""
    msg = email.message_from_bytes(raw)
    sender = email.utils.parseaddr(_dec(msg.get("From")))[1]
    subject = _dec(msg.get("Subject"))[:200]
    mid = (msg.get("Message-ID") or hashlib.sha256(raw[:4096]).hexdigest())[:200]
    files = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        name = _dec(part.get_filename() or "")
        ctype = part.get_content_type()
        if not name and ctype == "application/pdf":
            name = "attachment.pdf"
        if not name:
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext not in {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic"} | zumen.CAD_EXT and not ctype.startswith("image/") and ctype != "application/pdf":
            continue
        data = part.get_payload(decode=True) or b""
        if data:
            files.append((os.path.basename(name)[:200], data))
    return sender, subject, mid, files


def poll_mail_once() -> int:
    n = 0
    for t, a in mail_accounts().items():
        try:
            box = imaplib.IMAP4_SSL(a["host"], a["port"])
            box.login(a["user"], a["password"])
            box.select(a["folder"])
            typ, ids = box.search(None, "UNSEEN")
            for num in (ids[0].split() if ids and ids[0] else [])[:30]:
                typ, data = box.fetch(num, "(RFC822)")
                if typ != "OK" or not data or not isinstance(data[0], tuple):
                    continue
                n += ingest_mail(t, data[0][1])
            box.logout()
        except Exception:
            log.exception("メールの取り込みに失敗（%s）", t)
    return n


def ingest_mail(t: str, raw: bytes) -> int:
    sender, subject, mid, files = attachments_of(raw)
    with _conn() as c:
        if c.execute("SELECT 1 FROM dz_mail_seen WHERE tenant_id=? AND message_id=?", (t, mid)).fetchone():
            return 0
        c.execute("INSERT INTO dz_mail_seen (tenant_id, message_id, at) VALUES (?,?,?)", (t, mid, _now()))
        c.commit()
    hint = {"mail_from": sender, "mail_subject": subject}
    dom = zm.mail_domain(sender)
    if dom:
        hint["mail_domain"] = dom
    n = 0
    for name, data in files:
        r = zumen.ingest_bytes(t, name, data, "mail", hint, sender[:40] or None)
        n += r["result"] == "queued"
    if n:
        zumen.kick()
    return n


# ════════════════════════════════════════
# ルーター
# ════════════════════════════════════════
def build_router(check_session: Callable, fetch_cards: Callable, get_tenant: Optional[Callable] = None, main_router=None) -> tuple:
    r = APIRouter(prefix="/api/zumen")
    pub = APIRouter(prefix="/api/zumen-share")

    def own(t):
        return ((get_tenant(t) or {}).get("display_name") or t) if get_tenant else t

    def actor_of(a):
        return (a or "").strip()[:40] or None

    def file_of(c, t, fid):
        f = c.execute("SELECT * FROM dz_files WHERE id=? AND tenant_id=?", (fid, t)).fetchone()
        if not f:
            raise HTTPException(404, "ファイルが見つかりません")
        return f

    # ── 書き込み・スタンプ ──
    @r.get("/files/{fid}/marks")
    def marks(fid: int, page: int = 1, session: dict = Depends(check_session)):
        with _conn() as c:
            file_of(c, session["tenant_id"], fid)
            rows = c.execute("""SELECT * FROM dz_marks WHERE file_id=? AND page_no=? AND deleted_at IS NULL ORDER BY id""", (fid, page)).fetchall()
        return {"marks": [{"id": x["id"], "kind": x["kind"], "data": _l(x["data_json"], {}), "author": x["author"], "created_at": x["created_at"]} for x in rows],
                "stamps": STAMPS}

    class MarkIn(BaseModel):
        page: int = 1
        kind: str
        data: dict
        author: Optional[str] = None

    def clean_mark(kind: str, d: dict) -> dict:
        if kind not in MARK_KINDS:
            raise HTTPException(400, "書き込みの種類が正しくありません")
        num = lambda v: max(-0.2, min(1.2, float(v)))
        out = {}
        try:
            for k in ("x", "y", "w", "h", "x2", "y2"):
                if k in d:
                    out[k] = num(d[k])
            if "points" in d:
                out["points"] = [[num(p[0]), num(p[1])] for p in d["points"][:2000]]
        except (TypeError, ValueError, IndexError):
            raise HTTPException(400, "書き込む位置が正しくありません")
        out["text"] = str(d.get("text") or "")[:200]
        out["color"] = d.get("color") if d.get("color") in ("red", "blue", "green", "black", "orange") else "red"
        out["size"] = max(0.5, min(4.0, float(d.get("size") or 1)))
        if kind == "stamp" and not out["text"]:
            raise HTTPException(400, "スタンプの文字を選択してください")
        return out

    @r.post("/files/{fid}/marks")
    def add_mark(fid: int, body: MarkIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        data = clean_mark(body.kind, body.data)
        with _conn() as c:
            file_of(c, t, fid)
            mid = c.execute("""INSERT INTO dz_marks (tenant_id, file_id, page_no, kind, data_json, author, created_at) VALUES (?,?,?,?,?,?,?)""",
                            (t, fid, body.page, body.kind, _j(data), actor_of(body.author), _now())).lastrowid
            c.commit()
        return {"ok": True, "id": mid}

    @r.delete("/marks/{mid}")
    def del_mark(mid: int, session: dict = Depends(check_session)):
        with _conn() as c:
            if c.execute("UPDATE dz_marks SET deleted_at=? WHERE id=? AND tenant_id=?", (_now(), mid, session["tenant_id"])).rowcount == 0:
                raise HTTPException(404, "見つかりません")
            c.commit()
        return {"ok": True}

    @r.post("/marks/{mid}/restore")
    def restore_mark(mid: int, session: dict = Depends(check_session)):
        with _conn() as c:
            if c.execute("UPDATE dz_marks SET deleted_at=NULL WHERE id=? AND tenant_id=?", (mid, session["tenant_id"])).rowcount == 0:
                raise HTTPException(404, "見つかりません")
            c.commit()
        return {"ok": True}

    # ── QR ──
    @r.get("/qr")
    def qr(u: str, request: Request, session: dict = Depends(check_session)):
        if not re.fullmatch(r"/zumen(-share)?\.html\?[\w=&%-]{1,200}", u):
            raise HTTPException(400, "QRコードにできないリンク先です")
        base = os.getenv("APP_URL") or str(request.base_url).rstrip("/")
        import segno
        b = io.BytesIO()
        segno.make(base + u, error="m").save(b, kind="png", scale=8, border=2)
        return Response(b.getvalue(), media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})

    @r.get("/revisions/{rid}/check")
    def rev_check(rid: int, session: dict = Depends(check_session)):
        """印刷した図面のQRから：この版が今も最新かどうか"""
        with _conn() as c:
            rv = c.execute("SELECT * FROM dz_revisions WHERE id=? AND tenant_id=?", (rid, session["tenant_id"])).fetchone()
            if not rv:
                raise HTTPException(404, "版が見つかりません")
            cur = zumen.current_rev(c, rv["drawing_id"])
        return {"drawing_id": rv["drawing_id"], "rev": rv["rev"], "is_current": bool(cur and cur["id"] == rid),
                "current": {"id": cur["id"], "rev": cur["rev"]} if cur else None}

    # ── 製作指示書 ──
    @r.get("/deals/{deal_id}/instruction")
    def instruction(deal_id: int, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            d = c.execute("SELECT * FROM deals WHERE id=? AND tenant_id=?", (deal_id, t)).fetchone()
            if not d:
                raise HTTPException(404, "案件が見つかりません")
            docs = c.execute("""SELECT dd.* FROM dz_links l JOIN dz_docs dd ON dd.id=l.doc_id WHERE l.tenant_id=? AND l.target_type='deal'
                AND l.target_id=? AND dd.status='confirmed'""", (t, deal_id)).fetchall()
            orders = [x for x in docs if x["kind"] in ("注文書", "注文変更書")]
            lines, dids = [], {}
            for o in orders:
                for ln in c.execute("SELECT * FROM dz_lines WHERE doc_id=? ORDER BY line_no", (o["id"],)):
                    lines.append({k: ln[k] for k in ("drawing_no", "rev", "name", "material", "thickness", "qty", "due_on", "drawing_id")})
                    if ln["drawing_id"]:
                        dids[ln["drawing_id"]] = ln["rev"]
            for x in docs:
                for rv in c.execute("SELECT drawing_id FROM dz_revisions WHERE doc_id=?", (x["id"],)):
                    dids.setdefault(rv["drawing_id"], None)
            drawings = []
            for ln in lines:
                if ln["drawing_id"] and not (ln["material"] and ln["thickness"]):
                    dr0 = c.execute("SELECT material, thickness FROM dz_drawings WHERE id=?", (ln["drawing_id"],)).fetchone()
                    if dr0:
                        ln["material"] = ln["material"] or dr0["material"]
                        ln["thickness"] = ln["thickness"] or dr0["thickness"]
            for did, orev in dids.items():
                dr = c.execute("SELECT * FROM dz_drawings WHERE id=?", (did,)).fetchone()
                cur = zumen.current_rev(c, did)
                notes = [dict(n) for n in c.execute("""SELECT kind, body, created_at FROM dz_notes WHERE drawing_id=? AND deleted_at IS NULL
                    AND kind IN ('不具合','加工メモ','ノウハウ') ORDER BY id DESC LIMIT 5""", (did,))]
                drawings.append({"id": did, "drawing_no": dr["drawing_no"], "name": dr["name"], "material": dr["material"], "thickness": dr["thickness"],
                                 "surface": dr["surface"], "rev": cur["rev"] if cur else None, "rev_id": cur["id"] if cur else None,
                                 "file_id": cur["file_id"] if cur else None, "page_no": cur["page_no"] if cur else None, "ordered_rev": orev,
                                 "rev_mismatch": bool(orev and cur and zm.compare_rev(orev, cur["rev"]) not in (0, None)), "notes": notes})
            co = None
            if orders and orders[0]["company_id"]:
                row = c.execute("SELECT name FROM dz_companies WHERE id=?", (orders[0]["company_id"],)).fetchone()
                co = row["name"] if row else None
            due = min([o["due_on"] for o in orders if o["due_on"]] or [d["delivery_on"]] or [None]) if orders else d["delivery_on"]
            sched = [{"deal_id": deal_id, "due": due, "company_id": orders[0]["company_id"] if orders else None, "state": "ok"}]
            kind = "prod" if (main_router and main_router.has_prod(t)) else "sheet"
            add_milestones(c, t, sched, kind)
        return {"deal": {"id": d["id"], "title": d["title"], "stage": d["stage"], "owner": d["owner"], "site_name": d["site_name"]},
                "company": co or d["company_snapshot"], "due": due, "order_nos": [o["doc_no"] for o in orders if o["doc_no"]],
                "lines": lines, "drawings": sorted(drawings, key=lambda x: x["drawing_no"]), "milestones": sched[0]["milestones"],
                "own": own(t), "printed_at": _now()}

    # ── 逆算の工程 ──
    class StepsIn(BaseModel):
        company_id: int = 0
        steps: list

    @r.get("/templates")
    def templates(session: dict = Depends(check_session)):
        t = session["tenant_id"]
        kind = "prod" if (main_router and main_router.has_prod(t)) else "sheet"
        with _conn() as c:
            rows = {x["company_id"]: _l(x["steps_json"], []) for x in c.execute("SELECT * FROM dz_templates WHERE tenant_id=?", (t,))}
            names = {x["id"]: x["name"] for x in c.execute("SELECT id, name FROM dz_companies WHERE tenant_id=?", (t,))}
        return {"default": rows.get(0) or DEFAULT_STEPS[kind], "is_custom_default": 0 in rows,
                "companies": [{"company_id": k, "name": names.get(k, "?"), "steps": v} for k, v in rows.items() if k]}

    @r.put("/templates")
    def put_template(body: StepsIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        steps = []
        for s in body.steps[:12]:
            label = str((s or {}).get("label") or "").strip()[:20]
            try:
                days = int((s or {}).get("days"))
            except (TypeError, ValueError):
                raise HTTPException(400, "日数は数字で入力してください")
            if label and 0 <= days <= 250:
                steps.append({"label": label, "days": days})
        steps.sort(key=lambda x: -x["days"])
        with _conn() as c:
            if not steps:
                c.execute("DELETE FROM dz_templates WHERE tenant_id=? AND company_id=?", (t, body.company_id))
            else:
                c.execute("""INSERT INTO dz_templates (tenant_id, company_id, steps_json, updated_at) VALUES (?,?,?,?)
                    ON CONFLICT(tenant_id, company_id) DO UPDATE SET steps_json=excluded.steps_json, updated_at=excluded.updated_at""",
                          (t, body.company_id, _j(steps), _now()))
            c.commit()
        return {"ok": True, "steps": steps}

    class DoneIn(BaseModel):
        deal_id: int
        label: str
        done: bool = True

    @r.post("/schedule/done")
    def step_done(body: DoneIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            if not c.execute("SELECT 1 FROM deals WHERE id=? AND tenant_id=?", (body.deal_id, t)).fetchone():
                raise HTTPException(404, "案件が見つかりません")
            c.execute("DELETE FROM dz_schedule WHERE tenant_id=? AND deal_id=? AND stage=?", (t, body.deal_id, body.label[:20]))
            if body.done:
                c.execute("INSERT INTO dz_schedule (tenant_id, deal_id, stage, done_on) VALUES (?,?,?,?)",
                          (t, body.deal_id, body.label[:20], date.today().isoformat()))
            c.commit()
        return {"ok": True}

    # ── 似た図面（絵柄） ──
    @r.get("/drawings/{did}/similar-image")
    def similar_image(did: int, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            if not c.execute("SELECT 1 FROM dz_drawings WHERE id=? AND tenant_id=?", (did, t)).fetchone():
                raise HTTPException(404, "図面が見つかりません")
            cur = zumen.current_rev(c, did)
            if not cur:
                return {"items": [], "note": "図面の版が登録されていないため、比較できません"}
            f = ensure_feature(c, cur["file_id"], cur["page_no"] or 1)
            if not f:
                return {"items": [], "note": "図面の画像がありません"}
            return {"items": similar_by_feature(c, t, f, did)}

    @r.post("/search-image")
    async def search_image(file: UploadFile = File(...), session: dict = Depends(check_session)):
        """手元の図面・写真に似た図面を探す（登録はしない）"""
        t = session["tenant_id"]
        data = await file.read(40 * 1024 * 1024 + 1)
        mime = zumen.sniff(data, file.filename or "")
        try:
            if mime == "application/pdf":
                import tempfile
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                    tf.write(data)
                    path = tf.name
                try:
                    im = zumen._pdf_render(path, 1, 80)
                finally:
                    os.unlink(path)
            elif mime.startswith("image/"):
                im = zumen._open_images(data, mime)[0]
            else:
                raise HTTPException(400, "PDFか画像ファイルを選択してください")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(400, "画像を読み込めませんでした")
        with _conn() as c:
            return {"items": similar_by_feature(c, t, feature_of(im), 0, 18)}

    # ── AIに質問 ──
    class AskIn(BaseModel):
        q: str

    @r.post("/ask")
    def ask(body: AskIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        q = body.q.strip()[:500]
        if not q:
            raise HTTPException(400, "質問を入力してください")
        if not za.ai_available():
            raise HTTPException(400, "AIの設定（APIキー）が済んでいません")
        with _conn() as c:
            ctx, sources = build_context(c, t, q)
        if not ctx:
            return {"answer": "手がかりになる書類や図面が見つかりませんでした。図番・会社名・品名など、書類に書かれている言葉を入れて聞いてみてください。", "sources": []}
        prompt = ("あなたは製造業の事務担当です。下の社内データだけを根拠に、質問に日本語で簡潔に答えてください。"
                  "金額・日付・版は書いてあるとおりに。根拠にした書類は [書類#番号]、図番は [図面#番号] の形で文中に示してください。"
                  "データにないことは「データにはありません」と答え、推測しないでください。\n\n"
                  f"質問: {q}\n\n社内データ:\n{ctx}")
        text = za.call_ai([{"type": "text", "text": prompt}], max_tokens=1500)
        used = set(re.findall(r"\[(書類|図面)#(\d+)\]", text))
        srcs = [s for s in sources if (("書類" if s["type"] == "doc" else "図面"), str(s["id"])) in used] or sources[:5]
        return {"answer": text.strip(), "sources": srcs}

    def build_context(c, t: str, q: str) -> tuple:
        words = [w for w in re.split(r"[\s　、。？?！!「」]+", q) if len(w) >= 2][:8]
        ids = []
        for w in words + [q]:
            for i in zumen.search_doc_ids(c, t, w, 30):
                if i not in ids:
                    ids.append(i)
        lines, sources = [], []
        for i in ids[:25]:
            d = c.execute("""SELECT d.*, co.name company FROM dz_docs d LEFT JOIN dz_companies co ON co.id=d.company_id
                WHERE d.id=? AND d.status='confirmed'""", (i,)).fetchone()
            if not d:
                continue
            ls = c.execute("SELECT drawing_no, rev, name, material, thickness, qty, unit_price, amount, due_on FROM dz_lines WHERE doc_id=?", (i,)).fetchall()
            desc = f"[書類#{i}] {d['kind']}（{d['direction']}） 会社:{d['company'] or '?'} 番号:{d['doc_no'] or '-'} 日付:{d['doc_date'] or '-'} 金額(税抜):{d['amount'] if d['amount'] is not None else '-'} 納期:{d['due_on'] or '-'} 件名:{d['title'] or '-'}"
            for x in ls[:15]:
                desc += f"\n  明細: 図番{x['drawing_no'] or '-'} 版{x['rev'] or '-'} {x['name'] or ''} {x['material'] or ''} t{x['thickness'] or '-'} 数量{x['qty'] or '-'} 単価{x['unit_price'] if x['unit_price'] is not None else '-'}"
            lines.append(desc)
            sources.append({"type": "doc", "id": i, "label": f"{d['kind']} {d['doc_no'] or d['title'] or ''} {d['company'] or ''}".strip()})
        like = [f"%{w}%" for w in words[:4]]
        if like:
            cond = " OR ".join(["dr.drawing_no LIKE ? OR dr.name LIKE ? OR dr.material LIKE ? OR co.name LIKE ?"] * len(like))
            args = [x for w in like for x in (w, w, w, w)]
            for dr in c.execute(f"""SELECT dr.*, co.name company FROM dz_drawings dr JOIN dz_companies co ON co.id=dr.company_id
                    WHERE dr.tenant_id=? AND dr.merged_into IS NULL AND ({cond}) LIMIT 15""", [t] + args).fetchall():
                cur = zumen.current_rev(c, dr["id"])
                notes = c.execute("SELECT kind, body FROM dz_notes WHERE drawing_id=? AND deleted_at IS NULL ORDER BY id DESC LIMIT 3", (dr["id"],)).fetchall()
                desc = f"[図面#{dr['id']}] 図番:{dr['drawing_no']} 会社:{dr['company']} 品名:{dr['name'] or '-'} 材質:{dr['material'] or '-'} 板厚:{dr['thickness'] or '-'} 表面処理:{dr['surface'] or '-'} 最新版:{(cur['rev'] or '初版') if cur else 'なし'}"
                for n in notes:
                    desc += f"\n  メモ({n['kind']}): {n['body']}"
                lines.append(desc)
                sources.append({"type": "drawing", "id": dr["id"], "label": f"{dr['drawing_no']} {dr['name'] or ''}".strip()})
        return "\n".join(lines)[:24000], sources

    # ── 共有リンク ──
    class ShareIn(BaseModel):
        kind: str          # drawing | doc
        target_id: int
        days: int = 7
        allow_download: bool = False
        note: Optional[str] = None
        actor: Optional[str] = None

    @r.post("/shares")
    def create_share(body: ShareIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        if body.kind not in ("drawing", "doc"):
            raise HTTPException(400, "共有できるのは図面か書類だけです")
        days = max(1, min(SHARE_MAX_DAYS, body.days))
        with _conn() as c:
            tbl = "dz_drawings" if body.kind == "drawing" else "dz_docs"
            if not c.execute(f"SELECT 1 FROM {tbl} WHERE id=? AND tenant_id=?", (body.target_id, t)).fetchone():
                raise HTTPException(404, "共有するものが見つかりません")
            token = secrets.token_urlsafe(24)
            exp = (datetime.now() + timedelta(days=days)).isoformat(timespec="seconds")
            sid = c.execute("""INSERT INTO dz_shares (tenant_id, token, kind, target_id, note, allow_download, expires_at, created_by, created_at)
                VALUES (?,?,?,?,?,?,?,?,?)""", (t, token, body.kind, body.target_id, (body.note or "")[:200], 1 if body.allow_download else 0,
                                                exp, actor_of(body.actor), _now())).lastrowid
            zumen.event(c, t, actor_of(body.actor), "share", f"{body.kind}:{body.target_id}", {"share_id": sid, "days": days})
            c.commit()
        return {"ok": True, "id": sid, "url": f"/zumen-share.html?t={token}", "expires_at": exp}

    @r.get("/shares")
    def list_shares(kind: str, target_id: int, session: dict = Depends(check_session)):
        with _conn() as c:
            rows = c.execute("""SELECT id, token, note, allow_download, expires_at, created_by, created_at, revoked_at, views, last_view_at
                FROM dz_shares WHERE tenant_id=? AND kind=? AND target_id=? ORDER BY id DESC""", (session["tenant_id"], kind, target_id)).fetchall()
        now = _now()
        return {"shares": [dict(x) | {"url": f"/zumen-share.html?t={x['token']}", "active": not x["revoked_at"] and x["expires_at"] > now} for x in rows]}

    @r.delete("/shares/{sid}")
    def revoke_share(sid: int, session: dict = Depends(check_session)):
        with _conn() as c:
            if c.execute("UPDATE dz_shares SET revoked_at=? WHERE id=? AND tenant_id=?", (_now(), sid, session["tenant_id"])).rowcount == 0:
                raise HTTPException(404, "見つかりません")
            c.commit()
        return {"ok": True}

    def share_of(c, token: str):
        s = c.execute("SELECT * FROM dz_shares WHERE token=?", (token,)).fetchone()
        if not s or s["revoked_at"] or s["expires_at"] < _now():
            raise HTTPException(404, "このリンクは期限が切れたか、取り消されています")
        return s

    def share_pages(c, s) -> list:
        """共有で見せてよいページ [(file_id, page_no, label)]"""
        if s["kind"] == "drawing":
            cur = zumen.current_rev(c, resolve_drawing(c, s["target_id"]))
            return [(cur["file_id"], cur["page_no"] or 1, "")] if cur else []
        d = c.execute("SELECT * FROM dz_docs WHERE id=?", (s["target_id"],)).fetchone()
        if not d or d["status"] != "confirmed":
            return []
        return [(d["file_id"], p, "") for p in range(d["page_from"] or 1, (d["page_to"] or d["page_from"] or 1) + 1)]

    @pub.get("/{token}")
    def share_meta(token: str):
        with _conn() as c:
            s = share_of(c, token)
            pages = share_pages(c, s)
            c.execute("UPDATE dz_shares SET views=views+1, last_view_at=? WHERE id=?", (_now(), s["id"]))
            c.commit()
            if s["kind"] == "drawing":
                did = resolve_drawing(c, s["target_id"])
                dr = c.execute("SELECT drawing_no, name FROM dz_drawings WHERE id=?", (did,)).fetchone()
                cur = zumen.current_rev(c, did)
                title = f"{dr['drawing_no']} {dr['name'] or ''}".strip()
                sub = f"版 {cur['rev'] or '初版'}" if cur else ""
            else:
                d = c.execute("SELECT kind, doc_no, title FROM dz_docs WHERE id=?", (s["target_id"],)).fetchone()
                title = f"{d['kind']} {d['doc_no'] or d['title'] or ''}".strip()
                sub = ""
            sender = own(s["tenant_id"])
        return {"title": title, "sub": sub, "from": sender, "note": s["note"], "expires_at": s["expires_at"],
                "pages": len(pages), "allow_download": bool(s["allow_download"])}

    @pub.get("/{token}/page/{n}")
    def share_page(token: str, n: int, size: str = "view"):
        if size not in ("view", "hi", "thumb"):
            raise HTTPException(400, "画像の大きさの指定が正しくありません")
        with _conn() as c:
            s = share_of(c, token)
            pages = share_pages(c, s)
            if not (1 <= n <= len(pages)):
                raise HTTPException(404, "指定したページがありません")
            fid, pno, _ = pages[n - 1]
            f = c.execute("SELECT * FROM dz_files WHERE id=?", (fid,)).fetchone()
        st = storage.get_store()
        data = st.get(storage.page_key(f["sha256"], pno, size)) or st.get(storage.page_key(f["sha256"], pno, "view"))
        if data is None:
            raise HTTPException(404, "画像がありません")
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=600", "X-Robots-Tag": "noindex"})

    @pub.get("/{token}/raw")
    def share_raw(token: str):
        with _conn() as c:
            s = share_of(c, token)
            if not s["allow_download"]:
                raise HTTPException(403, "ダウンロードは許可されていません")
            pages = share_pages(c, s)
            if not pages:
                raise HTTPException(404, "ファイルがありません")
            f = c.execute("SELECT * FROM dz_files WHERE id=?", (pages[0][0],)).fetchone()
        data = storage.get_store().get(f["storage_key"])
        from urllib.parse import quote
        return Response(data, media_type=f["mime"] if f["mime"] != "application/cad" else "application/octet-stream",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(f['filename'] or 'file')}", "X-Robots-Tag": "noindex"})

    # ── メール取り込みの状態 ──
    @r.get("/mail/status")
    def mail_status(session: dict = Depends(check_session)):
        t = session["tenant_id"]
        a = mail_accounts().get(t)
        with _conn() as c:
            n = c.execute("SELECT COUNT(*) n FROM dz_ingest WHERE tenant_id=? AND source='mail'", (t,)).fetchone()["n"]
            last = c.execute("SELECT MAX(at) a FROM dz_mail_seen WHERE tenant_id=?", (t,)).fetchone()["a"]
        return {"configured": bool(a), "address": a["user"] if a else None, "received": n, "last": last}

    return r, pub


# ワーカーの合間にメールを見る（5分ごと）
_MAIL = {"last": 0.0}


def resolve_drawing(c, did: int) -> int:
    """まとめた図番は、まとめ先の図番を返す（共有リンク・印刷したQRを生かすため）"""
    for _ in range(5):
        m = c.execute("SELECT merged_into FROM dz_drawings WHERE id=?", (did,)).fetchone()
        if not m or not m["merged_into"]:
            break
        did = m["merged_into"]
    return did


_DISK = {"day": ""}


def check_disk(now: datetime = None):
    """ディスクが70%を超えたら1日1回Slackで知らせる（R2へ切り替えるかを決める合図）"""
    now = now or datetime.now()
    day = now.strftime("%Y-%m-%d")
    if _DISK["day"] == day:
        return None
    _DISK["day"] = day
    try:
        u = storage.disk_usage()
    except Exception:
        return None
    ratio = (u.get("used") or 0) / (u.get("total") or 1)
    if ratio < float(os.getenv("DZ_DISK_WARN", "0.7")):
        return None
    text = (f"*図面の保存先のディスクが {ratio:.0%} になりました*（{u['used'] / 1024 ** 3:.0f}GB / {u['total'] / 1024 ** 3:.0f}GB）。"
            "R2へ切り替えるか、ディスクを広げる時期です")
    try:
        import slack_bot
        with _conn() as c:
            ts = [r["tenant_id"] for r in c.execute("SELECT DISTINCT tenant_id FROM dz_files")]
        for t in ts:
            ch = slack_bot.tenant_channel(t)
            if ch and slack_bot.token_for_channel(ch):
                slack_bot.post(ch, None, text)
                break
    except Exception:
        log.exception("ディスクの知らせに失敗")
    return ratio


def tick():
    check_disk()
    if not mail_accounts():
        return
    if time.time() - _MAIL["last"] < int(os.getenv("DZ_MAIL_INTERVAL", "300")):
        return
    _MAIL["last"] = time.time()
    poll_mail_once()
