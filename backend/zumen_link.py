"""図面タブと名刺・営業・グループ会社とのつなぎ込み（phase30）

- 名刺の会社ごとの図面・書類の件数（名刺の画面に出す）
- 受注後の進行ボードのカードに出す図面の縮小画像
- 営業で作った見積書の控えを、図面タブの書類として残す
- グループ会社への依頼に紐づく図面を、依頼を受けた側から読み取り専用で見る
"""
import hashlib
import re
from typing import Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

import sales
import storage
import zumen
import zumen_match as zm
from sales import _conn

MAX_HTML = 2 * 1024 * 1024
# グループ会社に見せる書類の種類（金額の入った見積書・請求書・発注書は見せない）
GROUP_KINDS = ("図面", "注文書", "注文変更書", "検査成績書", "写真", "承認図", "仕様書")
GROUP_KINDS_SQL = ",".join(f"'{k}'" for k in GROUP_KINDS)


def _clean_html(html: str) -> str:
    """控えに残すHTMLからスクリプトと外部への読み込みを落とす（表示側でも sandbox で止めている）"""
    h = re.sub(r"(?is)<script\b.*?</script>", "", html or "")
    h = re.sub(r"(?is)<(iframe|object|embed|link|meta)\b[^>]*>", "", h)
    h = re.sub(r"(?i)\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", h)
    h = re.sub(r"(?i)contenteditable(=\"[^\"]*\")?", "", h)
    return h


def quote_image(company: str, doc_no: str, doc_date: str, amount, title: str, lines: list):
    """見積書の控えの縮小画像（一覧の小さな画像・似た図面の対象外）。本体はHTMLのまま見せる"""
    from PIL import Image, ImageDraw
    import zumen_dxf
    W, H = 1240, 1754
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    ink, sub = (20, 24, 32), (90, 98, 112)
    d.text((W // 2, 150), "御見積書", fill=ink, font=zumen_dxf._font(64), anchor="mm")
    d.text((110, 300), f"{company} 御中", fill=ink, font=zumen_dxf._font(40))
    d.line([(110, 356), (720, 356)], fill=ink, width=3)
    d.text((780, 300), f"見積番号 {doc_no or ''}", fill=sub, font=zumen_dxf._font(28))
    d.text((780, 345), f"発行日 {doc_date or ''}", fill=sub, font=zumen_dxf._font(28))
    if amount is not None:
        d.text((110, 430), f"御見積金額（税抜）  ¥{int(amount):,}", fill=ink, font=zumen_dxf._font(44))
    d.text((110, 520), f"件名  {title or ''}", fill=ink, font=zumen_dxf._font(32))
    y = 620
    d.line([(110, y), (W - 110, y)], fill=sub, width=2)
    for ln in lines[:18]:
        y += 56
        d.text((120, y - 44), str(ln.get("name") or "")[:28], fill=ink, font=zumen_dxf._font(28))
        if ln.get("amount") not in (None, ""):
            try:
                d.text((W - 120, y - 44), f"¥{int(ln['amount']):,}", fill=ink, font=zumen_dxf._font(28), anchor="ra")
            except (TypeError, ValueError):
                pass
        d.line([(110, y), (W - 110, y)], fill=(220, 224, 230), width=1)
    return im


def deal_drawing_pages(c, t: str, deal_id: int) -> list:
    """案件に紐づく図面（最新版）のページ [(file_id, page_no, drawing_id, drawing_no, rev)]"""
    out, seen = [], set()
    rows = c.execute("""SELECT DISTINCT r.drawing_id FROM dz_links l JOIN dz_revisions r ON r.doc_id=l.doc_id
        WHERE l.tenant_id=? AND l.target_type='deal' AND l.target_id=?
        UNION SELECT DISTINCT ln.drawing_id FROM dz_links l JOIN dz_lines ln ON ln.doc_id=l.doc_id
        WHERE l.tenant_id=? AND l.target_type='deal' AND l.target_id=? AND ln.drawing_id IS NOT NULL""", (t, deal_id, t, deal_id)).fetchall()
    for x in rows:
        did = x[0]
        if did in seen:
            continue
        seen.add(did)
        cur = zumen.current_rev(c, did)
        dr = c.execute("SELECT drawing_no, name FROM dz_drawings WHERE id=?", (did,)).fetchone()
        if cur and dr:
            out.append({"file_id": cur["file_id"], "page_no": cur["page_no"] or 1, "drawing_id": did,
                        "drawing_no": dr["drawing_no"], "name": dr["name"], "rev": cur["rev"]})
    out.sort(key=lambda x: x["drawing_no"] or "")
    return out


def build_router(check_session: Callable, main_router) -> APIRouter:
    r = APIRouter(prefix="/api/zumen")

    # ── 名刺：会社ごとの件数 ──
    @r.get("/card-counts")
    def card_counts(session: dict = Depends(check_session)):
        t = session["tenant_id"]
        out = {}
        with _conn() as c:
            als = {}
            for a in c.execute("SELECT company_id, norm_alias FROM dz_aliases WHERE tenant_id=?", (t,)):
                als.setdefault(a["company_id"], []).append(a["norm_alias"])
            for co in c.execute("""SELECT co.id, co.name, co.norm_name, co.card_company, COALESCE(dw.n, 0) drawings, COALESCE(dd.n, 0) docs, COALESCE(dd.orders, 0) orders
                FROM dz_companies co
                LEFT JOIN (SELECT company_id, COUNT(*) n FROM dz_drawings WHERE tenant_id=? AND merged_into IS NULL GROUP BY company_id) dw ON dw.company_id=co.id
                LEFT JOIN (SELECT company_id, COUNT(*) n, SUM(kind='注文書') orders FROM dz_docs WHERE tenant_id=? AND status='confirmed' GROUP BY company_id) dd ON dd.company_id=co.id
                WHERE co.tenant_id=? AND co.merged_into IS NULL""", (t, t, t)):
                if not (co["drawings"] or co["docs"]):
                    continue
                v = {"company_id": co["id"], "name": co["name"], "drawings": co["drawings"], "docs": co["docs"], "orders": co["orders"]}
                for key in {co["norm_name"], zm.norm_company(co["card_company"] or "")} - {""}:
                    out[key] = v
                for na in als.get(co["id"], []):
                    out.setdefault(na, v)
        # 名刺の会社名そのままでも引けるようにする（名刺の画面の見出しは名刺の表記）
        by_name = {}
        for name in main_router.card_companies(t):
            hit = out.get(zm.norm_company(name)) or out.get(zm.base_company(name))
            if hit:
                by_name[name] = hit
        return {"companies": out, "by_name": by_name}

    # ── 受注後の進行ボード：カードの縮小画像 ──
    @r.get("/deal-thumbs")
    def deal_thumbs(ids: str = Query(""), session: dict = Depends(check_session)):
        t = session["tenant_id"]
        want = [int(x) for x in re.findall(r"\d+", ids)][:300]
        out = {}
        if not want:
            return {"thumbs": out}
        with _conn() as c:
            linked = {x["target_id"] for x in c.execute(f"""SELECT DISTINCT target_id FROM dz_links WHERE tenant_id=? AND target_type='deal'
                AND target_id IN ({",".join("?" * len(want))})""", (t, *want))}
            for did in linked:
                pages = deal_drawing_pages(c, t, did)
                if pages:
                    p = pages[0]
                    out[str(did)] = {"file_id": p["file_id"], "page_no": p["page_no"], "drawing_no": p["drawing_no"],
                                     "rev": p["rev"], "count": len(pages)}
        return {"thumbs": out}

    # ── 見積書の控え ──
    class QuoteIn(BaseModel):
        deal_id: int
        doc_no: Optional[str] = None
        doc_date: Optional[str] = None
        amount: Optional[int] = None
        title: Optional[str] = None
        lines: list = []
        html: str
        actor: Optional[str] = None

    @r.post("/quote-doc")
    def quote_doc(body: QuoteIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        html = _clean_html(body.html)
        data = html.encode("utf-8")
        if len(data) > MAX_HTML:
            raise HTTPException(400, "見積書が大きすぎます")
        sha = hashlib.sha256(data).hexdigest()
        now = zumen._now()
        with _conn() as c:
            c.execute("BEGIN IMMEDIATE")
            deal = c.execute("SELECT * FROM deals WHERE id=? AND tenant_id=?", (body.deal_id, t)).fetchone()
            if not deal:
                raise HTTPException(404, "案件が見つかりません")
            ex = c.execute("SELECT id FROM dz_files WHERE tenant_id=? AND sha256=?", (t, sha)).fetchone()
            if ex:
                d = c.execute("SELECT id FROM dz_docs WHERE file_id=? AND status<>'trashed'", (ex["id"],)).fetchone()
                if d:
                    c.rollback()
                    return {"ok": True, "doc_id": d["id"], "duplicate": True}
            reg = {}
            company_name = deal["company_snapshot"] or "（会社未設定）"
            company_id = zumen.ensure_company(c, t, company_name, reg, company_name, main_router.group_names(t))
            name = f"見積書_{zumen._txt(body.doc_no, 40) or body.deal_id}.html"
            key = storage.orig_key(t, sha, ".html")
            storage.get_store().put(key, data)
            fid = ex["id"] if ex else c.execute("""INSERT INTO dz_files (tenant_id, sha256, filename, ext, mime, size, storage_key, pages, rendered, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""", (t, sha, name, ".html", "text/html", len(data), key, 1, 1, now)).lastrowid
            lines = [x for x in (body.lines or []) if isinstance(x, dict)][:100]
            if not ex:   # 一覧で見分けがつくよう、見積書の小さな画像を作っておく
                try:
                    im = quote_image(company_name, body.doc_no, body.doc_date, body.amount, body.title or deal["title"], lines)
                    view, size = zumen._jpeg(im, zumen.VIEW_PX)
                    thumb, _ = zumen._jpeg(im, zumen.THUMB_PX, 80)
                    st = storage.get_store()
                    st.put(storage.page_key(sha, 1, "view"), view)
                    st.put(storage.page_key(sha, 1, "thumb"), thumb)
                    text = " ".join(str(x.get("name") or "") for x in lines)
                    c.execute("INSERT OR REPLACE INTO dz_pages (file_id, page_no, text, width, height) VALUES (?,?,?,?,?)", (fid, 1, text, size[0], size[1]))
                except Exception:
                    pass
            doc_id = c.execute("""INSERT INTO dz_docs (tenant_id, file_id, status, kind, direction, company_id, doc_no, doc_date, amount, title,
                page_from, page_to, edit_json, confirmed_by, confirmed_at, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                               (t, fid, "confirmed", "見積書", "発行", company_id, zumen._txt(body.doc_no, 60), zumen._d(body.doc_date),
                                body.amount, zumen._txt(body.title or deal["title"], 60), 1, 1,
                                zumen._j({"lines": lines, "drawings": [], "_company": {"company_id": company_id, "name": company_name}, "source": "sales-quote"}),
                                zumen._txt(body.actor, 40) or None, now, now, now)).lastrowid
            for i, ln in enumerate(lines, 1):
                def _i(v):
                    try:
                        return int(v) if v not in (None, "") else None
                    except (TypeError, ValueError):
                        return None
                try:
                    qty = float(ln.get("qty")) if ln.get("qty") not in (None, "") else None
                except (TypeError, ValueError):
                    qty = None
                did = zumen.ensure_drawing(c, t, company_id, ln, reg) if ln.get("drawing_no") else None
                c.execute("""INSERT INTO dz_lines (tenant_id, doc_id, line_no, drawing_no, norm_no, rev, name, material, thickness, qty,
                    unit_price, amount, due_on, drawing_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (t, doc_id, i, zumen._txt(ln.get("drawing_no"), 80), zm.norm_drawing_no(ln.get("drawing_no")), None,
                           zumen._txt(ln.get("name")), zumen._txt(ln.get("material"), 60), zumen._txt(ln.get("thickness"), 20), qty,
                           _i(ln.get("unit_price")), _i(ln.get("amount")), None, did))
            zumen.link(c, t, doc_id, "deal", body.deal_id, None, ["営業の見積書から"], "rule", reg)
            zumen._index(c, doc_id)
            zumen.event(c, t, zumen._txt(body.actor, 40) or None, "quote", f"doc:{doc_id}", {"reg": reg})
            c.commit()
        return {"ok": True, "doc_id": doc_id}

    # ── グループ会社への依頼に紐づく図面（依頼を受けた側・出した側から読み取り専用）──
    def group_peer(c, t: str, deal_id: int):
        row = c.execute("SELECT * FROM deals WHERE id=? AND tenant_id=?", (deal_id, t)).fetchone()
        if not row:
            raise HTTPException(404, "案件が見つかりません")
        g = sales.group_link(c, t, row)
        if not g:
            return None, None
        return row["group_tenant"], row["group_deal_id"]

    @r.get("/group/deals/{deal_id}/docs")
    def group_docs(deal_id: int, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            gt, gid = group_peer(c, t, deal_id)
            if not gt:
                return {"tenant": None, "drawings": [], "docs": []}
            drawings = deal_drawing_pages(c, gt, gid)
            docs = [dict(x) for x in c.execute(f"""SELECT d.id, d.kind, d.doc_no, d.title, d.doc_date, d.file_id, d.page_from, f.pages, f.mime
                FROM dz_links l JOIN dz_docs d ON d.id=l.doc_id JOIN dz_files f ON f.id=d.file_id
                WHERE l.tenant_id=? AND l.target_type='deal' AND l.target_id=? AND d.status='confirmed'
                AND d.kind IN ({GROUP_KINDS_SQL}) ORDER BY d.doc_date DESC, d.id DESC""", (gt, gid))]
        return {"tenant": sales.display_of(gt) or gt, "deal_id": gid, "drawings": drawings, "docs": docs}

    @r.get("/group/deals/{deal_id}/page/{file_id}/{page_no}")
    def group_page(deal_id: int, file_id: int, page_no: int, size: str = "view", session: dict = Depends(check_session)):
        t = session["tenant_id"]
        if size not in ("view", "thumb", "hi"):
            raise HTTPException(400, "画像の大きさの指定が正しくありません")
        with _conn() as c:
            gt, gid = group_peer(c, t, deal_id)
            if not gt:
                raise HTTPException(404, "グループ会社の案件と紐づいていません")
            allowed = {(p["file_id"], p["page_no"]) for p in deal_drawing_pages(c, gt, gid)}
            for d in c.execute(f"""SELECT d.file_id, d.page_from, d.page_to FROM dz_links l JOIN dz_docs d ON d.id=l.doc_id
                    WHERE l.tenant_id=? AND l.target_type='deal' AND l.target_id=? AND d.status='confirmed' AND d.kind IN ({GROUP_KINDS_SQL})""", (gt, gid)):
                for p in range(d["page_from"] or 1, (d["page_to"] or d["page_from"] or 1) + 1):
                    allowed.add((d["file_id"], p))
            if (file_id, page_no) not in allowed:
                raise HTTPException(404, "このページは表示できません")
            f = c.execute("SELECT * FROM dz_files WHERE id=? AND tenant_id=?", (file_id, gt)).fetchone()
        st = storage.get_store()
        data = st.get(storage.page_key(f["sha256"], page_no, size)) or st.get(storage.page_key(f["sha256"], page_no, "view"))
        if data is None:
            raise HTTPException(404, "画像がありません")
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

    return r
