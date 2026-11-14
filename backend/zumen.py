"""
図面管理（図面タブ）
====================
書類（図面・注文書・見積書・請求書など）を上げる → AIが読む → 確認待ち → 人が1クリックで確定。
確定すると、会社フォルダ・別名・図番と版・案件への紐づけ・納期（案件／共有カレンダー）・検索の索引が一度にそろう。

  POST /api/zumen/upload                 ファイルを受け取る（複数可）
  GET  /api/zumen/inbox                  確認待ち
  GET  /api/zumen/docs/{id}              書類と候補（候補は毎回計算し直す）
  PUT  /api/zumen/docs/{id}              確定前の修正を保存
  POST /api/zumen/docs/{id}/confirm      確定（1つのトランザクション）
  POST /api/zumen/events/{id}/undo       確定・統合を元に戻す（確定は24時間以内）
  ほか 会社・別名・図番・検索・納期・現場・数字・設定
"""

import fcntl
import hashlib
import io
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

import sales
import storage
import teamcal
import zumen_ai as za
import zumen_match as zm

log = logging.getLogger("zumen")
_conn = sales._conn

MAX_FILE = int(os.getenv("DZ_MAX_FILE_MB", "200")) * 1024 * 1024
RENDER_MAX_PAGES = int(os.getenv("DZ_RENDER_MAX_PAGES", "200"))
VIEW_PX = 2000
THUMB_PX = 360
AI_PX = 1568
CAD_EXT = {".dxf", ".dwg", ".step", ".stp", ".igs", ".iges", ".sldprt", ".sldasm", ".iam", ".ipt"}
IMG_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic", ".heif", ".webp", ".bmp", ".gif"}
UNDO_HOURS = 24
TRASH_DAYS = 30
SITE_STAGES = ("部材手配", "製作中", "立会検査")
DUE_PERSON = "納期"
DUE_COLOR = "#D23F3F"
STATUS_FILE = os.getenv("DZ_BACKUP_STATUS", "/var/meishi/backup-zumen/last.json")
FTS = {"mode": None}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _j(v) -> str:
    return json.dumps(v, ensure_ascii=False)


def _l(s, default=None):
    if not s:
        return default
    try:
        return json.loads(s)
    except ValueError:
        return default


# ════════════════════════════════════════
# 表
# ════════════════════════════════════════
def init_db():
    teamcal.init_db()
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS dz_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, sha256 TEXT NOT NULL,
            filename TEXT, ext TEXT, mime TEXT, size INTEGER, pages INTEGER, storage_key TEXT NOT NULL,
            rendered INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, UNIQUE (tenant_id, sha256));
        CREATE TABLE IF NOT EXISTS dz_pages (
            file_id INTEGER NOT NULL, page_no INTEGER NOT NULL, text TEXT, width INTEGER, height INTEGER,
            PRIMARY KEY (file_id, page_no));
        CREATE TABLE IF NOT EXISTS dz_ingest (
            id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, file_id INTEGER NOT NULL,
            source TEXT NOT NULL, hint_json TEXT, status TEXT NOT NULL, tries INTEGER NOT NULL DEFAULT 0,
            error TEXT, actor TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_dz_ingest_status ON dz_ingest(status, id);
        CREATE TABLE IF NOT EXISTS dz_docs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, file_id INTEGER NOT NULL, ingest_id INTEGER,
            page_from INTEGER, page_to INTEGER, kind TEXT, direction TEXT, company_id INTEGER, counterparty_raw TEXT,
            doc_no TEXT, ref_no TEXT, doc_date TEXT, amount INTEGER, due_on TEXT, title TEXT,
            status TEXT NOT NULL, ai_json TEXT, edit_json TEXT, ai_conf REAL, questions_json TEXT, answers_json TEXT,
            hint_json TEXT, error TEXT, confirmed_by TEXT, confirmed_at TEXT, trashed_at TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_dz_docs_status ON dz_docs(tenant_id, status);
        CREATE INDEX IF NOT EXISTS idx_dz_docs_company ON dz_docs(tenant_id, company_id);
        CREATE TABLE IF NOT EXISTS dz_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, doc_id INTEGER NOT NULL, line_no INTEGER,
            drawing_no TEXT, norm_no TEXT, rev TEXT, name TEXT, material TEXT, thickness TEXT, qty REAL,
            unit_price INTEGER, amount INTEGER, due_on TEXT, drawing_id INTEGER);
        CREATE INDEX IF NOT EXISTS idx_dz_lines_doc ON dz_lines(doc_id);
        CREATE INDEX IF NOT EXISTS idx_dz_lines_drawing ON dz_lines(drawing_id);
        CREATE TABLE IF NOT EXISTS dz_companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, name TEXT NOT NULL, norm_name TEXT NOT NULL,
            card_company TEXT, mail_domains TEXT, is_group INTEGER NOT NULL DEFAULT 0, cost_rate REAL,
            merged_into INTEGER, created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_dz_companies ON dz_companies(tenant_id, norm_name);
        CREATE TABLE IF NOT EXISTS dz_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, company_id INTEGER NOT NULL,
            alias TEXT NOT NULL, norm_alias TEXT NOT NULL, source TEXT NOT NULL, hits INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, UNIQUE (tenant_id, norm_alias));
        CREATE TABLE IF NOT EXISTS dz_drawings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, company_id INTEGER NOT NULL,
            drawing_no TEXT NOT NULL, norm_no TEXT NOT NULL, name TEXT, material TEXT, thickness TEXT, surface TEXT,
            current_rev_id INTEGER, tags TEXT, merged_into INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_dz_drawings_no ON dz_drawings(tenant_id, company_id, norm_no) WHERE merged_into IS NULL;
        CREATE TABLE IF NOT EXISTS dz_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, drawing_id INTEGER NOT NULL,
            doc_id INTEGER NOT NULL, file_id INTEGER NOT NULL, page_no INTEGER, rev TEXT, change_note TEXT,
            superseded_at TEXT, created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_dz_rev_drawing ON dz_revisions(drawing_id);
        CREATE TABLE IF NOT EXISTS dz_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, doc_id INTEGER NOT NULL,
            target_type TEXT NOT NULL, target_id INTEGER NOT NULL, score INTEGER, reasons TEXT, method TEXT NOT NULL,
            created_at TEXT NOT NULL, UNIQUE (doc_id, target_type, target_id));
        CREATE INDEX IF NOT EXISTS idx_dz_links_target ON dz_links(tenant_id, target_type, target_id);
        CREATE TABLE IF NOT EXISTS dz_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, doc_id INTEGER NOT NULL, field TEXT NOT NULL,
            candidates_json TEXT, chosen TEXT, was_top INTEGER, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS dz_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, drawing_id INTEGER, deal_id INTEGER,
            kind TEXT NOT NULL, body TEXT, photo_file_id INTEGER, author TEXT, created_at TEXT NOT NULL, deleted_at TEXT);
        CREATE INDEX IF NOT EXISTS idx_dz_notes_drawing ON dz_notes(drawing_id);
        CREATE TABLE IF NOT EXISTS dz_bom (
            id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, parent_id INTEGER NOT NULL,
            child_id INTEGER NOT NULL, qty REAL, note TEXT, created_at TEXT NOT NULL, UNIQUE (parent_id, child_id));
        CREATE TABLE IF NOT EXISTS dz_schedule (
            id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, deal_id INTEGER NOT NULL,
            stage TEXT NOT NULL, plan_on TEXT, done_on TEXT);
        CREATE TABLE IF NOT EXISTS dz_deal_state (
            tenant_id TEXT NOT NULL, deal_id INTEGER NOT NULL, started_at TEXT, shipped_at TEXT,
            PRIMARY KEY (tenant_id, deal_id));
        CREATE TABLE IF NOT EXISTS dz_finance (
            tenant_id TEXT NOT NULL, month TEXT NOT NULL, gross_target INTEGER, gross_rate_target REAL,
            op_target INTEGER, fixed_cost INTEGER, cost_rate REAL, updated_at TEXT,
            PRIMARY KEY (tenant_id, month));
        CREATE TABLE IF NOT EXISTS dz_settings (
            tenant_id TEXT PRIMARY KEY, bulk_confirm INTEGER NOT NULL DEFAULT 0, bulk_threshold REAL NOT NULL DEFAULT 0.9,
            slack_notify INTEGER NOT NULL DEFAULT 0, basis TEXT NOT NULL DEFAULT 'order', updated_at TEXT);
        CREATE TABLE IF NOT EXISTS dz_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, actor TEXT, action TEXT NOT NULL,
            target TEXT, data_json TEXT, at TEXT NOT NULL, undone_at TEXT);
        """)
        if "feat" not in {r["name"] for r in c.execute("PRAGMA table_info(dz_pages)")}:
            c.execute("ALTER TABLE dz_pages ADD COLUMN feat TEXT")
        if "extra_kinds" not in {r["name"] for r in c.execute("PRAGMA table_info(dz_settings)")}:
            c.execute("ALTER TABLE dz_settings ADD COLUMN extra_kinds TEXT")
        if "group_numbers" not in {r["name"] for r in c.execute("PRAGMA table_info(dz_settings)")}:
            c.execute("ALTER TABLE dz_settings ADD COLUMN group_numbers INTEGER NOT NULL DEFAULT 0")
        if "no_ai" not in {r["name"] for r in c.execute("PRAGMA table_info(dz_companies)")}:
            c.execute("ALTER TABLE dz_companies ADD COLUMN no_ai INTEGER NOT NULL DEFAULT 0")
        cols = {r["name"] for r in c.execute("PRAGMA table_info(cal_events)")}
        if "source_ref" not in cols:
            c.execute("ALTER TABLE cal_events ADD COLUMN source_ref TEXT")
        try:
            c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS dz_fts USING fts5(doc_id UNINDEXED, tenant_id UNINDEXED, body, tokenize='trigram')")
            FTS["mode"] = "fts5"
        except Exception:
            c.execute("CREATE TABLE IF NOT EXISTS dz_fts_plain (doc_id INTEGER PRIMARY KEY, tenant_id TEXT, body TEXT)")
            FTS["mode"] = "like"
        c.commit()


# ════════════════════════════════════════
# ファイルの画像化と文字の取り出し
# ════════════════════════════════════════
def _ext(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lower()[:8]


def sniff(data: bytes, filename: str) -> str:
    if data[:5] == b"%PDF-":
        return "application/pdf"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftyphevc"):
        return "image/heic"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    ext = _ext(filename)
    if ext in CAD_EXT:
        return "application/cad"
    return "application/octet-stream"


def _jpeg(img, px: int, quality: int = 82) -> tuple:
    from PIL import Image
    im = img.convert("RGB") if img.mode not in ("RGB", "L") else img
    w, h = im.size
    s = min(1.0, px / max(w, h))
    if s < 1.0:
        im = im.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)
    b = io.BytesIO()
    im.save(b, "JPEG", quality=quality, optimize=True)
    return b.getvalue(), im.size


def _bin(name: str) -> str:
    """外部の道具（pdftoppm など）の場所。サービスの PATH が venv だけに絞られていても見つけられるようにする"""
    import shutil
    found = shutil.which(name)
    if found:
        return found
    for d in ("/usr/bin", "/usr/local/bin", "/bin", "/opt/homebrew/bin"):
        if os.path.exists(os.path.join(d, name)):
            return os.path.join(d, name)
    return name


def _pdf_pages(path: str) -> int:
    try:
        from pypdf import PdfReader
        return len(PdfReader(path).pages)
    except Exception:
        try:
            out = subprocess.run([_bin("pdfinfo"), path], capture_output=True, text=True, timeout=60).stdout
            m = re.search(r"Pages:\s+(\d+)", out)
            return int(m.group(1)) if m else 0
        except Exception:
            return 0


def _pdf_text(path: str, n: int) -> str:
    try:
        return subprocess.run([_bin("pdftotext"), "-f", str(n), "-l", str(n), "-layout", path, "-"],
                              capture_output=True, text=True, timeout=60).stdout.strip()[:20000]
    except Exception:
        return ""


def _safe_dpi(path: str, n: int, dpi: int, max_px: int = 9000) -> int:
    """極端に大きいページ（巨大な用紙サイズを書いたPDF）で画像が何GBにもならないよう、長い辺を max_px までに抑える"""
    try:
        out = subprocess.run([_bin("pdfinfo"), "-f", str(n), "-l", str(n), path], capture_output=True, text=True, timeout=30).stdout
        m = re.search(r"Page\s+%d\s+size:\s+([\d.]+)\s+x\s+([\d.]+)" % n, out) or re.search(r"Page size:\s+([\d.]+)\s+x\s+([\d.]+)", out)
        if m:
            longest = max(float(m.group(1)), float(m.group(2))) / 72   # インチ
            if longest > 0 and longest * dpi > max_px:
                return max(10, int(max_px / longest))
    except Exception:
        pass
    return dpi


def _pdf_render(path: str, n: int, dpi: int = 110):
    from PIL import Image
    with tempfile.TemporaryDirectory() as td:
        base = os.path.join(td, "p")
        dpi = _safe_dpi(path, n, dpi)
        try:
            subprocess.run([_bin("pdftoppm"), "-f", str(n), "-l", str(n), "-r", str(dpi), "-jpeg", "-jpegopt", "quality=88", path, base],
                           capture_output=True, timeout=180, check=True)
        except FileNotFoundError:
            raise RuntimeError("PDFを画像にする道具（pdftoppm）がサーバーで見つかりません。システム担当者に連絡してください")
        outs = sorted(f for f in os.listdir(td) if f.endswith(".jpg"))
        if not outs:
            raise RuntimeError("PDFを画像にできませんでした")
        im = Image.open(os.path.join(td, outs[0]))
        im.load()
        return im


def _open_images(data: bytes, mime: str) -> list:
    from PIL import Image, ImageOps
    if mime == "image/heic":
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError:
            raise RuntimeError("HEIC（iPhoneの写真）を開く部品が入っていません。JPEGで撮り直すか、deploy.sh を最新にしてください")
    im = Image.open(io.BytesIO(data))
    frames = []
    try:
        i = 0
        while True:
            im.seek(i)
            fr = ImageOps.exif_transpose(im.copy())
            frames.append(fr)
            i += 1
            if i >= RENDER_MAX_PAGES:
                break
    except EOFError:
        pass
    return frames or [ImageOps.exif_transpose(im)]


def _feat(im) -> Optional[str]:
    try:
        import zumen_extra
        return zumen_extra.feature_of(im)
    except Exception:
        return None


def render_file(c, frow) -> int:
    """原本から表示用・縮小の画像と各ページの文字を作る。ページ数を返す"""
    st = storage.get_store()
    data = st.get(frow["storage_key"])
    if data is None:
        raise RuntimeError("原本が見つかりません")
    mime = frow["mime"]
    pages = 0
    if mime == "application/pdf":
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            tf.write(data)
            path = tf.name
        try:
            total = _pdf_pages(path)
            pages = min(total, RENDER_MAX_PAGES)
            for n in range(1, pages + 1):
                im = _pdf_render(path, n)
                view, size = _jpeg(im, VIEW_PX)
                thumb, _ = _jpeg(im, THUMB_PX, 75)
                st.put(storage.page_key(frow["sha256"], n, "view"), view)
                st.put(storage.page_key(frow["sha256"], n, "thumb"), thumb)
                c.execute("INSERT OR REPLACE INTO dz_pages (file_id, page_no, text, width, height, feat) VALUES (?,?,?,?,?,?)",
                          (frow["id"], n, _pdf_text(path, n), size[0], size[1], _feat(im)))
            pages = total
        finally:
            os.unlink(path)
    elif mime == "application/cad" and _ext(frow["filename"] or "") == ".dxf":
        import zumen_dxf
        try:
            im, text = zumen_dxf.render(data)
        except Exception:
            log.exception("DXFを描けませんでした")
            im, text = None, ""
        if im is not None:
            view, size = _jpeg(im, VIEW_PX, 88)
            thumb, _ = _jpeg(im, THUMB_PX, 80)
            hi, _ = _jpeg(im, 6000, 88)
            st.put(storage.page_key(frow["sha256"], 1, "view"), view)
            st.put(storage.page_key(frow["sha256"], 1, "thumb"), thumb)
            st.put(storage.page_key(frow["sha256"], 1, "hi"), hi)
            c.execute("INSERT OR REPLACE INTO dz_pages (file_id, page_no, text, width, height, feat) VALUES (?,?,?,?,?,?)",
                      (frow["id"], 1, text, size[0], size[1], _feat(im)))
            pages = 1
    elif mime.startswith("image/"):
        frames = _open_images(data, mime)
        for i, im in enumerate(frames, 1):
            view, size = _jpeg(im, VIEW_PX)
            thumb, _ = _jpeg(im, THUMB_PX, 75)
            st.put(storage.page_key(frow["sha256"], i, "view"), view)
            st.put(storage.page_key(frow["sha256"], i, "thumb"), thumb)
            c.execute("INSERT OR REPLACE INTO dz_pages (file_id, page_no, text, width, height, feat) VALUES (?,?,?,?,?,?)",
                      (frow["id"], i, "", size[0], size[1], _feat(im)))
        pages = len(frames)
    c.execute("UPDATE dz_files SET pages=?, rendered=1 WHERE id=?", (pages, frow["id"]))
    return pages


def renderable(frow) -> bool:
    """画像にできるファイルか（PDF・画像・DXF。DWG・STEP は保存と紐づけだけ）"""
    return frow["mime"] != "application/cad" or _ext(frow["filename"] or "") == ".dxf"


def ai_inputs(c, frow, page_from: int = None, page_to: int = None) -> tuple:
    from PIL import Image
    st = storage.get_store()
    rows = c.execute("SELECT page_no, text FROM dz_pages WHERE file_id=? ORDER BY page_no", (frow["id"],)).fetchall()
    imgs, texts = [], []
    for r in rows:
        if page_from and r["page_no"] < page_from or page_to and r["page_no"] > page_to:
            continue
        raw = st.get(storage.page_key(frow["sha256"], r["page_no"], "view"))
        if raw is None:
            continue
        jpg, _ = _jpeg(Image.open(io.BytesIO(raw)), AI_PX, 80)
        imgs.append((r["page_no"], jpg))
        texts.append((r["page_no"], r["text"] or ""))
    return imgs, texts


# ════════════════════════════════════════
# 受付と読み取り（ワーカー）
# ════════════════════════════════════════
_W = {"started": False, "event": threading.Event(), "own": None, "last_purge": None}


def kick():
    _W["event"].set()


def start_worker(own_name: Callable[[str], str]):
    """読み取りのワーカー（アプリの中のスレッド）。プロセスが複数あっても、ロックを取れた1つだけが動く"""
    _W["own"] = own_name
    if _W["started"] or os.getenv("DZ_WORKER", "1") == "0":
        return
    _W["started"] = True
    threading.Thread(target=_worker_main, daemon=True).start()


def _worker_main():
    try:
        init_db()
    except Exception:
        log.exception("図面の表を作れませんでした")
    lock_path = os.getenv("DZ_WORKER_LOCK", "/tmp/zumen-worker.lock")
    fh = open(lock_path, "w")
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            time.sleep(60)
    while True:
        try:
            if not work_once():
                _purge_trash()
                try:
                    import zumen_extra
                    zumen_extra.tick()
                except Exception:
                    log.exception("メールの取り込みでエラー")
                _W["event"].wait(30)
                _W["event"].clear()
        except Exception:
            log.exception("図面のワーカーでエラー")
            time.sleep(10)


def _claim():
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM dz_ingest WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
        if not row:
            c.commit()
            return None
        c.execute("UPDATE dz_ingest SET status='reading', tries=tries+1, updated_at=? WHERE id=?", (_now(), row["id"]))
        c.commit()
        return dict(row)


def work_once() -> bool:
    """受付を1件処理する。処理したら True"""
    job = _claim()
    if not job:
        return False
    try:
        process(job)
        with _conn() as c:
            c.execute("UPDATE dz_ingest SET status='done', error=NULL, updated_at=? WHERE id=?", (_now(), job["id"]))
            c.commit()
    except Exception as e:
        log.exception("読み取りに失敗 ingest=%s", job["id"])
        with _conn() as c:
            st = "queued" if job["tries"] + 1 < 3 and not isinstance(e, PermissionError) else "failed"
            c.execute("UPDATE dz_ingest SET status=?, error=?, updated_at=? WHERE id=?", (st, str(e)[:500], _now(), job["id"]))
            if st == "failed":
                _failed_doc(c, job, str(e))
            c.commit()
    return True


def _failed_doc(c, job, err: str):
    hint = _l(job.get("hint_json"), {}) or {}
    if hint.get("redo_doc"):
        c.execute("UPDATE dz_docs SET status='review', error=?, updated_at=? WHERE id=?", (err[:300], _now(), hint["redo_doc"]))
        return
    frow = c.execute("SELECT * FROM dz_files WHERE id=?", (job["file_id"],)).fetchone()
    now = _now()
    c.execute("""INSERT INTO dz_docs (tenant_id, file_id, ingest_id, page_from, page_to, kind, direction, status, ai_json,
        ai_conf, questions_json, hint_json, error, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (job["tenant_id"], job["file_id"], job["id"], 1, max(1, frow["pages"] or 1), "その他", "受け取り", "review", None, 0.0,
               _j(["読み取れませんでした。手で入力するか、PDFにし直して上げてください"]), job.get("hint_json"), err[:300], now, now))


def _default_doc(frow) -> dict:
    g = za.guess_from_filename(frow["filename"])
    is_cad = frow["mime"] == "application/cad"
    return {"pages": list(range(1, max(1, frow["pages"] or 1) + 1)), "kind": "図面" if is_cad else "その他", "direction": "受け取り",
            "counterparty": "", "doc_no": "", "ref_no": "", "doc_date": None, "amount": None, "due_on": None, "title": g["name"],
            "lines": [], "drawings": [{"page": 1, "drawing_no": g["drawing_no"], "rev": "", "name": g["name"], "material": "",
                                       "thickness": "", "surface": "", "change_note": "", "parts": []}] if is_cad and g["drawing_no"] else [],
            "confidence": {k: 0.0 for k in ("kind", "counterparty", "doc_no", "amount", "due_on", "drawing_no", "rev")},
            "questions": ["このCADファイルは中身を読み取れないため、会社と図番を入力してください" if is_cad else
                          "AIが使えないため、内容を手入力してください"]}


def ai_blocked(c, t: str, hint: dict) -> str:
    """AIに送らない書類か（上げるときに指定した／秘密保持でAIを止めた会社の書類）。理由の文を返す"""
    if hint.get("no_ai"):
        return "追加するときに「AIに送らない」が選ばれています"
    cids = []
    if hint.get("mail_domain"):
        cids += [x["id"] for x in c.execute("SELECT id, mail_domains FROM dz_companies WHERE tenant_id=? AND merged_into IS NULL AND no_ai=1", (t,))
                 if hint["mail_domain"] in (x["mail_domains"] or "").split(",")]
    if hint.get("deal_id"):
        d = c.execute("SELECT company_snapshot company FROM deals WHERE id=? AND tenant_id=?", (hint["deal_id"], t)).fetchone()
        if d and d["company"]:
            n = zm.norm_company(d["company"])
            cids += [x["id"] for x in c.execute("SELECT id FROM dz_companies WHERE tenant_id=? AND norm_name=? AND no_ai=1", (t, n))]
    if hint.get("folder"):
        n = zm.norm_company(hint["folder"])
        cids += [x["id"] for x in c.execute("""SELECT co.id FROM dz_companies co LEFT JOIN dz_aliases a ON a.company_id=co.id
            WHERE co.tenant_id=? AND co.no_ai=1 AND (co.norm_name=? OR a.norm_alias=?)""", (t, n, n))]
    if cids:
        name = c.execute("SELECT name FROM dz_companies WHERE id=?", (cids[0],)).fetchone()["name"]
        return f"「{name}」はAIに送らない設定です"
    return ""


def _conf_of(d: dict) -> float:
    cf = d.get("confidence") or {}
    keys = ["kind", "counterparty"]
    if d.get("kind") in ("注文書", "注文変更書", "見積書", "請求書", "発注書"):
        keys += ["doc_no", "amount"]
    if d.get("kind") in ("注文書", "注文変更書"):
        keys.append("due_on")
    if d.get("kind") == "図面":
        keys += ["drawing_no", "rev"]
    return round(min(cf.get(k, 0.5) for k in keys), 3)


def process(job: dict):
    with _conn() as c:
        frow = c.execute("SELECT * FROM dz_files WHERE id=?", (job["file_id"],)).fetchone()
        if not frow:
            raise PermissionError("ファイルが見つかりません")
        if not frow["rendered"] and renderable(frow):
            render_file(c, frow)
            c.commit()
            frow = c.execute("SELECT * FROM dz_files WHERE id=?", (job["file_id"],)).fetchone()
        hint = _l(job.get("hint_json"), {}) or {}
        redo = hint.get("redo_doc")
        pf, pt = None, None
        extra = ""
        if redo:
            d = c.execute("SELECT * FROM dz_docs WHERE id=?", (redo,)).fetchone()
            if not d:
                return
            pf, pt = d["page_from"], d["page_to"]
            qa = _l(d["answers_json"], []) or []
            if qa:
                extra = "前回の読み取りへの担当者の答え（これを優先して読み直してください）:\n" + "\n".join(
                    f"- 問い: {x.get('q', '')} → 答え: {x.get('a', '')}" for x in qa)
            if pf and pt and (pf > 1 or pt < (frow["pages"] or pt)):
                extra += f"\nこのファイルの {pf}〜{pt}ページを1つの書類として読んでください。"
        imgs, texts = ai_inputs(c, frow, pf, pt) if renderable(frow) else ([], [])
        blocked = ai_blocked(c, job["tenant_id"], hint)
        kinds = kinds_for(c, job["tenant_id"])
        if kinds != za.KINDS:
            extra += "\n種類は次の中から選んでください：" + "・".join(kinds)
    own = (_W["own"] or (lambda t: t))(job["tenant_id"])
    if imgs and za.ai_available() and not blocked:
        docs = za.read_document(own, imgs, texts, frow["filename"] or "", extra, kinds)
        if pf:   # ページ番号を元のファイルの番号に戻す（AIには pf ページ目からを渡している）
            for d in docs:
                d["pages"] = [p for p in d["pages"] if pf <= p <= (pt or p)] or list(range(pf, (pt or pf) + 1))
                for dw in d["drawings"]:
                    if not (pf <= (dw.get("page") or pf) <= (pt or pf)):
                        dw["page"] = pf
    else:
        docs = [_default_doc(frow)]
        if blocked and renderable(frow):
            docs[0]["questions"] = [f"{blocked}。AIには送っていないため、内容を手入力してください"]
        if pf:
            docs[0]["pages"] = list(range(pf, (pt or pf) + 1))
    if not docs:
        docs = [_default_doc(frow)]
    if not redo and len(docs) >= 1 and (frow["pages"] or 1) > 1:
        _cover_all_pages(docs, frow["pages"] or 1)
    now = _now()
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        if redo:
            d0 = docs[0]
            if len(docs) > 1:   # 読み直しは1つの書類として扱う
                for x in docs[1:]:
                    d0["lines"] += x["lines"]
                    d0["drawings"] += x["drawings"]
            _write_ai(c, redo, d0, pf, pt, now)
            c.execute("UPDATE dz_docs SET status='review', error=NULL WHERE id=?", (redo,))
            _index(c, redo)
        else:
            for d in docs:
                cur = c.execute("""INSERT INTO dz_docs (tenant_id, file_id, ingest_id, status, hint_json, created_at, updated_at)
                    VALUES (?,?,?,?,?,?,?)""", (job["tenant_id"], frow["id"], job["id"], "review", job.get("hint_json"), now, now))
                _write_ai(c, cur.lastrowid, d, None, None, now)
                _index(c, cur.lastrowid)
        c.commit()


def _cover_all_pages(docs: list, total: int):
    """AIがどの書類にも入れなかったページを、直前の書類に足す（ページが消えて確認待ちに出てこないのを防ぐ）"""
    covered = {p for d in docs for p in d["pages"]}
    docs.sort(key=lambda d: min(d["pages"]))
    for p in range(1, total + 1):
        if p in covered:
            continue
        before = [d for d in docs if min(d["pages"]) < p]
        target = before[-1] if before else docs[0]
        target["pages"] = sorted(set(target["pages"] + [p]))


def _write_ai(c, doc_id: int, d: dict, pf, pt, now: str):
    pages = d.get("pages") or [1]
    c.execute("""UPDATE dz_docs SET page_from=?, page_to=?, kind=?, direction=?, counterparty_raw=?, doc_no=?, ref_no=?,
        doc_date=?, amount=?, due_on=?, title=?, ai_json=?, edit_json=NULL, ai_conf=?, questions_json=?, updated_at=? WHERE id=?""",
              (pf or min(pages), pt or max(pages), d["kind"], d["direction"], d["counterparty"], d["doc_no"], d["ref_no"],
               d["doc_date"], d["amount"], d["due_on"], d["title"], _j(d), _conf_of(d), _j(d.get("questions") or []), now, doc_id))


def _purge_trash():
    today = date.today().isoformat()
    if _W["last_purge"] == today:
        return
    _W["last_purge"] = today
    limit = (datetime.now() - timedelta(days=TRASH_DAYS)).isoformat(timespec="seconds")
    st = storage.get_store()
    with _conn() as c:
        old = c.execute("SELECT * FROM dz_docs WHERE status='trashed' AND trashed_at < ?", (limit,)).fetchall()
        for d in old:
            _delete_doc_rows(c, d["id"])
        for f in c.execute("""SELECT * FROM dz_files f WHERE NOT EXISTS (SELECT 1 FROM dz_docs d WHERE d.file_id=f.id)
                AND NOT EXISTS (SELECT 1 FROM dz_notes n WHERE n.photo_file_id=f.id) AND created_at < ?""", (limit,)).fetchall():
            st.delete(f["storage_key"])
            st.delete_prefix(f"page/{f['sha256'][:2]}/{f['sha256']}")
            c.execute("DELETE FROM dz_pages WHERE file_id=?", (f["id"],))
            try:
                c.execute("DELETE FROM dz_marks WHERE file_id=?", (f["id"],))
            except Exception:
                pass
            c.execute("DELETE FROM dz_files WHERE id=?", (f["id"],))
        c.commit()


def _delete_doc_rows(c, doc_id: int):
    drawing_ids = [r["drawing_id"] for r in c.execute("SELECT drawing_id FROM dz_revisions WHERE doc_id=?", (doc_id,))]
    for t in ("dz_lines", "dz_revisions", "dz_links", "dz_candidates"):
        c.execute(f"DELETE FROM {t} WHERE doc_id=?", (doc_id,))
    c.execute("DELETE FROM dz_links WHERE target_type='doc' AND target_id=?", (doc_id,))
    _fts_delete(c, doc_id)
    c.execute("DELETE FROM dz_docs WHERE id=?", (doc_id,))
    for did in set(drawing_ids):
        recompute_current(c, did)


# ════════════════════════════════════════
# 検索の索引
# ════════════════════════════════════════
def _fts_delete(c, doc_id: int):
    if FTS["mode"] == "fts5":
        c.execute("DELETE FROM dz_fts WHERE doc_id=?", (doc_id,))
    else:
        c.execute("DELETE FROM dz_fts_plain WHERE doc_id=?", (doc_id,))


def _index(c, doc_id: int):
    d = c.execute("SELECT * FROM dz_docs WHERE id=?", (doc_id,)).fetchone()
    if not d:
        return
    parts = [d["kind"], d["counterparty_raw"], d["doc_no"], d["ref_no"], d["title"]]
    if d["company_id"]:
        co = c.execute("SELECT name FROM dz_companies WHERE id=?", (d["company_id"],)).fetchone()
        if co:
            parts.append(co["name"])
    v = _values(d)
    for ln in v.get("lines") or []:
        parts += [ln.get("drawing_no"), ln.get("name"), ln.get("material")]
    for dw in v.get("drawings") or []:
        parts += [dw.get("drawing_no"), dw.get("name"), dw.get("material"), dw.get("surface"), dw.get("change_note")]
    for r in c.execute("SELECT text FROM dz_pages WHERE file_id=? AND page_no BETWEEN ? AND ?",
                       (d["file_id"], d["page_from"] or 1, d["page_to"] or 9999)):
        parts.append((r["text"] or "")[:6000])
    body = " ".join(str(p) for p in parts if p)[:30000]
    _fts_delete(c, doc_id)
    if FTS["mode"] == "fts5":
        c.execute("INSERT INTO dz_fts (doc_id, tenant_id, body) VALUES (?,?,?)", (doc_id, d["tenant_id"], body))
    else:
        c.execute("INSERT INTO dz_fts_plain (doc_id, tenant_id, body) VALUES (?,?,?)", (doc_id, d["tenant_id"], body))


def search_doc_ids(c, t: str, q: str, limit: int = 200) -> list:
    q = (q or "").strip()
    if not q:
        return []
    if FTS["mode"] == "fts5" and len(q) >= 3:
        terms = [x for x in re.split(r"\s+", q) if x]
        expr = " AND ".join('"' + x.replace('"', '""') + '"' for x in terms if len(x) >= 3)
        if expr:
            try:
                rows = c.execute("SELECT doc_id FROM dz_fts WHERE dz_fts MATCH ? AND tenant_id=? LIMIT ?", (expr, t, limit)).fetchall()
                ids = [r["doc_id"] for r in rows]
                short = [x for x in terms if len(x) < 3]
                if short and ids:
                    keep = set()
                    for i in ids:
                        b = c.execute("SELECT body FROM dz_fts WHERE doc_id=?", (i,)).fetchone()
                        if b and all(s.lower() in (b["body"] or "").lower() for s in short):
                            keep.add(i)
                    ids = [i for i in ids if i in keep]
                return ids
            except Exception:
                pass
    tbl = "dz_fts" if FTS["mode"] == "fts5" else "dz_fts_plain"
    terms = [x for x in re.split(r"\s+", q) if x]
    sql = f"SELECT doc_id FROM {tbl} WHERE tenant_id=?" + "".join(" AND body LIKE ?" for _ in terms) + " LIMIT ?"
    return [r["doc_id"] for r in c.execute(sql, [t] + [f"%{x}%" for x in terms] + [limit])]


# ════════════════════════════════════════
# 書類の値と候補
# ════════════════════════════════════════
FIELDS = ("kind", "direction", "counterparty", "doc_no", "ref_no", "doc_date", "amount", "due_on", "title", "lines", "drawings")


def _values(d) -> dict:
    """AIの読み取り＋確定前の修正 = いまの値"""
    ai = _l(d["ai_json"], {}) or {}
    ed = _l(d["edit_json"], {}) or {}
    v = {k: ai.get(k) for k in FIELDS}
    v.update({k: ed[k] for k in FIELDS if k in ed})
    v["lines"] = v.get("lines") or []
    v["drawings"] = v.get("drawings") or []
    if d["status"] == "confirmed":
        for k, col in (("kind", "kind"), ("direction", "direction"), ("doc_no", "doc_no"), ("ref_no", "ref_no"),
                       ("doc_date", "doc_date"), ("amount", "amount"), ("due_on", "due_on"), ("title", "title")):
            v[k] = d[col]
    return v


def companies_of(c, t: str) -> tuple:
    cos = [dict(r) for r in c.execute("SELECT * FROM dz_companies WHERE tenant_id=? AND merged_into IS NULL ORDER BY name", (t,))]
    als = [dict(r) for r in c.execute("SELECT * FROM dz_aliases WHERE tenant_id=?", (t,))]
    return cos, als


def deals_for_company(c, t: str, company: dict) -> list:
    """会社に関係する案件（会社名が一致するもの＋この会社の書類が紐づいているもの）"""
    if not company:
        return []
    norms = {company["norm_name"]} | {a["norm_alias"] for a in c.execute(
        "SELECT norm_alias FROM dz_aliases WHERE company_id=?", (company["id"],))}
    if company.get("card_company"):
        norms.add(zm.norm_company(company["card_company"]))
    ids = {r["target_id"] for r in c.execute("""SELECT l.target_id FROM dz_links l JOIN dz_docs d ON d.id=l.doc_id
        WHERE l.tenant_id=? AND l.target_type='deal' AND d.company_id=?""", (t, company["id"]))}
    since = (date.today() - timedelta(days=90)).isoformat()
    out = []
    for r in c.execute("""SELECT id, title, stage, amount, created_at, closed_at, site_name, pipeline, company_snapshot, delivery_on
            FROM deals WHERE tenant_id=?""", (t,)):
        hit = r["id"] in ids or zm.norm_company(r["company_snapshot"]) in norms or zm.base_company(r["company_snapshot"]) in norms
        if not hit:
            continue
        is_open = r["stage"] not in sales.CLOSED_STAGES
        if not is_open and (r["closed_at"] or "") < since and r["id"] not in ids:
            continue
        d = dict(r)
        d["is_open"] = is_open
        linked = [x["doc_id"] for x in c.execute("SELECT doc_id FROM dz_links WHERE tenant_id=? AND target_type='deal' AND target_id=?", (t, r["id"]))]
        nos, dnos, quotes, last = set(), set(), [], None
        for di in linked:
            dd = c.execute("SELECT * FROM dz_docs WHERE id=? AND status='confirmed'", (di,)).fetchone()
            if not dd:
                continue
            for x in (dd["doc_no"], dd["ref_no"]):
                if x:
                    nos.add(x)
            if dd["kind"] == "見積書" and dd["amount"]:
                quotes.append(dd["amount"])
            if dd["doc_date"] and (not last or dd["doc_date"] > last):
                last = dd["doc_date"]
            for ln in c.execute("SELECT norm_no FROM dz_lines WHERE doc_id=? AND norm_no<>''", (di,)):
                dnos.add(ln["norm_no"])
            for rv in c.execute("""SELECT dr.norm_no FROM dz_revisions r JOIN dz_drawings dr ON dr.id=r.drawing_id WHERE r.doc_id=?""", (di,)):
                dnos.add(rv["norm_no"])
        d.update(doc_nos=nos, drawing_nos=dnos, quote_amounts=quotes, last_doc_date=last)
        out.append(d)
    return out


def current_rev(c, drawing_id: int) -> Optional[dict]:
    r = c.execute("""SELECT r.* FROM dz_drawings d JOIN dz_revisions r ON r.id=d.current_rev_id WHERE d.id=?""", (drawing_id,)).fetchone()
    return dict(r) if r else None


def _rev_sort_key(r) -> tuple:
    k = zm.rev_order(r["rev"])
    return (k if k is not None else (-1, 0)) + (r["created_at"] or "", r["id"])


def recompute_current(c, drawing_id: int):
    revs = c.execute("""SELECT r.* FROM dz_revisions r JOIN dz_docs d ON d.id=r.doc_id
        WHERE r.drawing_id=? AND d.status='confirmed'""", (drawing_id,)).fetchall()
    if not revs:
        c.execute("UPDATE dz_drawings SET current_rev_id=NULL WHERE id=?", (drawing_id,))
        return None
    best = max(revs, key=_rev_sort_key)
    c.execute("UPDATE dz_drawings SET current_rev_id=? WHERE id=?", (best["id"], drawing_id))
    now = _now()
    for r in revs:
        if r["id"] == best["id"]:
            c.execute("UPDATE dz_revisions SET superseded_at=NULL WHERE id=?", (r["id"],))
        elif not r["superseded_at"]:
            c.execute("UPDATE dz_revisions SET superseded_at=? WHERE id=?", (now, r["id"]))
    return best["id"]


def find_drawing(c, t: str, company_id: int, drawing_no: str) -> Optional[dict]:
    n = zm.norm_drawing_no(drawing_no)
    if not n or not company_id:
        return None
    r = c.execute("SELECT * FROM dz_drawings WHERE tenant_id=? AND company_id=? AND norm_no=? AND merged_into IS NULL",
                  (t, company_id, n)).fetchone()
    return dict(r) if r else None


def candidates(c, t: str, d, card_companies: list) -> dict:
    v = _values(d)
    cos, als = companies_of(c, t)
    hint = _l(d["hint_json"], {}) or {}
    # 会社
    if d["company_id"]:
        co = next((x for x in cos if x["id"] == d["company_id"]), None)
        comp = [{"kind": "company", "company_id": co["id"], "name": co["name"], "score": 100, "reason": "選択済み"}] if co else []
    else:
        comp = zm.match_company(v.get("counterparty") or "", cos, als, card_companies, hint.get("mail_domain") or "")
        if hint.get("folder") and (not comp or comp[0]["score"] < 85):
            fc = []
            for seg in [hint["folder"]] + [p for p in (hint.get("path") or "").split("/") if p and p != hint["folder"]]:
                fc = [x | {"reason": f"フォルダ名「{seg}」から：" + x["reason"]}
                      for x in zm.match_company(seg, cos, als, card_companies) if x["kind"] != "new"]
                if fc:
                    break
            if not fc:
                fc = [{"kind": "new", "company_id": None, "name": hint["folder"], "score": 60, "reason": f"フォルダ名「{hint['folder']}」"}]
            seen = {(x["kind"], x.get("company_id") or x["name"]) for x in fc}
            comp = fc + [x for x in comp if (x["kind"], x.get("company_id") or x["name"]) not in seen]
    ed = _l(d["edit_json"], {}) or {}
    chosen = ed.get("_company") or (comp[0] if comp and comp[0]["score"] >= 85 else None)
    company = next((x for x in cos if chosen and x["id"] == chosen.get("company_id")), None)
    # 案件
    deals, pick = [], None
    if company:
        dl = deals_for_company(c, t, company)
        deals, pick = zm.rank_deals(v, dl)
    if hint.get("deal_id"):
        row = c.execute("SELECT id, title, stage FROM deals WHERE id=? AND tenant_id=?", (hint["deal_id"], t)).fetchone()
        if row:
            deals = [{"deal_id": row["id"], "title": row["title"], "stage": row["stage"], "score": 100,
                      "reasons": ["案件画面から追加"]}] + [x for x in deals if x["deal_id"] != row["id"]]
            pick = row["id"]
    # 図番と版
    drawings = []
    warnings = []
    for dw in v.get("drawings") or []:
        info = {"drawing_no": dw.get("drawing_no"), "rev": dw.get("rev"), "status": "new"}
        ex = find_drawing(c, t, company["id"], dw.get("drawing_no")) if company else None
        if ex:
            info["drawing_id"] = ex["id"]
            cur = current_rev(c, ex["id"])
            if not cur:
                info["status"] = "first_rev"
            else:
                info["current_rev"] = cur["rev"]
                if cur["file_id"] == d["file_id"] and (cur["page_no"] or 1) == (dw.get("page") or 1):
                    info["status"] = "same_file"
                else:
                    cmp = zm.compare_rev(dw.get("rev"), cur["rev"])
                    info["status"] = {1: "newer", 0: "same_rev", -1: "older", None: "unknown"}[cmp]
                    if cmp == 0:
                        warnings.append({"level": "warn", "text": f"図番 {dw.get('drawing_no')} は同じ版（{cur['rev'] or '初版'}）が登録済みですが、内容が異なります。図面の差し替えか、版の書き忘れかを確認してください",
                                         "diff": {"drawing_id": ex["id"], "rev_id": cur["id"]}})
                    elif cmp is None:
                        warnings.append({"level": "warn", "text": f"図番 {dw.get('drawing_no')} の版「{dw.get('rev') or '空欄'}」と登録済みの最新版「{cur['rev'] or '初版'}」の新旧を判断できません"})
                    elif cmp == -1:
                        warnings.append({"level": "info", "text": f"図番 {dw.get('drawing_no')} は登録済みの最新版（{cur['rev']}）より古い版です。履歴には登録しますが、最新版は変わりません"})
        drawings.append(info)
    for ln in v.get("lines") or []:
        ex = find_drawing(c, t, company["id"], ln.get("drawing_no")) if company and ln.get("drawing_no") else None
        if not ex:
            continue
        cur = current_rev(c, ex["id"])
        if cur and ln.get("rev") and zm.compare_rev(ln.get("rev"), cur["rev"]) not in (0, None):
            warnings.append({"level": "bad", "text": f"注文書では図番 {ln.get('drawing_no')} の版が「{ln.get('rev')}」ですが、登録済みの最新版は「{cur['rev'] or '初版'}」です"})
        bad = c.execute("SELECT COUNT(*) n FROM dz_notes WHERE drawing_id=? AND kind='不具合' AND deleted_at IS NULL", (ex["id"],)).fetchone()["n"]
        if bad:
            warnings.append({"level": "warn", "text": f"図番 {ln.get('drawing_no')} には、過去の不具合メモが{bad}件あります", "drawing_id": ex["id"]})
    # 書類同士
    related = []
    if company:
        for o in c.execute("""SELECT id, kind, doc_no, ref_no, doc_date, amount, due_on FROM dz_docs WHERE tenant_id=? AND company_id=?
                AND status='confirmed' AND id<>?""", (t, company["id"], d["id"])):
            why = None
            if v.get("ref_no") and o["doc_no"] == v.get("ref_no"):
                why = f"この書類が参照する番号 {o['doc_no']}"
            elif v.get("doc_no") and o["ref_no"] == v.get("doc_no"):
                why = f"番号 {v.get('doc_no')} を参照している"
            if why:
                related.append({"doc_id": o["id"], "kind": o["kind"], "doc_no": o["doc_no"], "doc_date": o["doc_date"],
                                "amount": o["amount"], "due_on": o["due_on"], "reason": why})
    if v.get("kind") == "注文変更書":
        orig = next((x for x in related if x["kind"] == "注文書"), None)
        if orig and v.get("due_on") and orig.get("due_on") and orig["due_on"] != v.get("due_on"):
            warnings.append({"level": "info", "text": f"納期 {orig['due_on']} → {v.get('due_on')}（確定すると、案件と予定の納期も更新されます）"})
    if d["status"] == "review" and v.get("doc_no") and company:
        dup = c.execute("""SELECT id FROM dz_docs WHERE tenant_id=? AND company_id=? AND doc_no=? AND kind=? AND status='confirmed' AND id<>?""",
                        (t, company["id"], v.get("doc_no"), v.get("kind"), d["id"])).fetchone()
        if dup:
            warnings.append({"level": "bad", "text": f"同じ番号 {v.get('doc_no')} の{v.get('kind')}はすでに確定済みです（#{dup['id']}）", "doc_id": dup["id"]})
    return {"companies": comp, "company": chosen, "deals": deals, "deal_pick": pick, "drawings": drawings,
            "warnings": warnings, "related": related}


def doc_view(c, t: str, d, card_companies: list = (), with_candidates: bool = True) -> dict:
    f = c.execute("SELECT * FROM dz_files WHERE id=?", (d["file_id"],)).fetchone()
    co = c.execute("SELECT id, name FROM dz_companies WHERE id=?", (d["company_id"],)).fetchone() if d["company_id"] else None
    out = {
        "id": d["id"], "status": d["status"], "file": {"id": f["id"], "filename": f["filename"], "mime": f["mime"], "pages": f["pages"],
                                                     "size": f["size"], "ext": f["ext"]},
        "page_from": d["page_from"] or 1, "page_to": d["page_to"] or max(1, f["pages"] or 1),
        "values": _values(d), "confidence": (_l(d["ai_json"], {}) or {}).get("confidence") or {},
        "ai_conf": d["ai_conf"], "questions": _l(d["questions_json"], []) or [], "answers": _l(d["answers_json"], []) or [],
        "company": dict(co) if co else None, "error": d["error"], "created_at": d["created_at"],
        "confirmed_by": d["confirmed_by"], "confirmed_at": d["confirmed_at"], "edited": bool(d["edit_json"]),
        "ai_read": bool(d["ai_json"]),
    }
    if d["status"] == "confirmed":
        out["links"] = [dict(r) for r in c.execute("SELECT * FROM dz_links WHERE doc_id=?", (d["id"],))]
        for ln in out["links"]:
            if ln["target_type"] == "deal":
                row = c.execute("SELECT title, stage, company_snapshot FROM deals WHERE id=?", (ln["target_id"],)).fetchone()
                ln["label"] = f"#{ln['target_id']} {row['title']}" if row else f"#{ln['target_id']}（削除済み）"
                ln["stage"] = row["stage"] if row else None
            elif ln["target_type"] == "doc":
                row = c.execute("SELECT kind, doc_no FROM dz_docs WHERE id=?", (ln["target_id"],)).fetchone()
                ln["label"] = f"{row['kind']} {row['doc_no'] or ''}".strip() if row else "（削除済み）"
        out["revisions"] = [dict(r) | {"drawing_no": r["drawing_no"]} for r in c.execute(
            """SELECT r.id, r.drawing_id, r.rev, r.page_no, dr.drawing_no FROM dz_revisions r JOIN dz_drawings dr ON dr.id=r.drawing_id WHERE r.doc_id=?""", (d["id"],))]
        ev = c.execute("""SELECT id, at FROM dz_events WHERE tenant_id=? AND action='confirm' AND target=? AND undone_at IS NULL
            ORDER BY id DESC LIMIT 1""", (t, f"doc:{d['id']}")).fetchone()
        if ev and ev["at"] >= (datetime.now() - timedelta(hours=UNDO_HOURS)).isoformat(timespec="seconds"):
            out["undo_event"] = ev["id"]
    if with_candidates and d["status"] in ("review", "confirmed"):
        out["candidates"] = candidates(c, t, d, list(card_companies))
    return out


# ════════════════════════════════════════
# 確定
# ════════════════════════════════════════
class ConfirmIn(BaseModel):
    kind: str
    direction: str = "受け取り"
    company_id: Optional[int] = None
    company_name: Optional[str] = None
    card_company: Optional[str] = None
    doc_no: Optional[str] = None
    ref_no: Optional[str] = None
    doc_date: Optional[str] = None
    amount: Optional[int] = None
    due_on: Optional[str] = None
    title: Optional[str] = None
    lines: list = []
    drawings: list = []
    register_parts: bool = True
    deal_id: Optional[int] = None
    create_deal: bool = False
    deal_title: Optional[str] = None
    link_docs: list = []
    update_deal_due: bool = False
    history: bool = False   # 過去分の取り込み：案件・納期の予定を作らない
    actor: Optional[str] = None


def _d(v) -> Optional[str]:
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10]).isoformat()
    except ValueError:
        raise HTTPException(400, f"日付の形式が正しくありません（{v}）")


def _txt(v, n=120) -> str:
    return (str(v).strip() if v is not None else "")[:n]


def ensure_company(c, t: str, name: str, reg: dict, card_company: str = None, group_names: tuple = ()) -> int:
    name = _txt(name, 120)
    if not name:
        raise HTTPException(400, "会社を選択するか、会社名を入力してください")
    n = zm.norm_company(name)
    row = c.execute("SELECT id FROM dz_companies WHERE tenant_id=? AND norm_name=? AND merged_into IS NULL", (t, n)).fetchone()
    if row:
        return row["id"]
    al = c.execute("SELECT company_id FROM dz_aliases WHERE tenant_id=? AND norm_alias=?", (t, n)).fetchone()
    if al:
        return al["company_id"]
    is_group = 1 if any(n and n == zm.norm_company(g) for g in group_names) else 0
    cur = c.execute("""INSERT INTO dz_companies (tenant_id, name, norm_name, card_company, is_group, created_at)
        VALUES (?,?,?,?,?,?)""", (t, name, n, card_company, is_group, _now()))
    reg.setdefault("companies", []).append(cur.lastrowid)
    return cur.lastrowid


def add_alias(c, t: str, company_id: int, alias: str, source: str, reg: dict):
    alias = _txt(alias, 120)
    n = zm.norm_company(alias)
    if not n:
        return
    co = c.execute("SELECT norm_name FROM dz_companies WHERE id=?", (company_id,)).fetchone()
    if co and co["norm_name"] == n:
        return
    ex = c.execute("SELECT id, company_id FROM dz_aliases WHERE tenant_id=? AND norm_alias=?", (t, n)).fetchone()
    if ex:
        if ex["company_id"] == company_id:
            c.execute("UPDATE dz_aliases SET hits=hits+1 WHERE id=?", (ex["id"],))
        return
    cur = c.execute("""INSERT INTO dz_aliases (tenant_id, company_id, alias, norm_alias, source, hits, created_at)
        VALUES (?,?,?,?,?,1,?)""", (t, company_id, alias, n, source, _now()))
    reg.setdefault("aliases", []).append(cur.lastrowid)


def ensure_drawing(c, t: str, company_id: int, dw: dict, reg: dict) -> int:
    no = _txt(dw.get("drawing_no"), 80)
    n = zm.norm_drawing_no(no)
    if not n:
        return None
    ex = find_drawing(c, t, company_id, no)
    now = _now()
    if ex:
        sets = {k: _txt(dw.get(k), 120) for k in ("name", "material", "thickness", "surface") if dw.get(k) and not ex.get(k)}
        if sets:
            reg.setdefault("drawing_fields", []).append({"id": ex["id"], **{k: ex.get(k) for k in sets}})
            c.execute(f"UPDATE dz_drawings SET {', '.join(k + '=?' for k in sets)}, updated_at=? WHERE id=?", (*sets.values(), now, ex["id"]))
        return ex["id"]
    cur = c.execute("""INSERT INTO dz_drawings (tenant_id, company_id, drawing_no, norm_no, name, material, thickness, surface,
        created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (t, company_id, no, n, _txt(dw.get("name")), _txt(dw.get("material"), 60), _txt(dw.get("thickness"), 20),
                     _txt(dw.get("surface"), 60), now, now))
    reg.setdefault("drawings", []).append(cur.lastrowid)
    return cur.lastrowid


def _due_person(c, t: str) -> int:
    row = c.execute("SELECT id FROM cal_people WHERE tenant_id=? AND name=?", (t, DUE_PERSON)).fetchone()
    if row:
        return row["id"]
    return c.execute("INSERT INTO cal_people (tenant_id, name, color, created_at) VALUES (?,?,?,?)",
                     (t, DUE_PERSON, DUE_COLOR, _now())).lastrowid


def upsert_due_event(c, t: str, ref: str, day: str, title: str, memo: str, reg: dict):
    if not day:
        return
    ex = c.execute("SELECT * FROM cal_events WHERE tenant_id=? AND source_ref=?", (t, ref)).fetchone()
    now = _now()
    if ex:
        reg.setdefault("cal_updated", []).append({"id": ex["id"], "day": ex["day"], "title": ex["title"], "memo": ex["memo"]})
        c.execute("UPDATE cal_events SET day=?, title=?, memo=?, updated_at=? WHERE id=?", (day, title[:120], memo[:1000], now, ex["id"]))
    else:
        pid = _due_person(c, t)
        cur = c.execute("""INSERT INTO cal_events (tenant_id, person_id, day, start_min, end_min, slot, title, memo, created_at, updated_at, source_ref)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (t, pid, day, 0, 24 * 60, "day", title[:120], memo[:1000], now, now, ref))
        reg.setdefault("cal_events", []).append(cur.lastrowid)


def link(c, t: str, doc_id: int, ttype: str, tid: int, score, reasons, method: str, reg: dict):
    ex = c.execute("SELECT id FROM dz_links WHERE doc_id=? AND target_type=? AND target_id=?", (doc_id, ttype, tid)).fetchone()
    if ex:
        return
    cur = c.execute("""INSERT INTO dz_links (tenant_id, doc_id, target_type, target_id, score, reasons, method, created_at)
        VALUES (?,?,?,?,?,?,?,?)""", (t, doc_id, ttype, tid, score, _j(reasons or []), method, _now()))
    reg.setdefault("links", []).append(cur.lastrowid)


def event(c, t: str, actor, action: str, target: str, data: dict) -> int:
    return c.execute("INSERT INTO dz_events (tenant_id, actor, action, target, data_json, at) VALUES (?,?,?,?,?,?)",
                     (t, actor, action, target, _j(data), _now())).lastrowid


def ingest_bytes(t: str, name: str, data: bytes, source: str = "upload", hint: Optional[dict] = None, actor: Optional[str] = None) -> dict:
    """1ファイルを受け付ける（画面・メール共通）。result は queued / duplicates / errors"""
    if len(data) > MAX_FILE:
        return {"result": "errors", "filename": name, "error": f"{MAX_FILE // 1024 // 1024}MBを超えています"}
    if not data:
        return {"result": "errors", "filename": name, "error": "空のファイルです"}
    mime = sniff(data, name)
    if mime == "application/octet-stream":
        return {"result": "errors", "filename": name, "error": "PDF・画像・CADのファイルを上げてください"}
    sha = hashlib.sha256(data).hexdigest()
    with _conn() as c:
        ex = c.execute("SELECT id FROM dz_files WHERE tenant_id=? AND sha256=?", (t, sha)).fetchone()
        if ex:
            docs = [dict(x) for x in c.execute("SELECT id, kind, status FROM dz_docs WHERE file_id=? AND status<>'trashed'", (ex["id"],))]
            if docs:
                return {"result": "duplicates", "filename": name, "docs": docs}
    key = storage.orig_key(t, sha, _ext(name))
    storage.get_store().put(key, data)
    now = _now()
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        ex = c.execute("SELECT id FROM dz_files WHERE tenant_id=? AND sha256=?", (t, sha)).fetchone()
        fid = ex["id"] if ex else c.execute("""INSERT INTO dz_files (tenant_id, sha256, filename, ext, mime, size, storage_key, created_at)
            VALUES (?,?,?,?,?,?,?,?)""", (t, sha, name, _ext(name), mime, len(data), key, now)).lastrowid
        iid = c.execute("""INSERT INTO dz_ingest (tenant_id, file_id, source, hint_json, status, actor, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?)""", (t, fid, source, _j(hint) if hint else None, "queued", actor, now, now)).lastrowid
        c.commit()
    return {"result": "queued", "filename": name, "ingest_id": iid, "file_id": fid}


# ── 過去分の取り込み（ズメーンからの移行）──
IMPORT_SKIP = re.compile(r"(^|/)(__MACOSX|\.[^/]*|Thumbs\.db|desktop\.ini)(/|$)", re.I)
MAX_ZIP_ENTRIES = 3000


def _fix_zip_name(info) -> str:
    """Windowsで作ったZIPのファイル名（Shift_JIS）を読めるようにする"""
    name = info.filename
    if not (info.flag_bits & 0x800):
        try:
            name = name.encode("cp437").decode("cp932")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return name


def unzip_entries(data: bytes):
    """ZIPの中身を1つずつ返す（全部をメモリに載せない）。開けないZIPは None"""
    import zipfile
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        infos = zf.infolist()[:MAX_ZIP_ENTRIES]
    except Exception:
        return None

    def gen():
        total = 0
        for info in infos:
            name = _fix_zip_name(info).replace("\\", "/")
            if info.is_dir() or IMPORT_SKIP.search(name) or info.flag_bits & 0x1 or info.file_size > MAX_FILE:
                continue
            total += info.file_size
            if total > 4 * 1024 ** 3:   # 展開後4GBで打ち切る（ZIP爆弾よけ）
                break
            try:
                yield name, zf.read(info)
            except Exception:
                continue
    return gen()


IMPORT_GENERIC = re.compile(r"(図面|書類|資料|過去分?|ズメーン.*|zumen.*|export.*|backup.*|書き?出し|エクスポート|バックアップ|新しいフォルダ.*|"
                            r"\d{2,4}(年度?)?|\d{1,2}月?|\d{4}[-_.]?\d{1,2}([-_.]?\d{1,2})?)", re.I)


def import_hint(path: str) -> dict:
    """フォルダ名を会社名の手がかりにする。
    フォルダで選ぶと選んだフォルダ自体が先頭に付くので、2段目に会社らしい名前があればそちらを使う
    （書き出し/大阪機械/x.pdf → 大阪機械、大阪機械/2021/x.pdf → 大阪機械）"""
    segs = [p for p in path.replace("\\", "/").split("/") if p][:-1]
    named = [p for p in segs if not IMPORT_GENERIC.fullmatch(p)]
    h = {"import": 1}
    later = [p for p in segs[1:] if not IMPORT_GENERIC.fullmatch(p)]
    pick = later[0] if later else (named[0] if named else None)
    if pick:
        h["folder"] = pick[:60]
    if named:
        h["path"] = "/".join(named)[:200]
    return h


def import_name(path: str) -> str:
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    return "/".join(parts[-3:])


# ════════════════════════════════════════
# ルーター
# ════════════════════════════════════════
def build_router(check_session: Callable, fetch_cards: Callable, get_tenant: Optional[Callable] = None,
                 sales_router=None, list_tenants: Optional[Callable] = None) -> APIRouter:
    r = APIRouter(prefix="/api/zumen")

    def own(t: str) -> str:
        if get_tenant:
            return (get_tenant(t) or {}).get("display_name") or t
        return t

    def group_names(t: str) -> tuple:
        if not list_tenants:
            return ()
        try:
            return tuple(v.get("display_name") or k for k, v in (list_tenants() or {}).items() if k != t)
        except Exception:
            return ()

    def card_companies(t: str) -> list:
        try:
            return sorted({x.get("company") for x in fetch_cards(t) if x.get("company")})
        except Exception:
            return []

    def card_for(t: str, company_name: str) -> dict:
        n = zm.norm_company(company_name)
        try:
            for x in fetch_cards(t):
                if zm.norm_company(x.get("company")) == n:
                    return x
        except Exception:
            pass
        return {"id": "", "company": company_name, "name": ""}

    def get_doc(c, t, doc_id):
        d = c.execute("SELECT * FROM dz_docs WHERE id=? AND tenant_id=?", (doc_id, t)).fetchone()
        if not d:
            raise HTTPException(404, "書類が見つかりません")
        return d

    def actor_of(a: Optional[str]) -> Optional[str]:
        return _txt(a, 40) or None

    def has_prod(t: str) -> bool:
        try:
            return any(p["key"] == "prod" for p in sales.pipelines_for(own(t)))
        except Exception:
            return False

    start_worker(own)
    r.has_prod = has_prod

    # ── 取り込み ──
    @r.post("/upload")
    async def upload(files: list[UploadFile] = File(...), source: str = Form("upload"), actor: Optional[str] = Form(None),
                     deal_id: Optional[int] = Form(None), paths: Optional[str] = Form(None), mode: str = Form(""),
                     no_ai: int = Form(0),
                     session: dict = Depends(check_session)):
        t = session["tenant_id"]
        res = {"queued": [], "duplicates": [], "errors": []}
        rels = _l(paths, []) or []
        imp = mode == "import"
        for i, uf in enumerate(files[:100]):
            data = await uf.read(MAX_FILE + 1)
            rel = rels[i] if i < len(rels) and isinstance(rels[i], str) else (uf.filename or "file")
            items = unzip_entries(data) if rel.lower().endswith(".zip") else [(rel, data)]
            if items is None:
                res["errors"].append({"filename": os.path.basename(rel), "error": "ZIPを開けませんでした（暗号化されたZIPは使えません）"})
                continue
            for path, body in items:
                hint = {"deal_id": deal_id} if deal_id else {}
                if no_ai:
                    hint["no_ai"] = 1
                if imp:
                    hint.update(import_hint(path))
                name = (import_name(path) if imp else os.path.basename(path))[:200] or "file"
                out = ingest_bytes(t, name, body, "import" if imp else source[:20], hint, actor_of(actor))
                res[out.pop("result")].append(out)
        kick()
        return res

    @r.get("/inbox")
    def inbox(session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            docs = c.execute("""SELECT d.*, f.filename, f.pages, f.mime, i.source FROM dz_docs d JOIN dz_files f ON f.id=d.file_id
                LEFT JOIN dz_ingest i ON i.id=d.ingest_id
                WHERE d.tenant_id=? AND d.status='review' ORDER BY d.id""", (t,)).fetchall()
            cos, als = companies_of(c, t)
            cards = card_companies(t)
            items, memo = [], {}
            for d in docs:
                v = _values(d)
                ed = _l(d["edit_json"], {}) or {}
                hint = _l(d["hint_json"], {}) or {}
                key = (v.get("counterparty") or "", hint.get("mail_domain") or "")
                if key not in memo:
                    memo[key] = next(iter(zm.match_company(key[0], cos, als, cards, key[1], fuzzy=False)), None)
                comp = ed.get("_company") or memo[key]
                if hint.get("folder") and (not comp or comp.get("kind") == "new" or comp.get("score", 0) < 85):
                    fk = ("folder", hint["folder"])
                    if fk not in memo:
                        memo[fk] = next((x for x in zm.match_company(hint["folder"], cos, als, cards, fuzzy=False) if x["kind"] != "new"), None)
                    fc = memo[fk]
                    comp = fc or {"kind": "new", "company_id": None, "name": hint["folder"], "score": 60, "reason": "フォルダ名"}
                items.append({"folder": hint.get("folder"), "id": d["id"], "file_id": d["file_id"], "filename": d["filename"], "source": d["source"], "page_from": d["page_from"] or 1,
                              "page_to": d["page_to"] or 1, "pages": d["pages"], "kind": v.get("kind"), "counterparty": v.get("counterparty"),
                              "company": comp, "doc_no": v.get("doc_no"), "amount": v.get("amount"), "due_on": v.get("due_on"),
                              "title": v.get("title"), "ai_conf": d["ai_conf"], "questions": len(_l(d["questions_json"], []) or []),
                              "error": d["error"], "created_at": d["created_at"], "edited": bool(d["edit_json"]),
                              "drawings": [x.get("drawing_no") for x in v.get("drawings") or []][:3]})
            q = {r_["status"]: r_["n"] for r_ in c.execute("SELECT status, COUNT(*) n FROM dz_ingest WHERE tenant_id=? GROUP BY status", (t,))}
            failed = [dict(x) for x in c.execute("""SELECT i.id, i.error, f.filename FROM dz_ingest i JOIN dz_files f ON f.id=i.file_id
                WHERE i.tenant_id=? AND i.status='failed' AND i.updated_at >= ? ORDER BY i.id DESC LIMIT 20""",
                                                 (t, (datetime.now() - timedelta(days=3)).isoformat()))]
            stg = _settings(c, t)
        return {"items": items, "processing": q.get("queued", 0) + q.get("reading", 0), "failed": failed,
                "settings": stg, "ai_available": za.ai_available()}

    @r.get("/docs/{doc_id}")
    def get_doc_api(doc_id: int, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            d = get_doc(c, t, doc_id)
            return doc_view(c, t, d, card_companies(t))

    class EditIn(BaseModel):
        values: dict = {}
        company: Optional[dict] = None
        answers: Optional[list] = None

    @r.put("/docs/{doc_id}")
    def edit_doc(doc_id: int, body: EditIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            d = get_doc(c, t, doc_id)
            if d["status"] != "review":
                raise HTTPException(400, "確定済みの書類は修正できません。確定を取り消してから修正してください")
            ed = _l(d["edit_json"], {}) or {}
            for k, v in (body.values or {}).items():
                if k in FIELDS:
                    ed[k] = v
            if body.company is not None:
                ed["_company"] = body.company or None
            c.execute("UPDATE dz_docs SET edit_json=?, updated_at=? WHERE id=?", (_j(ed), _now(), doc_id))
            c.commit()
            return doc_view(c, t, get_doc(c, t, doc_id), card_companies(t))

    class AnswerIn(BaseModel):
        answers: list   # [{q, a}]

    @r.post("/docs/{doc_id}/answer")
    def answer(doc_id: int, body: AnswerIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        qa = [{"q": _txt(x.get("q"), 200), "a": _txt(x.get("a"), 500)} for x in body.answers if isinstance(x, dict) and _txt(x.get("a"))]
        if not qa:
            raise HTTPException(400, "回答を入力してください")
        if not za.ai_available():
            raise HTTPException(400, "AIが使えないため、各項目を直接修正してください")
        with _conn() as c:
            d = get_doc(c, t, doc_id)
            if d["status"] != "review":
                raise HTTPException(400, "読み取り直しができるのは、確認待ちの書類だけです")
            old = _l(d["answers_json"], []) or []
            now = _now()
            c.execute("UPDATE dz_docs SET answers_json=?, status='reading', updated_at=? WHERE id=?", (_j(old + qa), now, doc_id))
            c.execute("""INSERT INTO dz_ingest (tenant_id, file_id, source, hint_json, status, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?)""", (t, d["file_id"], "redo", _j({"redo_doc": doc_id}), "queued", now, now))
            c.commit()
        kick()
        return {"ok": True}

    class SplitIn(BaseModel):
        at_page: int

    @r.post("/docs/{doc_id}/split")
    def split(doc_id: int, body: SplitIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            d = get_doc(c, t, doc_id)
            pf, pt = d["page_from"] or 1, d["page_to"] or 1
            if d["status"] != "review" or not (pf < body.at_page <= pt):
                raise HTTPException(400, f"{pf + 1}〜{pt}ページの範囲で指定してください")
            now = _now()
            c.execute("BEGIN IMMEDIATE")
            c.execute("UPDATE dz_docs SET page_to=?, edit_json=NULL, updated_at=? WHERE id=?", (body.at_page - 1, now, doc_id))
            nid = c.execute("""INSERT INTO dz_docs (tenant_id, file_id, ingest_id, page_from, page_to, kind, direction, status, hint_json,
                created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                            (t, d["file_id"], d["ingest_id"], body.at_page, pt, "その他", "受け取り", "review", d["hint_json"], now, now)).lastrowid
            if za.ai_available():
                for x in (doc_id, nid):
                    c.execute("UPDATE dz_docs SET status='reading' WHERE id=?", (x,))
                    c.execute("""INSERT INTO dz_ingest (tenant_id, file_id, source, hint_json, status, created_at, updated_at)
                        VALUES (?,?,?,?,?,?,?)""", (t, d["file_id"], "redo", _j({"redo_doc": x}), "queued", now, now))
            c.commit()
        kick()
        return {"ok": True, "ids": [doc_id, nid]}

    @r.post("/docs/{doc_id}/reject")
    def reject(doc_id: int, session: dict = Depends(check_session)):
        """取り込まない（ごみ箱へ。30日後に消える）"""
        t = session["tenant_id"]
        with _conn() as c:
            d = get_doc(c, t, doc_id)
            if d["status"] == "confirmed":
                raise HTTPException(400, "確定済みの書類は「ごみ箱に移す」を使ってください")
            c.execute("UPDATE dz_docs SET status='trashed', trashed_at=?, updated_at=? WHERE id=?", (_now(), _now(), doc_id))
            c.commit()
        return {"ok": True}

    @r.post("/docs/{doc_id}/trash")
    def trash(doc_id: int, actor: Optional[str] = Query(None), session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            d = get_doc(c, t, doc_id)
            c.execute("BEGIN IMMEDIATE")
            hint = {**(_l(d["hint_json"], {}) or {}), "_before_trash": d["status"]}
            # 注文書をごみ箱に入れたら、共有カレンダーの「納期」の予定も外す（戻したら元に戻す）
            ce = c.execute("SELECT * FROM cal_events WHERE tenant_id=? AND source_ref=?", (t, f"dz_doc:{doc_id}")).fetchone()
            if ce:
                hint["_cal"] = {k: ce[k] for k in ce.keys() if k != "id"}
                c.execute("DELETE FROM cal_events WHERE id=?", (ce["id"],))
            c.execute("UPDATE dz_docs SET status='trashed', hint_json=?, trashed_at=?, updated_at=? WHERE id=?",
                      (_j(hint), _now(), _now(), doc_id))
            for rv in c.execute("SELECT DISTINCT drawing_id FROM dz_revisions WHERE doc_id=?", (doc_id,)).fetchall():
                recompute_current(c, rv["drawing_id"])
            event(c, t, actor_of(actor), "trash", f"doc:{doc_id}", {"status": d["status"]})
            c.commit()
        return {"ok": True}

    @r.post("/docs/{doc_id}/restore")
    def restore(doc_id: int, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            d = get_doc(c, t, doc_id)
            if d["status"] != "trashed":
                return {"ok": True}
            hint = _l(d["hint_json"], {}) or {}
            before = hint.get("_before_trash") or "review"
            c.execute("BEGIN IMMEDIATE")
            cal = hint.pop("_cal", None)
            if cal and not c.execute("SELECT 1 FROM cal_events WHERE tenant_id=? AND source_ref=?", (t, f"dz_doc:{doc_id}")).fetchone():
                cols = [k for k in cal if k in {r_["name"] for r_ in c.execute("PRAGMA table_info(cal_events)")}]
                c.execute(f"INSERT INTO cal_events ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", [cal[k] for k in cols])
            hint.pop("_before_trash", None)
            c.execute("UPDATE dz_docs SET status=?, trashed_at=NULL, hint_json=?, updated_at=? WHERE id=?", (before, _j(hint), _now(), doc_id))
            for rv in c.execute("SELECT DISTINCT drawing_id FROM dz_revisions WHERE doc_id=?", (doc_id,)).fetchall():
                recompute_current(c, rv["drawing_id"])
            c.commit()
        return {"ok": True}

    @r.get("/trash")
    def trash_list(session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            rows = c.execute("""SELECT d.id, d.kind, d.title, d.doc_no, d.trashed_at, d.file_id, d.page_from, f.filename
                FROM dz_docs d JOIN dz_files f ON f.id=d.file_id WHERE d.tenant_id=? AND d.status='trashed' ORDER BY d.trashed_at DESC LIMIT 300""", (t,))
            return {"items": [dict(x) for x in rows], "days": TRASH_DAYS}

    def confirm_in_tx(c, t: str, d, body: ConfirmIn, method: str = "manual") -> dict:
        """確定の本体（トランザクションの中で呼ぶ。commit はしない）。元に戻すための記録を返す"""
        if d["status"] != "review":
            raise HTTPException(400, "この書類はすでに確認待ちではありません")
        kind = body.kind if body.kind in kinds_for(c, t) else None
        if not kind:
            raise HTTPException(400, "書類の種類が正しくありません")
        direction = body.direction if body.direction in ("受け取り", "発行") else "受け取り"
        reg, upd = {}, {}
        before = {k: d[k] for k in d.keys()}
        # 会社
        if body.company_id:
            co = c.execute("SELECT * FROM dz_companies WHERE id=? AND tenant_id=? AND merged_into IS NULL", (body.company_id, t)).fetchone()
            if not co:
                raise HTTPException(400, "会社が見つかりません")
            company_id = co["id"]
        else:
            company_id = ensure_company(c, t, body.company_name, reg, body.card_company, group_names(t))
        raw = (d["counterparty_raw"] or "").strip()
        if raw:
            add_alias(c, t, company_id, raw, "confirm", reg)
        hint = _l(d["hint_json"], {}) or {}
        if hint.get("mail_domain"):
            co = c.execute("SELECT mail_domains FROM dz_companies WHERE id=?", (company_id,)).fetchone()
            doms = [x for x in (co["mail_domains"] or "").split(",") if x]
            if hint["mail_domain"] not in doms:
                upd["mail_domains"] = {"id": company_id, "old": co["mail_domains"]}
                c.execute("UPDATE dz_companies SET mail_domains=? WHERE id=?", (",".join(doms + [hint["mail_domain"]]), company_id))
        company = dict(c.execute("SELECT * FROM dz_companies WHERE id=?", (company_id,)).fetchone())
        # 書類
        doc_date, due_on = _d(body.doc_date), _d(body.due_on)
        lines = [x for x in (body.lines or []) if isinstance(x, dict)][:300]
        line_dues = [_d(x.get("due_on")) for x in lines if x.get("due_on")]
        if line_dues and (not due_on or min(line_dues) < due_on):
            due_on = min(line_dues)
        amount = body.amount
        if amount is None and lines and all(x.get("amount") is not None for x in lines):
            amount = sum(int(x["amount"]) for x in lines)
        if amount is not None and not (-10_000_000_000 < amount < 10_000_000_000):
            raise HTTPException(400, "金額が正しくありません")
        now = _now()
        c.execute("""UPDATE dz_docs SET kind=?, direction=?, company_id=?, doc_no=?, ref_no=?, doc_date=?, amount=?, due_on=?, title=?,
            edit_json=?, status='confirmed', confirmed_by=?, confirmed_at=?, updated_at=? WHERE id=?""",
                  (kind, direction, company_id, _txt(body.doc_no, 60), _txt(body.ref_no, 60), doc_date, amount, due_on, _txt(body.title, 60),
                   _j({"lines": lines, "drawings": body.drawings or [], "_company": {"company_id": company_id, "name": company["name"]}}),
                   actor_of(body.actor), now, now, d["id"]))
        # 明細
        for i, ln in enumerate(lines, 1):
            did = None
            if ln.get("drawing_no"):
                did = ensure_drawing(c, t, company_id, ln, reg)
            qty = ln.get("qty")
            try:
                qty = float(qty) if qty not in (None, "") else None
            except (TypeError, ValueError):
                qty = None
            def _i(v):
                try:
                    return int(v) if v not in (None, "") else None
                except (TypeError, ValueError):
                    return None
            c.execute("""INSERT INTO dz_lines (tenant_id, doc_id, line_no, drawing_no, norm_no, rev, name, material, thickness, qty,
                unit_price, amount, due_on, drawing_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (t, d["id"], i, _txt(ln.get("drawing_no"), 80), zm.norm_drawing_no(ln.get("drawing_no")), _txt(ln.get("rev"), 20),
                       _txt(ln.get("name")), _txt(ln.get("material"), 60), _txt(ln.get("thickness"), 20), qty,
                       _i(ln.get("unit_price")), _i(ln.get("amount")), _d(ln.get("due_on")), did))
        # 図番と版
        touched = set()
        for dw in (body.drawings or [])[:100]:
            if not isinstance(dw, dict):
                continue
            did = ensure_drawing(c, t, company_id, dw, reg)
            if not did:
                continue
            page = dw.get("page") or d["page_from"] or 1
            cur = c.execute("""INSERT INTO dz_revisions (tenant_id, drawing_id, doc_id, file_id, page_no, rev, change_note, created_at)
                VALUES (?,?,?,?,?,?,?,?)""", (t, did, d["id"], d["file_id"], page, _txt(dw.get("rev"), 20), _txt(dw.get("change_note"), 300), now))
            reg.setdefault("revisions", []).append(cur.lastrowid)
            prev = c.execute("SELECT current_rev_id FROM dz_drawings WHERE id=?", (did,)).fetchone()["current_rev_id"]
            upd.setdefault("current", {})[str(did)] = prev
            touched.add(did)
            if body.register_parts:
                for p in (dw.get("parts") or [])[:200]:
                    if not isinstance(p, dict) or not p.get("drawing_no"):
                        continue
                    cid = ensure_drawing(c, t, company_id, p, reg)
                    if cid and cid != did:
                        ex = c.execute("SELECT id FROM dz_bom WHERE parent_id=? AND child_id=?", (did, cid)).fetchone()
                        if not ex:
                            b = c.execute("INSERT INTO dz_bom (tenant_id, parent_id, child_id, qty, created_at) VALUES (?,?,?,?,?)",
                                          (t, did, cid, p.get("qty") or 1, now))
                            reg.setdefault("bom", []).append(b.lastrowid)
        for did in touched:
            recompute_current(c, did)
        # 書類同士の紐づけ
        for o in c.execute("""SELECT id, doc_no, ref_no FROM dz_docs WHERE tenant_id=? AND company_id=? AND status='confirmed' AND id<>?""",
                           (t, company_id, d["id"])).fetchall():
            if (body.ref_no and o["doc_no"] == body.ref_no) or (body.doc_no and o["ref_no"] == body.doc_no) or o["id"] in (body.link_docs or []):
                link(c, t, d["id"], "doc", o["id"], None, ["番号の参照"], "rule", reg)
        # 案件
        deal_id = None if body.history else body.deal_id
        result = {"deal_id": None, "notes": []}
        if deal_id:
            if not c.execute("SELECT 1 FROM deals WHERE id=? AND tenant_id=?", (deal_id, t)).fetchone():
                raise HTTPException(400, "案件が見つかりません")
        elif body.create_deal and sales_router is not None and not body.history:
            title = _txt(body.deal_title or body.title or (lines[0].get("name") if lines else "") or kind, 120) or kind
            card = card_for(t, company["name"])
            stage = "見積提出" if kind in ("注文書", "注文変更書", "見積書") else None
            deal_id = sales.insert_deal(c, t, card if card.get("id") else {"id": "", "company": company["name"], "name": ""},
                                        title, stage, amount, created_note=f"図面タブの{kind}から作成")
            reg.setdefault("deals", []).append(deal_id)
            if kind == "注文書":
                sales_router.update_in_tx(c, t, deal_id, {"stage": "受注"})
        if deal_id:
            link(c, t, d["id"], "deal", deal_id, None, ["確定時に選択"], method, reg)
            result["deal_id"] = deal_id
            drow = dict(c.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone())
            sets = {}
            if kind in ("注文書", "注文変更書") and due_on:
                if not drow.get("delivery_on") or body.update_deal_due or kind == "注文変更書":
                    if drow.get("delivery_on") != due_on:
                        sets["delivery_on"] = due_on
                elif drow.get("delivery_on") != due_on:
                    result["notes"].append({"text": f"案件の納期は {drow['delivery_on']}、注文書の納期は {due_on} です", "due_on": due_on})
            if kind == "注文書" and amount and not drow.get("amount"):
                sets["amount"] = amount
            if sets and sales_router is not None:
                upd.setdefault("deals", {})[str(deal_id)] = {k: drow.get(k) for k in sets}
                sales_router.update_in_tx(c, t, deal_id, sets)
            if kind == "注文書" and drow["stage"] not in sales.CLOSED_STAGES and (drow.get("pipeline") or "sales") == "sales" and deal_id not in reg.get("deals", []):
                result["suggest_won"] = deal_id
        # 納期の予定（過去分には作らない）
        if kind in ("注文書", "注文変更書") and due_on and not body.history:
            ref_doc = d["id"]
            if kind == "注文変更書":
                orig = c.execute("""SELECT id FROM dz_docs WHERE tenant_id=? AND company_id=? AND kind='注文書' AND status='confirmed'
                    AND doc_no=? AND doc_no<>''""", (t, company_id, _txt(body.ref_no, 60))).fetchone()
                if orig:
                    ref_doc = orig["id"]
                    upd["orig_due"] = {"id": orig["id"], "due_on": c.execute("SELECT due_on FROM dz_docs WHERE id=?", (orig["id"],)).fetchone()["due_on"]}
                    c.execute("UPDATE dz_docs SET due_on=? WHERE id=?", (due_on, orig["id"]))
            qty = sum((x.get("qty") or 0) for x in lines if isinstance(x.get("qty"), (int, float)))
            name = _txt(body.title or (lines[0].get("name") if lines else "") or "注文", 40)
            title = f"納期：{name}（{company['name']}）" + (f" {qty:g}個" if qty else "")
            memo = "\n".join(x for x in [f"注文番号 {body.doc_no}" if body.doc_no else "", f"案件 #{deal_id}" if deal_id else "",
                                         f"図面タブ /zumen.html?doc={ref_doc}"] if x)
            upsert_due_event(c, t, f"dz_doc:{ref_doc}", due_on, title, memo, reg)
        if body.history:   # 過去分の印（納期の画面と現場には出さない。数字には入れる）
            h = _l(c.execute("SELECT hint_json FROM dz_docs WHERE id=?", (d["id"],)).fetchone()["hint_json"], {}) or {}
            h["history"] = 1
            c.execute("UPDATE dz_docs SET hint_json=? WHERE id=?", (_j(h), d["id"]))
        _index(c, d["id"])
        return {"reg": reg, "upd": upd, "before": before, "result": result, "company_id": company_id}

    @r.post("/docs/{doc_id}/confirm")
    def confirm(doc_id: int, body: ConfirmIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            c.execute("BEGIN IMMEDIATE")
            d = get_doc(c, t, doc_id)
            cand = candidates(c, t, d, card_companies(t))
            out = confirm_in_tx(c, t, d, body)
            top_co = cand["companies"][0] if cand["companies"] else None
            c.execute("""INSERT INTO dz_candidates (tenant_id, doc_id, field, candidates_json, chosen, was_top, created_at)
                VALUES (?,?,?,?,?,?,?)""", (t, doc_id, "company", _j(cand["companies"]), str(out["company_id"]),
                                            1 if top_co and top_co.get("company_id") == out["company_id"] else 0, _now()))
            c.execute("""INSERT INTO dz_candidates (tenant_id, doc_id, field, candidates_json, chosen, was_top, created_at)
                VALUES (?,?,?,?,?,?,?)""", (t, doc_id, "deal", _j(cand["deals"]), str(out["result"]["deal_id"] or ""),
                                            1 if cand["deal_pick"] and cand["deal_pick"] == out["result"]["deal_id"] else 0, _now()))
            eid = event(c, t, actor_of(body.actor), "confirm", f"doc:{doc_id}", {k: out[k] for k in ("reg", "upd", "before")})
            c.commit()
        notify(t, doc_id)
        return {"ok": True, "event_id": eid, **out["result"]}

    def notify(t: str, doc_id: int):
        with _conn() as c:
            st = _settings(c, t)
            if not st["slack_notify"]:
                return
            d = c.execute("SELECT * FROM dz_docs WHERE id=?", (doc_id,)).fetchone()
            if not d or d["kind"] != "注文書":
                return
            co = c.execute("SELECT name FROM dz_companies WHERE id=?", (d["company_id"],)).fetchone()
        try:
            import slack_bot
            ch = slack_bot.tenant_channel(t)
            if not ch or not slack_bot.token_for_channel(ch):
                return
            amt = f" {d['amount']:,}円" if d["amount"] else ""
            text = (f"*注文書を確定* <{slack_bot.app_url()}/zumen.html?doc={doc_id}|{slack_bot.slack_esc(co['name'] if co else '')}｜"
                    f"{slack_bot.slack_esc(d['title'] or '')}>{amt}" + (f"　納期 {d['due_on']}" if d["due_on"] else ""))
            threading.Thread(target=lambda: slack_bot.post(ch, None, text), daemon=True).start()
        except Exception:
            log.exception("Slackへの通知に失敗")

    class BulkIn(BaseModel):
        ids: list
        actor: Optional[str] = None

    def auto_body(c, t, d, cards) -> Optional[ConfirmIn]:
        """まとめて確定してよい書類なら確定内容を返す（自信が基準以上・会社が確実・種類がその他でない）"""
        st = _settings(c, t)
        if (d["ai_conf"] or 0) < st["bulk_threshold"] or d["error"]:
            return None
        cand = candidates(c, t, d, cards)
        v = _values(d)
        if v.get("kind") in (None, "その他") or any(w["level"] == "bad" for w in cand["warnings"]):
            return None
        co = cand["company"]
        if not co or co.get("score", 0) < 90 or co.get("kind") != "company":
            return None
        if any(x["status"] in ("same_rev", "unknown") for x in cand["drawings"]):
            return None
        return ConfirmIn(kind=v["kind"], direction=v.get("direction") or "受け取り", company_id=co["company_id"], doc_no=v.get("doc_no"),
                         ref_no=v.get("ref_no"), doc_date=v.get("doc_date"), amount=v.get("amount"), due_on=v.get("due_on"),
                         title=v.get("title"), lines=v.get("lines") or [], drawings=v.get("drawings") or [], deal_id=cand["deal_pick"])

    @r.post("/inbox/bulk-confirm")
    def bulk_confirm(body: BulkIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        done, skipped, events = [], [], []
        cards = card_companies(t)
        with _conn() as c:
            if not _settings(c, t)["bulk_confirm"]:
                raise HTTPException(400, "「まとめて確定」は設定でオフになっています")
        for i in [int(x) for x in body.ids[:200]]:
            with _conn() as c:
                c.execute("BEGIN IMMEDIATE")
                d = c.execute("SELECT * FROM dz_docs WHERE id=? AND tenant_id=?", (i, t)).fetchone()
                if not d or d["status"] != "review":
                    c.rollback()
                    skipped.append(i)
                    continue
                b = auto_body(c, t, d, cards)
                if not b:
                    c.rollback()
                    skipped.append(i)
                    continue
                b.actor = body.actor
                try:
                    out = confirm_in_tx(c, t, d, b, method="auto")
                except HTTPException:
                    c.rollback()
                    skipped.append(i)
                    continue
                events.append(event(c, t, actor_of(body.actor), "confirm", f"doc:{i}", {k: out[k] for k in ("reg", "upd", "before")}))
                c.commit()
                done.append(i)
        return {"confirmed": done, "skipped": skipped, "events": events}

    @r.get("/inbox/bulk-preview")
    def bulk_preview(session: dict = Depends(check_session)):
        t = session["tenant_id"]
        cards = card_companies(t)
        with _conn() as c:
            ids = [d["id"] for d in c.execute("SELECT * FROM dz_docs WHERE tenant_id=? AND status='review'", (t,)).fetchall()
                   if auto_body(c, t, d, cards)]
        return {"ids": ids}

    # ── 過去分のまとめ確定：会社と図番だけを確定し、案件・納期の予定は作らない ──
    def import_body(c, t, d, cards, ctx: dict = None) -> Optional[ConfirmIn]:
        """過去分のまとめ確定に使う中身。案件の候補は計算しない（過去分は案件に紐づけない）。
        ctx に会社の一覧と照合結果を持ち回り、数千件でも速く回す"""
        hint = _l(d["hint_json"], {}) or {}
        if not hint.get("import") or d["error"]:
            return None
        v = _values(d)
        kind = v.get("kind")
        if ctx is None:
            ctx = {}
        if "kinds" not in ctx:
            ctx["kinds"] = kinds_for(c, t)
            ctx["cos"], ctx["als"] = companies_of(c, t)
            ctx["memo"] = {}
        if kind not in ctx["kinds"]:
            return None
        ed = _l(d["edit_json"], {}) or {}

        def look(name):
            k = (name or "")
            if k not in ctx["memo"]:
                ctx["memo"][k] = zm.match_company(k, ctx["cos"], ctx["als"], cards, fuzzy=False) if k else []
            return ctx["memo"][k]
        cid, cname = None, None
        if ed.get("_company"):
            cid = ed["_company"].get("company_id")
            cname = None if cid else ed["_company"].get("name")
        else:
            for name in [v.get("counterparty")] + ([hint["folder"]] if hint.get("folder") else []) + \
                        [p for p in (hint.get("path") or "").split("/") if p and p != hint.get("folder")]:
                hit = next((x for x in look(name) if x["kind"] == "company" and x["score"] >= 85), None)
                if hit:
                    cid = hit["company_id"]
                    break
            if not cid:
                card = next((x for x in look(v.get("counterparty")) + look(hint.get("folder")) if x["kind"] == "card"), None)
                cname = card["name"] if card else hint.get("folder")
        if not cid and not cname:
            return None
        return ConfirmIn(kind=kind, direction=v.get("direction") or "受け取り", company_id=cid, company_name=cname,
                         doc_no=v.get("doc_no"), ref_no=v.get("ref_no"), doc_date=v.get("doc_date"), amount=v.get("amount"),
                         due_on=v.get("due_on"), title=v.get("title"), lines=v.get("lines") or [], drawings=v.get("drawings") or [],
                         history=True)

    @r.get("/inbox/import-preview")
    def import_preview(session: dict = Depends(check_session)):
        t = session["tenant_id"]
        cards = card_companies(t)
        ready, hold, folders, ctx = [], 0, {}, {}
        with _conn() as c:
            for d in c.execute("SELECT * FROM dz_docs WHERE tenant_id=? AND status='review' AND hint_json LIKE '%\"import\"%'", (t,)).fetchall():
                if import_body(c, t, d, cards, ctx):
                    ready.append(d["id"])
                else:
                    hold += 1
                f = (_l(d["hint_json"], {}) or {}).get("folder") or "（フォルダなし）"
                folders[f] = folders.get(f, 0) + 1
            q = c.execute("""SELECT COUNT(*) n FROM dz_ingest WHERE tenant_id=? AND source='import' AND status IN ('queued','reading')""", (t,)).fetchone()["n"]
        return {"ready": ready, "hold": hold, "reading": q,
                "folders": sorted(({"folder": k, "n": v} for k, v in folders.items()), key=lambda x: -x["n"])[:50]}

    @r.post("/inbox/import-confirm")
    def import_confirm(body: BulkIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        done, skipped, events = [], [], []
        cards = card_companies(t)
        ctx = {}
        for i in [int(x) for x in body.ids[:500]]:
            with _conn() as c:
                c.execute("BEGIN IMMEDIATE")
                d = c.execute("SELECT * FROM dz_docs WHERE id=? AND tenant_id=?", (i, t)).fetchone()
                if ctx.get("dirty"):   # 会社を新しく作ったら一覧を読み直す
                    ctx = {}
                b = import_body(c, t, d, cards, ctx) if d and d["status"] == "review" else None
                if not b:
                    c.rollback()
                    skipped.append(i)
                    continue
                b.actor = body.actor
                try:
                    out = confirm_in_tx(c, t, d, b, method="import")
                    if out["reg"].get("companies"):
                        ctx["dirty"] = True
                except HTTPException:
                    c.rollback()
                    skipped.append(i)
                    continue
                events.append(event(c, t, actor_of(body.actor), "confirm", f"doc:{i}", {k: out[k] for k in ("reg", "upd", "before")}))
                c.commit()
                done.append(i)
        return {"confirmed": done, "skipped": skipped, "events": events}

    # ── 元に戻す ──
    def undo_confirm(c, t: str, ev):
        data = _l(ev["data_json"], {}) or {}
        reg, upd, before = data.get("reg") or {}, data.get("upd") or {}, data.get("before") or {}
        doc_id = int(ev["target"].split(":")[1])
        d = c.execute("SELECT * FROM dz_docs WHERE id=?", (doc_id,)).fetchone()
        if not d or d["status"] != "confirmed":
            raise HTTPException(400, "この書類は確定済みではありません")
        drawings_touched = {r_["drawing_id"] for r_ in c.execute("SELECT drawing_id FROM dz_revisions WHERE doc_id=?", (doc_id,))}
        for cid in reg.get("cal_events", []):
            c.execute("DELETE FROM cal_events WHERE id=? AND tenant_id=?", (cid, t))
        for x in reg.get("cal_updated", []):
            c.execute("UPDATE cal_events SET day=?, title=?, memo=? WHERE id=? AND tenant_id=?", (x["day"], x["title"], x["memo"], x["id"], t))
        for did, vals in (upd.get("deals") or {}).items():
            if sales_router is not None and c.execute("SELECT 1 FROM deals WHERE id=?", (int(did),)).fetchone():
                sales_router.update_in_tx(c, t, int(did), vals)
        if upd.get("orig_due"):
            c.execute("UPDATE dz_docs SET due_on=? WHERE id=?", (upd["orig_due"]["due_on"], upd["orig_due"]["id"]))
        for lid in reg.get("links", []):
            c.execute("DELETE FROM dz_links WHERE id=?", (lid,))
        c.execute("DELETE FROM dz_lines WHERE doc_id=?", (doc_id,))
        for b in reg.get("bom", []):
            c.execute("DELETE FROM dz_bom WHERE id=?", (b,))
        for rid in reg.get("revisions", []):
            c.execute("DELETE FROM dz_revisions WHERE id=?", (rid,))
        for x in reg.get("drawing_fields", []):
            sets = {k: v for k, v in x.items() if k != "id"}
            if sets:
                c.execute(f"UPDATE dz_drawings SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), x["id"]))
        for did in reg.get("drawings", []):
            used = c.execute("""SELECT (SELECT COUNT(*) FROM dz_revisions WHERE drawing_id=?) + (SELECT COUNT(*) FROM dz_lines WHERE drawing_id=?)
                + (SELECT COUNT(*) FROM dz_notes WHERE drawing_id=?) + (SELECT COUNT(*) FROM dz_bom WHERE parent_id=? OR child_id=?) n""",
                             (did, did, did, did, did)).fetchone()["n"]
            if not used:
                c.execute("DELETE FROM dz_drawings WHERE id=?", (did,))
                drawings_touched.discard(did)
        for did in drawings_touched:
            if c.execute("SELECT 1 FROM dz_drawings WHERE id=?", (did,)).fetchone():
                recompute_current(c, did)
        for deal_id in reg.get("deals", []):
            other = c.execute("SELECT COUNT(*) n FROM dz_links WHERE target_type='deal' AND target_id=?", (deal_id,)).fetchone()["n"]
            if not other:
                c.execute("DELETE FROM deals WHERE id=? AND tenant_id=?", (deal_id, t))
        for aid in reg.get("aliases", []):
            c.execute("DELETE FROM dz_aliases WHERE id=?", (aid,))
        if upd.get("mail_domains"):
            c.execute("UPDATE dz_companies SET mail_domains=? WHERE id=?", (upd["mail_domains"]["old"], upd["mail_domains"]["id"]))
        c.execute("""UPDATE dz_docs SET status='review', company_id=NULL, kind=?, direction=?, doc_no=?, ref_no=?, doc_date=?, amount=?,
            due_on=?, title=?, edit_json=?, confirmed_by=NULL, confirmed_at=NULL, updated_at=? WHERE id=?""",
                  (before.get("kind"), before.get("direction"), before.get("doc_no"), before.get("ref_no"), before.get("doc_date"),
                   before.get("amount"), before.get("due_on"), before.get("title"), before.get("edit_json"), _now(), doc_id))
        for cid in reg.get("companies", []):
            used = c.execute("SELECT COUNT(*) n FROM dz_docs WHERE company_id=?", (cid,)).fetchone()["n"] + \
                c.execute("SELECT COUNT(*) n FROM dz_drawings WHERE company_id=?", (cid,)).fetchone()["n"]
            if not used:
                c.execute("DELETE FROM dz_aliases WHERE company_id=?", (cid,))
                c.execute("DELETE FROM dz_companies WHERE id=?", (cid,))
        c.execute("DELETE FROM dz_candidates WHERE doc_id=?", (doc_id,))
        _index(c, doc_id)

    def undo_merge(c, t: str, ev):
        data = _l(ev["data_json"], {}) or {}
        keep, rem = data["keep"], data["remove"]
        for tbl, rows in (data.get("moved") or {}).items():
            for rid, old in rows:
                col = "company_id" if tbl in ("dz_docs", "dz_drawings", "dz_aliases") else "drawing_id"
                c.execute(f"UPDATE {tbl} SET {col}=? WHERE id=?", (old, rid))
        for x in data.get("merged_drawings") or []:
            for tbl, col, rows in (("dz_revisions", "drawing_id", x["revisions"]), ("dz_lines", "drawing_id", x["lines"]),
                                   ("dz_notes", "drawing_id", x["notes"])):
                for rid in rows:
                    c.execute(f"UPDATE {tbl} SET {col}=? WHERE id=?", (x["from"], rid))
            for rid, colname in x.get("bom", []):
                c.execute(f"UPDATE dz_bom SET {colname}=? WHERE id=?", (x["from"], rid))
            c.execute("UPDATE dz_drawings SET merged_into=NULL WHERE id=?", (x["from"],))
            recompute_current(c, x["from"])
            recompute_current(c, x["to"])
        for aid in data.get("aliases_added") or []:
            c.execute("DELETE FROM dz_aliases WHERE id=?", (aid,))
        if data.get("kept"):
            k = data["kept"]
            c.execute("UPDATE dz_companies SET mail_domains=?, no_ai=?, cost_rate=?, card_company=? WHERE id=? AND tenant_id=?",
                      (k["mail_domains"], k["no_ai"], k["cost_rate"], k["card_company"], keep, t))
        if data.get("tpl_moved"):
            c.execute("UPDATE dz_templates SET company_id=? WHERE tenant_id=? AND company_id=?", (rem, t, keep))
        c.execute("UPDATE dz_companies SET merged_into=NULL WHERE id=? AND tenant_id=?", (rem, t))

    @r.post("/events/{event_id}/undo")
    def undo(event_id: int, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            c.execute("BEGIN IMMEDIATE")
            ev = c.execute("SELECT * FROM dz_events WHERE id=? AND tenant_id=?", (event_id, t)).fetchone()
            if not ev or ev["undone_at"]:
                raise HTTPException(400, "元に戻せる操作が見つかりませんでした")
            if ev["action"] == "confirm":
                if ev["at"] < (datetime.now() - timedelta(hours=UNDO_HOURS)).isoformat(timespec="seconds"):
                    raise HTTPException(400, f"確定から{UNDO_HOURS}時間を過ぎているため、元に戻せません")
                undo_confirm(c, t, ev)
            elif ev["action"] == "merge":
                undo_merge(c, t, ev)
            elif ev["action"] == "trash":
                doc_id = int(ev["target"].split(":")[1])
                st_ = (_l(ev["data_json"], {}) or {}).get("status") or "review"
                c.execute("UPDATE dz_docs SET status=?, trashed_at=NULL WHERE id=?", (st_, doc_id))
                for rv in c.execute("SELECT DISTINCT drawing_id FROM dz_revisions WHERE doc_id=?", (doc_id,)).fetchall():
                    recompute_current(c, rv["drawing_id"])
            else:
                raise HTTPException(400, "この操作は元に戻せません")
            c.execute("UPDATE dz_events SET undone_at=? WHERE id=?", (_now(), event_id))
            c.commit()
        return {"ok": True}

    # ── ファイルと画像 ──
    @r.get("/files/{file_id}/page/{page_no}")
    def page_image(file_id: int, page_no: int, size: str = "view", session: dict = Depends(check_session)):
        t = session["tenant_id"]
        if size not in ("view", "thumb", "hi"):
            raise HTTPException(400, "画像の大きさの指定が正しくありません")
        with _conn() as c:
            f = c.execute("SELECT * FROM dz_files WHERE id=? AND tenant_id=?", (file_id, t)).fetchone()
            if not f:
                raise HTTPException(404, "ファイルが見つかりません")
            st = storage.get_store()
            key = storage.page_key(f["sha256"], page_no, size)
            data = st.get(key)
            if data is None and size == "hi" and f["mime"] == "application/pdf":
                src = st.get(f["storage_key"])
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                    tf.write(src)
                    path = tf.name
                try:
                    data, _ = _jpeg(_pdf_render(path, page_no, 220), 6000, 85)
                finally:
                    os.unlink(path)
                st.put(key, data)
            if data is None and size == "hi":
                data = st.get(storage.page_key(f["sha256"], page_no, "view"))
            if data is None and not f["rendered"] and renderable(f):
                render_file(c, f)
                c.commit()
                data = st.get(key)
        if data is None:
            raise HTTPException(404, "画像がありません")
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    @r.get("/files/{file_id}/raw")
    def raw(file_id: int, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            f = c.execute("SELECT * FROM dz_files WHERE id=? AND tenant_id=?", (file_id, t)).fetchone()
        if not f:
            raise HTTPException(404, "ファイルが見つかりません")
        data = storage.get_store().get(f["storage_key"])
        if data is None:
            raise HTTPException(404, "原本が見つかりません")
        from urllib.parse import quote
        mime = f["mime"] if f["mime"] not in ("application/cad",) else "application/octet-stream"
        disp = "inline" if mime in ("application/pdf", "text/html") or mime.startswith("image/") else "attachment"
        hd = {"Content-Disposition": f"{disp}; filename*=UTF-8''{quote(f['filename'] or 'file')}", "Cache-Control": "private, max-age=3600"}
        if mime == "text/html":   # 見積書の控え（この画面で作ったHTML）。念のためスクリプトは動かさない
            hd["Content-Security-Policy"] = "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:"
            mime = "text/html; charset=utf-8"
        return Response(data, media_type=mime, headers=hd)

    # ── 会社 ──
    @r.get("/companies")
    def companies(q: str = "", session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            rows = c.execute("""SELECT co.*, COALESCE(dd.n, 0) docs, COALESCE(dw.n, 0) drawings, dd.last_date
                FROM dz_companies co
                LEFT JOIN (SELECT company_id, COUNT(*) n, MAX(doc_date) last_date FROM dz_docs WHERE tenant_id=? AND status='confirmed' GROUP BY company_id) dd ON dd.company_id=co.id
                LEFT JOIN (SELECT company_id, COUNT(*) n FROM dz_drawings WHERE tenant_id=? AND merged_into IS NULL GROUP BY company_id) dw ON dw.company_id=co.id
                WHERE co.tenant_id=? AND co.merged_into IS NULL ORDER BY co.name""", (t, t, t)).fetchall()
            als = {}
            for a in c.execute("SELECT * FROM dz_aliases WHERE tenant_id=? ORDER BY hits DESC", (t,)):
                als.setdefault(a["company_id"], []).append({"id": a["id"], "alias": a["alias"], "source": a["source"], "hits": a["hits"]})
        out = []
        ql = zm.norm_company(q)
        for x in rows:
            item = dict(x) | {"aliases": als.get(x["id"], [])}
            if ql and ql not in x["norm_name"] and not any(ql in zm.norm_company(a["alias"]) for a in item["aliases"]):
                continue
            out.append(item)
        return {"companies": out}

    class CompanyIn(BaseModel):
        name: Optional[str] = None
        card_company: Optional[str] = None
        mail_domains: Optional[str] = None
        is_group: Optional[bool] = None
        cost_rate: Optional[float] = None
        no_ai: Optional[bool] = None

    @r.post("/companies")
    def create_company(body: CompanyIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            reg = {}
            cid = ensure_company(c, t, body.name, reg, body.card_company, group_names(t))
            c.commit()
        return {"ok": True, "id": cid}

    @r.get("/companies/{cid}")
    def company(cid: int, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            co = c.execute("SELECT * FROM dz_companies WHERE id=? AND tenant_id=?", (cid, t)).fetchone()
            if not co:
                raise HTTPException(404, "会社が見つかりません")
            co = dict(co)
            docs = [dict(x) for x in c.execute("""SELECT d.id, d.kind, d.direction, d.doc_no, d.doc_date, d.amount, d.due_on, d.title, d.file_id,
                d.page_from, f.filename FROM dz_docs d JOIN dz_files f ON f.id=d.file_id
                WHERE d.company_id=? AND d.status='confirmed' ORDER BY COALESCE(d.doc_date, d.created_at) DESC""", (cid,))]
            drawings = [dict(x) for x in c.execute("""SELECT dr.id, dr.drawing_no, dr.name, dr.material, dr.thickness, r.rev, r.file_id, r.page_no,
                (SELECT COUNT(*) FROM dz_revisions x WHERE x.drawing_id=dr.id) revs
                FROM dz_drawings dr LEFT JOIN dz_revisions r ON r.id=dr.current_rev_id
                WHERE dr.company_id=? AND dr.merged_into IS NULL ORDER BY dr.drawing_no""", (cid,))]
            aliases = [dict(x) for x in c.execute("SELECT id, alias, source, hits FROM dz_aliases WHERE company_id=? ORDER BY hits DESC", (cid,))]
            deals = [{k: v for k, v in x.items() if k not in ("doc_nos", "drawing_nos", "quote_amounts")}
                     for x in deals_for_company(c, t, co)]
        cn = zm.norm_company(co.get("card_company") or co["name"])
        try:
            contacts = [{"id": x["id"], "name": x.get("name"), "department_position": x.get("department_position"), "email": x.get("email"),
                         "phone": x.get("mobile") or x.get("phone")} for x in fetch_cards(t) if zm.norm_company(x.get("company")) == cn][:30]
        except Exception:
            contacts = []
        return {"company": co, "docs": docs, "drawings": drawings, "aliases": aliases, "deals": deals, "contacts": contacts}

    @r.put("/companies/{cid}")
    def update_company(cid: int, body: CompanyIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            co = c.execute("SELECT * FROM dz_companies WHERE id=? AND tenant_id=?", (cid, t)).fetchone()
            if not co:
                raise HTTPException(404, "会社が見つかりません")
            sets = {}
            if body.name is not None:
                name = _txt(body.name)
                if not name:
                    raise HTTPException(400, "会社名を入力してください")
                n = zm.norm_company(name)
                other = c.execute("SELECT id FROM dz_companies WHERE tenant_id=? AND norm_name=? AND id<>? AND merged_into IS NULL", (t, n, cid)).fetchone()
                if other:
                    raise HTTPException(400, "同じ名前の会社がすでにあります。「統合」を使ってまとめてください")
                if n != co["norm_name"]:
                    add_alias(c, t, cid, co["name"], "rename", {})
                sets.update(name=name, norm_name=n)
            if body.card_company is not None:
                sets["card_company"] = _txt(body.card_company) or None
            if body.mail_domains is not None:
                sets["mail_domains"] = ",".join(x.strip().lower().lstrip("@") for x in re.split(r"[,\s、]+", body.mail_domains) if x.strip())
            if body.is_group is not None:
                sets["is_group"] = 1 if body.is_group else 0
            if body.no_ai is not None:
                sets["no_ai"] = 1 if body.no_ai else 0
            if body.cost_rate is not None:
                if not (0 <= body.cost_rate <= 1.5):
                    raise HTTPException(400, "原価率は0〜150%の範囲で入力してください")
                sets["cost_rate"] = body.cost_rate
            if sets:
                c.execute(f"UPDATE dz_companies SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), cid))
                for d in c.execute("SELECT id FROM dz_docs WHERE company_id=?", (cid,)).fetchall():
                    _index(c, d["id"])
            c.commit()
        return {"ok": True}

    class AliasIn(BaseModel):
        alias: str

    @r.post("/companies/{cid}/aliases")
    def add_alias_api(cid: int, body: AliasIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            if not c.execute("SELECT 1 FROM dz_companies WHERE id=? AND tenant_id=?", (cid, t)).fetchone():
                raise HTTPException(404, "会社が見つかりません")
            n = zm.norm_company(body.alias)
            ex = c.execute("SELECT company_id FROM dz_aliases WHERE tenant_id=? AND norm_alias=?", (t, n)).fetchone()
            if ex and ex["company_id"] != cid:
                other = c.execute("SELECT name FROM dz_companies WHERE id=?", (ex["company_id"],)).fetchone()
                raise HTTPException(400, f"この別名は「{other['name'] if other else '別の会社'}」に登録されています")
            add_alias(c, t, cid, body.alias, "manual", {})
            c.commit()
        return {"ok": True}

    @r.delete("/aliases/{aid}")
    def del_alias(aid: int, session: dict = Depends(check_session)):
        with _conn() as c:
            if c.execute("DELETE FROM dz_aliases WHERE id=? AND tenant_id=?", (aid, session["tenant_id"])).rowcount == 0:
                raise HTTPException(404, "見つかりません")
            c.commit()
        return {"ok": True}

    class MergeIn(BaseModel):
        keep: int
        remove: int
        actor: Optional[str] = None

    @r.post("/companies/merge")
    def merge(body: MergeIn, session: dict = Depends(check_session)):
        """2つの会社フォルダを1つにまとめる（書類・図番・別名を keep へ移す。図番が重なれば1つにまとめる）"""
        t = session["tenant_id"]
        if body.keep == body.remove:
            raise HTTPException(400, "同じ会社どうしは統合できません")
        with _conn() as c:
            c.execute("BEGIN IMMEDIATE")
            keep = c.execute("SELECT * FROM dz_companies WHERE id=? AND tenant_id=? AND merged_into IS NULL", (body.keep, t)).fetchone()
            rem = c.execute("SELECT * FROM dz_companies WHERE id=? AND tenant_id=? AND merged_into IS NULL", (body.remove, t)).fetchone()
            if not keep or not rem:
                raise HTTPException(404, "会社が見つかりません")
            moved, merged, aliases_added = {}, [], []
            for x in c.execute("SELECT id FROM dz_docs WHERE company_id=?", (rem["id"],)).fetchall():
                moved.setdefault("dz_docs", []).append([x["id"], rem["id"]])
                c.execute("UPDATE dz_docs SET company_id=? WHERE id=?", (keep["id"], x["id"]))
            for x in c.execute("SELECT id FROM dz_aliases WHERE company_id=?", (rem["id"],)).fetchall():
                moved.setdefault("dz_aliases", []).append([x["id"], rem["id"]])
                c.execute("UPDATE dz_aliases SET company_id=? WHERE id=?", (keep["id"], x["id"]))
            for dr in c.execute("SELECT * FROM dz_drawings WHERE company_id=? AND merged_into IS NULL", (rem["id"],)).fetchall():
                twin = c.execute("SELECT id FROM dz_drawings WHERE company_id=? AND norm_no=? AND merged_into IS NULL", (keep["id"], dr["norm_no"])).fetchone()
                if twin:
                    m = {"from": dr["id"], "to": twin["id"],
                         "revisions": [x["id"] for x in c.execute("SELECT id FROM dz_revisions WHERE drawing_id=?", (dr["id"],))],
                         "lines": [x["id"] for x in c.execute("SELECT id FROM dz_lines WHERE drawing_id=?", (dr["id"],))],
                         "notes": [x["id"] for x in c.execute("SELECT id FROM dz_notes WHERE drawing_id=?", (dr["id"],))],
                         "bom": [[x["id"], "parent_id"] for x in c.execute("SELECT id FROM dz_bom WHERE parent_id=?", (dr["id"],))] +
                                [[x["id"], "child_id"] for x in c.execute("SELECT id FROM dz_bom WHERE child_id=?", (dr["id"],))]}
                    for tbl in ("dz_revisions", "dz_lines", "dz_notes"):
                        c.execute(f"UPDATE {tbl} SET drawing_id=? WHERE drawing_id=?", (twin["id"], dr["id"]))
                    c.execute("UPDATE OR IGNORE dz_bom SET parent_id=? WHERE parent_id=?", (twin["id"], dr["id"]))
                    c.execute("UPDATE OR IGNORE dz_bom SET child_id=? WHERE child_id=?", (twin["id"], dr["id"]))
                    c.execute("UPDATE dz_drawings SET merged_into=? WHERE id=?", (twin["id"], dr["id"]))
                    recompute_current(c, twin["id"])
                    merged.append(m)
                else:
                    moved.setdefault("dz_drawings", []).append([dr["id"], rem["id"]])
                    c.execute("UPDATE dz_drawings SET company_id=? WHERE id=?", (keep["id"], dr["id"]))
            # 会社の設定も引き継ぐ（メールのドメイン・AIに送らない・原価率・名刺の会社名・逆算の工程）
            kept = {k: keep[k] for k in ("mail_domains", "no_ai", "cost_rate", "card_company")}
            doms = [x for x in (keep["mail_domains"] or "").split(",") if x]
            doms += [x for x in (rem["mail_domains"] or "").split(",") if x and x not in doms]
            c.execute("UPDATE dz_companies SET mail_domains=?, no_ai=?, cost_rate=?, card_company=? WHERE id=?",
                      (",".join(doms) or None, 1 if (keep["no_ai"] or rem["no_ai"]) else 0,
                       keep["cost_rate"] if keep["cost_rate"] is not None else rem["cost_rate"],
                       keep["card_company"] or rem["card_company"], keep["id"]))
            tpl_moved = False
            try:
                if not c.execute("SELECT 1 FROM dz_templates WHERE tenant_id=? AND company_id=?", (t, keep["id"])).fetchone():
                    tpl_moved = c.execute("UPDATE dz_templates SET company_id=? WHERE tenant_id=? AND company_id=?", (keep["id"], t, rem["id"])).rowcount > 0
            except Exception:
                pass
            reg = {}
            add_alias(c, t, keep["id"], rem["name"], "merge", reg)
            aliases_added += reg.get("aliases", [])
            c.execute("UPDATE dz_companies SET merged_into=? WHERE id=?", (keep["id"], rem["id"]))
            eid = event(c, t, actor_of(body.actor), "merge", f"company:{keep['id']}",
                        {"keep": keep["id"], "remove": rem["id"], "moved": moved, "merged_drawings": merged, "aliases_added": aliases_added,
                         "kept": kept, "tpl_moved": tpl_moved})
            for x in c.execute("SELECT id FROM dz_docs WHERE company_id=?", (keep["id"],)).fetchall():
                _index(c, x["id"])
            c.commit()
        return {"ok": True, "event_id": eid}

    # ── 図番 ──
    @r.get("/drawings")
    def drawings(q: str = "", company_id: Optional[int] = None, material: str = "", limit: int = 300, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        sql = """SELECT dr.id, dr.drawing_no, dr.name, dr.material, dr.thickness, dr.surface, dr.company_id, co.name company,
            r.rev, r.file_id, r.page_no, r.created_at rev_at, (SELECT COUNT(*) FROM dz_revisions x WHERE x.drawing_id=dr.id) revs,
            (SELECT COUNT(*) FROM dz_bom b WHERE b.parent_id=dr.id) children
            FROM dz_drawings dr JOIN dz_companies co ON co.id=dr.company_id LEFT JOIN dz_revisions r ON r.id=dr.current_rev_id
            WHERE dr.tenant_id=? AND dr.merged_into IS NULL"""
        args = [t]
        if company_id:
            sql += " AND dr.company_id=?"
            args.append(company_id)
        if material:
            sql += " AND dr.material LIKE ?"
            args.append(f"%{material}%")
        for w in [x for x in re.split(r"\s+", q.strip()) if x]:
            sql += " AND (REPLACE(REPLACE(dr.norm_no,'-',''),'_','') LIKE ? OR dr.name LIKE ? OR dr.material LIKE ? OR co.name LIKE ?)"
            args += [f"%{re.sub(r'[-_]', '', zm.norm_drawing_no(w))}%", f"%{w}%", f"%{w}%", f"%{w}%"]
        sql += " ORDER BY COALESCE(r.created_at, dr.updated_at) DESC LIMIT ?"
        args.append(max(1, min(limit, 1000)))
        with _conn() as c:
            return {"drawings": [dict(x) for x in c.execute(sql, args)]}

    def drawing_detail(c, t: str, did: int) -> dict:
        dr = c.execute("""SELECT dr.*, co.name company FROM dz_drawings dr JOIN dz_companies co ON co.id=dr.company_id
            WHERE dr.id=? AND dr.tenant_id=?""", (did, t)).fetchone()
        if not dr:
            raise HTTPException(404, "図番が見つかりません")
        if dr["merged_into"]:
            return drawing_detail(c, t, dr["merged_into"])
        revs = [dict(x) for x in c.execute("""SELECT r.*, d.doc_date, d.confirmed_by, f.filename FROM dz_revisions r
            JOIN dz_docs d ON d.id=r.doc_id JOIN dz_files f ON f.id=r.file_id WHERE r.drawing_id=? AND d.status='confirmed'""", (did,))]
        revs.sort(key=_rev_sort_key, reverse=True)
        prices = [dict(x) for x in c.execute("""SELECT l.qty, l.unit_price, l.amount, l.rev, d.kind, d.doc_date, d.doc_no, d.id doc_id
            FROM dz_lines l JOIN dz_docs d ON d.id=l.doc_id WHERE l.drawing_id=? AND d.status='confirmed'
            ORDER BY COALESCE(d.doc_date, d.created_at) DESC LIMIT 100""", (did,))]
        doc_ids = {x["doc_id"] for x in revs} | {x["doc_id"] for x in prices}
        docs, deals = [], {}
        for di in doc_ids:
            dd = c.execute("SELECT id, kind, doc_no, doc_date, amount, due_on, file_id, page_from FROM dz_docs WHERE id=?", (di,)).fetchone()
            if dd:
                docs.append(dict(dd))
            for ln in c.execute("SELECT target_id FROM dz_links WHERE doc_id=? AND target_type='deal'", (di,)):
                row = c.execute("SELECT id, title, stage, delivery_on FROM deals WHERE id=?", (ln["target_id"],)).fetchone()
                if row:
                    deals[row["id"]] = dict(row)
        notes = [dict(x) for x in c.execute("""SELECT * FROM dz_notes WHERE drawing_id=? AND deleted_at IS NULL ORDER BY id DESC""", (did,))]
        children = [dict(x) for x in c.execute("""SELECT b.id bom_id, b.qty, dr.id, dr.drawing_no, dr.name, r.rev FROM dz_bom b
            JOIN dz_drawings dr ON dr.id=b.child_id LEFT JOIN dz_revisions r ON r.id=dr.current_rev_id WHERE b.parent_id=? ORDER BY dr.drawing_no""", (did,))]
        parents = [dict(x) for x in c.execute("""SELECT b.id bom_id, b.qty, dr.id, dr.drawing_no, dr.name, r.rev FROM dz_bom b
            JOIN dz_drawings dr ON dr.id=b.parent_id LEFT JOIN dz_revisions r ON r.id=dr.current_rev_id WHERE b.child_id=? ORDER BY dr.drawing_no""", (did,))]
        for p in parents:   # 子の版が親の最新版より後に上がっていれば印を出す
            pr = current_rev(c, p["id"])
            cr = current_rev(c, did)
            p["child_newer"] = bool(pr and cr and cr["created_at"] > pr["created_at"])
        similar = []
        if dr["material"] or dr["name"]:
            words = zm._words(dr["name"] or "")
            for x in c.execute("""SELECT dr.id, dr.drawing_no, dr.name, dr.material, dr.thickness, co.name company, r.file_id, r.page_no
                    FROM dz_drawings dr JOIN dz_companies co ON co.id=dr.company_id LEFT JOIN dz_revisions r ON r.id=dr.current_rev_id
                    WHERE dr.tenant_id=? AND dr.id<>? AND dr.merged_into IS NULL AND r.id IS NOT NULL LIMIT 3000""", (t, did)):
                s = 0
                if dr["material"] and x["material"] and zm.norm_company(dr["material"]) == zm.norm_company(x["material"]):
                    s += 2
                if dr["thickness"] and x["thickness"] and dr["thickness"] == x["thickness"]:
                    s += 2
                s += len(words & zm._words(x["name"] or "")) * 2
                if x["company"] == dr["company"]:
                    s += 1
                if s >= 4:
                    similar.append((s, dict(x)))
            similar = [x for _, x in sorted(similar, key=lambda y: -y[0])[:8]]
        cur = current_rev(c, did)
        return {"drawing": dict(dr), "current": cur, "revisions": revs, "prices": prices, "docs": sorted(docs, key=lambda x: x.get("doc_date") or "", reverse=True),
                "deals": list(deals.values()), "notes": notes, "children": children, "parents": parents, "similar": similar}

    @r.get("/drawings/{did}")
    def drawing(did: int, session: dict = Depends(check_session)):
        with _conn() as c:
            return drawing_detail(c, session["tenant_id"], did)

    class DrawingIn(BaseModel):
        drawing_no: Optional[str] = None
        name: Optional[str] = None
        material: Optional[str] = None
        thickness: Optional[str] = None
        surface: Optional[str] = None
        tags: Optional[str] = None

    @r.put("/drawings/{did}")
    def update_drawing(did: int, body: DrawingIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            dr = c.execute("SELECT * FROM dz_drawings WHERE id=? AND tenant_id=?", (did, t)).fetchone()
            if not dr:
                raise HTTPException(404, "図番が見つかりません")
            sets = {k: _txt(v, 120) for k, v in body.model_dump(exclude_none=True).items() if k != "drawing_no"}
            if body.drawing_no is not None:
                n = zm.norm_drawing_no(body.drawing_no)
                if not n:
                    raise HTTPException(400, "図番を入力してください")
                if c.execute("SELECT 1 FROM dz_drawings WHERE company_id=? AND norm_no=? AND id<>? AND merged_into IS NULL", (dr["company_id"], n, did)).fetchone():
                    raise HTTPException(400, "この会社には同じ図番がすでにあります")
                sets.update(drawing_no=_txt(body.drawing_no, 80), norm_no=n)
            if sets:
                c.execute(f"UPDATE dz_drawings SET {', '.join(k + '=?' for k in sets)}, updated_at=? WHERE id=?", (*sets.values(), _now(), did))
                c.commit()
        return {"ok": True}

    class RevIn(BaseModel):
        rev: Optional[str] = None
        change_note: Optional[str] = None

    @r.put("/revisions/{rid}")
    def update_rev(rid: int, body: RevIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            rv = c.execute("SELECT * FROM dz_revisions WHERE id=? AND tenant_id=?", (rid, t)).fetchone()
            if not rv:
                raise HTTPException(404, "版が見つかりません")
            sets = {k: _txt(v, 300) for k, v in body.model_dump(exclude_none=True).items()}
            if sets:
                c.execute(f"UPDATE dz_revisions SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), rid))
                recompute_current(c, rv["drawing_id"])
                c.commit()
        return {"ok": True}

    class BomIn(BaseModel):
        child_no: str
        name: Optional[str] = None
        qty: Optional[float] = 1

    @r.post("/drawings/{did}/bom")
    def add_bom(did: int, body: BomIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            dr = c.execute("SELECT * FROM dz_drawings WHERE id=? AND tenant_id=?", (did, t)).fetchone()
            if not dr:
                raise HTTPException(404, "図番が見つかりません")
            cid = ensure_drawing(c, t, dr["company_id"], {"drawing_no": body.child_no, "name": body.name}, {})
            if not cid or cid == did:
                raise HTTPException(400, "部品の図番が正しくありません")
            if c.execute("SELECT 1 FROM dz_bom WHERE parent_id=? AND child_id=?", (cid, did)).fetchone():
                raise HTTPException(400, "この2つは、組立図と部品の関係が逆向きで登録済みです")
            c.execute("INSERT OR REPLACE INTO dz_bom (tenant_id, parent_id, child_id, qty, created_at) VALUES (?,?,?,?,?)",
                      (t, did, cid, body.qty or 1, _now()))
            c.commit()
        return {"ok": True, "child_id": cid}

    @r.delete("/bom/{bid}")
    def del_bom(bid: int, session: dict = Depends(check_session)):
        with _conn() as c:
            if c.execute("DELETE FROM dz_bom WHERE id=? AND tenant_id=?", (bid, session["tenant_id"])).rowcount == 0:
                raise HTTPException(404, "見つかりません")
            c.commit()
        return {"ok": True}

    # ── メモ・写真 ──
    class NoteIn(BaseModel):
        drawing_id: Optional[int] = None
        deal_id: Optional[int] = None
        kind: str = "加工メモ"
        body: str
        author: Optional[str] = None

    NOTE_KINDS = ("加工メモ", "不具合", "ノウハウ", "現場写真")

    @r.post("/notes")
    def add_note(body: NoteIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        if body.kind not in NOTE_KINDS:
            raise HTTPException(400, "メモの種類が正しくありません")
        text = _txt(body.body, 3000)
        if not text:
            raise HTTPException(400, "メモの内容を入力してください")
        with _conn() as c:
            if body.drawing_id and not c.execute("SELECT 1 FROM dz_drawings WHERE id=? AND tenant_id=?", (body.drawing_id, t)).fetchone():
                raise HTTPException(404, "図番が見つかりません")
            nid = c.execute("""INSERT INTO dz_notes (tenant_id, drawing_id, deal_id, kind, body, author, created_at) VALUES (?,?,?,?,?,?,?)""",
                            (t, body.drawing_id, body.deal_id, body.kind, text, actor_of(body.author), _now())).lastrowid
            c.commit()
        return {"ok": True, "id": nid}

    @r.delete("/notes/{nid}")
    def del_note(nid: int, session: dict = Depends(check_session)):
        with _conn() as c:
            if c.execute("UPDATE dz_notes SET deleted_at=? WHERE id=? AND tenant_id=?", (_now(), nid, session["tenant_id"])).rowcount == 0:
                raise HTTPException(404, "見つかりません")
            c.commit()
        return {"ok": True}

    @r.post("/photo")
    async def photo(file: UploadFile = File(...), drawing_id: Optional[int] = Form(None), deal_id: Optional[int] = Form(None),
                    body: str = Form(""), kind: str = Form("現場写真"), author: Optional[str] = Form(None),
                    session: dict = Depends(check_session)):
        """現場の写真（AIには回さず、その場で図番・案件に付ける）"""
        t = session["tenant_id"]
        data = await file.read(40 * 1024 * 1024 + 1)
        if len(data) > 40 * 1024 * 1024:
            raise HTTPException(400, "写真が大きすぎます（40MBまで）")
        mime = sniff(data, file.filename or "photo.jpg")
        if not mime.startswith("image/"):
            raise HTTPException(400, "写真のファイルを選択してください")
        if kind not in NOTE_KINDS:
            kind = "現場写真"
        frames = _open_images(data, mime)
        jpg, _ = _jpeg(frames[0], 2400, 85)   # 写真は縮めて保存
        sha = hashlib.sha256(jpg).hexdigest()
        st = storage.get_store()
        key = storage.orig_key(t, sha, ".jpg")
        st.put(key, jpg)
        with _conn() as c:
            c.execute("BEGIN IMMEDIATE")
            ex = c.execute("SELECT * FROM dz_files WHERE tenant_id=? AND sha256=?", (t, sha)).fetchone()
            fid = ex["id"] if ex else c.execute("""INSERT INTO dz_files (tenant_id, sha256, filename, ext, mime, size, storage_key, created_at)
                VALUES (?,?,?,?,?,?,?,?)""", (t, sha, os.path.basename(file.filename or "photo.jpg")[:200], ".jpg", "image/jpeg", len(jpg), key, _now())).lastrowid
            frow = c.execute("SELECT * FROM dz_files WHERE id=?", (fid,)).fetchone()
            if not frow["rendered"]:
                render_file(c, frow)
            nid = c.execute("""INSERT INTO dz_notes (tenant_id, drawing_id, deal_id, kind, body, photo_file_id, author, created_at)
                VALUES (?,?,?,?,?,?,?,?)""", (t, drawing_id, deal_id, kind, _txt(body, 3000), fid, actor_of(author), _now())).lastrowid
            c.commit()
        return {"ok": True, "id": nid, "file_id": fid}

    # ── 検索 ──
    @r.get("/search")
    def search(q: str = "", kind: str = "", company_id: Optional[int] = None, frm: str = Query("", alias="from"), to: str = "",
               amount_min: Optional[int] = None, amount_max: Optional[int] = None, direction: str = "",
               status: str = "confirmed", offset: int = 0, session: dict = Depends(check_session)):
        """確定した書類を、取引先・日付の範囲・金額の範囲・種類・本文の文字で探す（条件はすべて同時に使える）"""
        t = session["tenant_id"]
        for v in (frm, to):
            if v and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                raise HTTPException(400, "日付の形式が正しくありません")
        where, args = ["d.tenant_id=?", "d.status!='trashed'"], [t]
        if status != "all":
            where.append("d.status=?"); args.append(status)
        with _conn() as c:
            if q.strip():
                ids = search_doc_ids(c, t, q, 2000)
                if not ids:
                    return {"docs": [], "total": 0, "mode": FTS["mode"]}
                where.append(f"d.id IN ({','.join('?' * len(ids))})"); args += ids
            if kind:
                where.append("d.kind=?"); args.append(kind)
            if direction in ("受け取り", "発行"):
                where.append("d.direction=?"); args.append(direction)
            if company_id:
                where.append("d.company_id=?"); args.append(company_id)
            dd = "COALESCE(d.doc_date, substr(d.created_at,1,10))"
            if frm:
                where.append(f"{dd}>=?"); args.append(frm)
            if to:
                where.append(f"{dd}<=?"); args.append(to)
            if amount_min is not None:
                where.append("d.amount>=?"); args.append(amount_min)
            if amount_max is not None:
                where.append("d.amount<=?"); args.append(amount_max)
            w = " AND ".join(where)
            total = c.execute(f"SELECT COUNT(*) FROM dz_docs d WHERE {w}", args).fetchone()[0]
            rows = c.execute(f"""SELECT d.*, f.filename, co.name company FROM dz_docs d JOIN dz_files f ON f.id=d.file_id
                LEFT JOIN dz_companies co ON co.id=d.company_id WHERE {w}
                ORDER BY {dd} DESC, d.id DESC LIMIT 300 OFFSET ?""", args + [max(0, offset)]).fetchall()
            out = []
            for d in rows:
                v = _values(d) if d["status"] != "confirmed" else {}
                out.append({"id": d["id"], "status": d["status"], "kind": d["kind"] or v.get("kind"), "company": d["company"] or d["counterparty_raw"] or v.get("counterparty"),
                            "company_id": d["company_id"], "doc_no": d["doc_no"] or v.get("doc_no"), "doc_date": d["doc_date"] or v.get("doc_date"),
                            "amount": d["amount"] if d["amount"] is not None else v.get("amount"), "direction": d["direction"],
                            "due_on": d["due_on"], "title": d["title"] or v.get("title"), "file_id": d["file_id"], "page_from": d["page_from"] or 1,
                            "filename": d["filename"]})
        return {"docs": out, "total": total, "mode": FTS["mode"]}

    # ── 案件の書類（営業の案件画面から） ──
    @r.get("/deals/{deal_id}/docs")
    def deal_docs(deal_id: int, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            if not c.execute("SELECT 1 FROM deals WHERE id=? AND tenant_id=?", (deal_id, t)).fetchone():
                raise HTTPException(404, "案件が見つかりません")
            rows = c.execute("""SELECT d.id, d.kind, d.doc_no, d.doc_date, d.amount, d.due_on, d.title, d.file_id, d.page_from, f.filename, f.mime
                FROM dz_links l JOIN dz_docs d ON d.id=l.doc_id JOIN dz_files f ON f.id=d.file_id
                WHERE l.tenant_id=? AND l.target_type='deal' AND l.target_id=? AND d.status='confirmed'
                ORDER BY COALESCE(d.doc_date, d.created_at) DESC""", (t, deal_id)).fetchall()
            docs = [dict(x) for x in rows]
            dids = set()
            for d in docs:
                for x in c.execute("SELECT drawing_id FROM dz_revisions WHERE doc_id=?", (d["id"],)):
                    dids.add(x["drawing_id"])
                for x in c.execute("SELECT drawing_id FROM dz_lines WHERE doc_id=? AND drawing_id IS NOT NULL", (d["id"],)):
                    dids.add(x["drawing_id"])
            drawings = []
            for did in dids:
                dr = c.execute("""SELECT dr.id, dr.drawing_no, dr.name, r.rev, r.file_id, r.page_no FROM dz_drawings dr
                    LEFT JOIN dz_revisions r ON r.id=dr.current_rev_id WHERE dr.id=?""", (did,)).fetchone()
                if dr:
                    drawings.append(dict(dr))
            # 「deal_id": 1」が 12 や 13 に当たらないよう、後ろの区切りまで見る
            pats = (f'%"deal_id": {deal_id},%', f'%"deal_id": {deal_id}}}%')
            pending = c.execute("""SELECT COUNT(*) n FROM dz_docs WHERE tenant_id=? AND status='review' AND (hint_json LIKE ? OR hint_json LIKE ?)""",
                                (t, *pats)).fetchone()["n"]
            pending += c.execute("""SELECT COUNT(*) n FROM dz_ingest WHERE tenant_id=? AND status IN ('queued','reading') AND (hint_json LIKE ? OR hint_json LIKE ?)""",
                                 (t, *pats)).fetchone()["n"]
            co_id = next((d["company_id"] for d in c.execute(
                "SELECT d.company_id FROM dz_links l JOIN dz_docs d ON d.id=l.doc_id WHERE l.tenant_id=? AND l.target_type='deal' AND l.target_id=? AND d.company_id IS NOT NULL",
                (t, deal_id))), None)
            if not co_id:
                snap = c.execute("SELECT company_snapshot FROM deals WHERE id=?", (deal_id,)).fetchone()["company_snapshot"]
                n = zm.norm_company(snap or "")
                row = c.execute("""SELECT co.id FROM dz_companies co LEFT JOIN dz_aliases a ON a.company_id=co.id
                    WHERE co.tenant_id=? AND co.merged_into IS NULL AND (co.norm_name=? OR a.norm_alias=?) LIMIT 1""", (t, n, n)).fetchone() if n else None
                co_id = row["id"] if row else None
        return {"docs": docs, "drawings": sorted(drawings, key=lambda x: x["drawing_no"]), "pending": pending, "company_id": co_id}

    # ── 納期（カレンダー・ガント） ──
    def order_rows(c, t: str, active_only: bool = False) -> list:
        """注文書（確定）と、その案件。active_only は納期・現場用で、過去分として取り込んだ注文書を除く"""
        out = []
        links = {r_["doc_id"]: r_["target_id"] for r_ in c.execute(
            "SELECT doc_id, MIN(target_id) target_id FROM dz_links WHERE tenant_id=? AND target_type='deal' GROUP BY doc_id", (t,))}
        for d in c.execute("""SELECT d.*, co.name company, co.is_group FROM dz_docs d LEFT JOIN dz_companies co ON co.id=d.company_id
                WHERE d.tenant_id=? AND d.status='confirmed' AND d.kind='注文書'""", (t,)):
            if active_only and '"history": 1' in (d["hint_json"] or ""):
                continue
            out.append(dict(d) | {"deal_id": links.get(d["id"])})
        return out

    @r.get("/schedule")
    def schedule(frm: str = Query("", alias="from"), to: str = "", session: dict = Depends(check_session)):
        t = session["tenant_id"]
        today = date.today()
        f = frm or (today - timedelta(days=14)).isoformat()
        e = to or (today + timedelta(days=90)).isoformat()
        rows = []
        with _conn() as c:
            orders = order_rows(c, t, active_only=True)
            by_deal = {}
            for o in orders:
                if o["deal_id"]:
                    by_deal.setdefault(o["deal_id"], []).append(o)
            seen = set()
            for deal_id, os_ in by_deal.items():
                dl = c.execute("SELECT * FROM deals WHERE id=? AND tenant_id=?", (deal_id, t)).fetchone()
                if not dl:
                    continue
                state = c.execute("SELECT * FROM dz_deal_state WHERE tenant_id=? AND deal_id=?", (t, deal_id)).fetchone()
                due = min([o["due_on"] for o in os_ if o["due_on"]] or [dl["delivery_on"]] or [None]) if any(o["due_on"] for o in os_) else dl["delivery_on"]
                start = min([o["doc_date"] for o in os_ if o["doc_date"]] or [(dl["created_at"] or "")[:10]])
                rows.append({"deal_id": deal_id, "title": dl["title"], "company": os_[0]["company"], "company_id": os_[0]["company_id"], "stage": dl["stage"], "pipeline": dl["pipeline"] or "sales",
                             "start": start, "due": due, "deal_due": dl["delivery_on"], "amount": sum(o["amount"] or 0 for o in os_),
                             "order_ids": [o["id"] for o in os_], "shipped_at": state["shipped_at"] if state else None,
                             "started_at": state["started_at"] if state else None})
                seen.add(deal_id)
            for o in orders:
                if o["deal_id"]:
                    continue
                rows.append({"deal_id": None, "doc_id": o["id"], "title": o["title"] or o["doc_no"] or "注文書", "company": o["company"], "stage": None,
                             "pipeline": None, "start": o["doc_date"], "due": o["due_on"], "deal_due": None, "amount": o["amount"] or 0,
                             "order_ids": [o["id"]], "shipped_at": None, "started_at": None})
            for dl in c.execute("""SELECT * FROM deals WHERE tenant_id=? AND pipeline='prod' AND delivery_on IS NOT NULL""", (t,)):
                if dl["id"] in seen or dl["stage"] in sales.CLOSED_STAGES:
                    continue
                rows.append({"deal_id": dl["id"], "title": dl["title"], "company": dl["company_snapshot"], "stage": dl["stage"], "pipeline": "prod",
                             "start": (dl["created_at"] or "")[:10], "due": dl["delivery_on"], "deal_due": dl["delivery_on"], "amount": dl["amount"] or 0,
                             "order_ids": [], "shipped_at": None, "started_at": None})
        today_s = today.isoformat()
        out = []
        for x in rows:
            if not x["due"]:
                x["state"] = "nodue"
            elif x["shipped_at"] or x["stage"] in ("出荷", "据付・試運転", "完了"):
                x["state"] = "shipped"
            elif x["due"] < today_s:
                x["state"] = "late"
            elif x["due"] <= (today + timedelta(days=7)).isoformat():
                x["state"] = "soon"
            else:
                x["state"] = "ok"
            if x["state"] == "nodue" and (x["start"] or "") < (today - timedelta(days=60)).isoformat():
                continue   # 納期のない古い注文書は、納期の画面と現場に出し続けない
            if x["state"] == "nodue" or (x["due"] and f <= x["due"] <= e) or (x["start"] and x["start"] <= e and (x["due"] or "9") >= f):
                if x["state"] == "shipped" and x["due"] and x["due"] < f:
                    continue
                out.append(x)
        out.sort(key=lambda x: (x["due"] or "9999", x["title"] or ""))
        try:
            import zumen_extra
            with _conn() as c:
                zumen_extra.add_milestones(c, t, out, "prod" if has_prod(t) else "sheet")
        except Exception:
            log.exception("工程の予定を出せませんでした")
        return {"rows": out, "from": f, "to": e, "today": today_s}

    # ── 現場 ──
    @r.get("/site")
    def site(session: dict = Depends(check_session)):
        t = session["tenant_id"]
        today = date.today()
        out = []
        with _conn() as c:
            # 納期を60日以上過ぎて「出荷した」が押されていないものは、現場の一覧に出さず件数だけ返す
            sch = schedule(frm=(today - timedelta(days=365)).isoformat(), to=(today + timedelta(days=365)).isoformat(), session=session)["rows"]
            old_limit = (today - timedelta(days=60)).isoformat()
            hidden = [x for x in sch if x["state"] == "late" and x["due"] and x["due"] < old_limit]
            sch = [x for x in sch if x not in hidden]
            for x in sch:
                if x["state"] == "shipped" or not x.get("deal_id") and not x.get("order_ids"):
                    continue
                if x["pipeline"] == "prod" and x["stage"] not in SITE_STAGES:
                    continue
                if x["pipeline"] == "sales" and x["stage"] in ("失注", "保留"):
                    continue
                dids, lines = {}, []
                for oid in x["order_ids"]:
                    for ln in c.execute("SELECT * FROM dz_lines WHERE doc_id=? ORDER BY line_no", (oid,)):
                        lines.append({"drawing_no": ln["drawing_no"], "name": ln["name"], "qty": ln["qty"], "rev": ln["rev"], "drawing_id": ln["drawing_id"]})
                        if ln["drawing_id"]:
                            dids[ln["drawing_id"]] = ln["rev"]
                if x.get("deal_id"):
                    for l2 in c.execute("""SELECT r.drawing_id FROM dz_links l JOIN dz_revisions r ON r.doc_id=l.doc_id
                            WHERE l.tenant_id=? AND l.target_type='deal' AND l.target_id=?""", (t, x["deal_id"])):
                        dids.setdefault(l2["drawing_id"], None)
                drawings = []
                for did, ordered_rev in dids.items():
                    dr = c.execute("""SELECT dr.id, dr.drawing_no, dr.name, dr.material, dr.thickness, r.rev, r.file_id, r.page_no
                        FROM dz_drawings dr LEFT JOIN dz_revisions r ON r.id=dr.current_rev_id WHERE dr.id=?""", (did,)).fetchone()
                    if dr:
                        bad = c.execute("SELECT COUNT(*) n FROM dz_notes WHERE drawing_id=? AND kind='不具合' AND deleted_at IS NULL", (did,)).fetchone()["n"]
                        drawings.append(dict(dr) | {"ordered_rev": ordered_rev, "bad_notes": bad,
                                                     "rev_mismatch": bool(ordered_rev and dr["rev"] and zm.compare_rev(ordered_rev, dr["rev"]) not in (0, None))})
                days = (date.fromisoformat(x["due"]) - today).days if x["due"] else None
                out.append(x | {"lines": lines[:30], "drawings": sorted(drawings, key=lambda y: y["drawing_no"]), "days_left": days})
        return {"items": out, "hidden_old": len(hidden)}

    class SiteIn(BaseModel):
        deal_id: int
        action: str   # start | ship | unship

    @r.post("/site/state")
    def site_state(body: SiteIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            dl = c.execute("SELECT * FROM deals WHERE id=? AND tenant_id=?", (body.deal_id, t)).fetchone()
            if not dl:
                raise HTTPException(404, "案件が見つかりません")
            c.execute("INSERT OR IGNORE INTO dz_deal_state (tenant_id, deal_id) VALUES (?,?)", (t, body.deal_id))
            if body.action == "start":
                c.execute("UPDATE dz_deal_state SET started_at=? WHERE tenant_id=? AND deal_id=?", (_now(), t, body.deal_id))
                if (dl["pipeline"] or "") == "prod" and dl["stage"] in ("部材手配",) and sales_router is not None:
                    sales_router.update_in_tx(c, t, body.deal_id, {"stage": "製作中"})
            elif body.action == "ship":
                c.execute("UPDATE dz_deal_state SET shipped_at=? WHERE tenant_id=? AND deal_id=?", (_now(), t, body.deal_id))
                if (dl["pipeline"] or "") == "prod" and dl["stage"] in SITE_STAGES and sales_router is not None:
                    sales_router.update_in_tx(c, t, body.deal_id, {"stage": "出荷"})
            elif body.action == "unship":
                c.execute("UPDATE dz_deal_state SET shipped_at=NULL WHERE tenant_id=? AND deal_id=?", (t, body.deal_id))
            else:
                raise HTTPException(400, "操作の指定が正しくありません")
            c.commit()
        return {"ok": True}

    # ── 今月の数字 ──
    def group_tenants(c, t: str) -> list:
        """グループ全体の数字に入れる会社（自社と、SALES_GROUP_LINKS でつながる相手のうち、両方が設定で許したもの）"""
        if not _settings(c, t)["group_numbers"]:
            return []
        peers = set()
        for a, b in sales.group_links().items():
            if t in (a, b):
                peers.add(b if a == t else a)
        return [t] + sorted(p for p in peers if _settings(c, p)["group_numbers"])

    def merge_numbers(parts: list) -> dict:
        def add(vals):
            vs = [v for v in vals if v is not None]
            return sum(vs) if vs else None
        base = dict(parts[0][1])
        tiles = []
        for i, t0 in enumerate(base["tiles"]):
            actual = add(p["tiles"][i]["actual"] for _, p in parts)
            target = add(p["tiles"][i]["target"] for _, p in parts)
            forecast = add(p["tiles"][i]["forecast"] for _, p in parts)
            tl = {**t0, "actual": actual, "target": target, "forecast": forecast, "rate": None, "state": "", "remain": None}
            if target:
                fr = (forecast if forecast is not None else actual or 0) / target
                tl.update(rate=round((actual or 0) / target, 3), state="good" if fr >= 1 else "warn" if fr >= 0.9 else "bad",
                          remain=max(0, target - (actual or 0)))
            tiles.append(tl)
        cur = {k: add(p["current"].get(k) for _, p in parts) for k in ("sales", "cost", "cost_actual", "cost_estimated", "gross", "op", "count")}
        cur["gross_rate"] = round(cur["gross"] / cur["sales"], 3) if cur.get("gross") is not None and cur.get("sales") else None
        cov = sum((p["current"].get("covered_ratio") or 0) * (p["current"].get("sales") or 0) for _, p in parts)
        cur["covered_ratio"] = round(cov / cur["sales"], 3) if cur.get("sales") else None
        months = []
        for i, m0 in enumerate(base["months"]):
            rows = [p["months"][i] for _, p in parts]
            mm = {**m0, "sales": add(x.get("sales") for x in rows), "gross": add(x.get("gross") for x in rows), "op": add(x.get("op") for x in rows),
                  "target": add(x.get("target") for x in rows), "before_start": all(x.get("before_start") for x in rows)}
            mm["gross_rate"] = round(mm["gross"] / mm["sales"], 3) if mm["gross"] is not None and mm["sales"] else None
            mm["achieve"] = round(mm["sales"] / mm["target"], 3) if mm["target"] and mm["sales"] is not None else None
            months.append(mm)
        customers = sorted([x | {"tenant": name} for name, p in parts for x in p["customers"]], key=lambda x: -(x.get("sales") or 0))
        fin = {**base["finance"], "fixed_cost": add(p["finance"].get("fixed_cost") for _, p in parts)}
        return {**base, "tiles": tiles, "current": cur, "months": months, "customers": customers, "finance": fin, "scope": "group",
                "companies": [{"tenant": name, "sales": p["current"].get("sales"), "gross": p["current"].get("gross"), "op": p["current"].get("op"),
                               "rate": p["tiles"][0].get("rate")} for name, p in parts],
                "note": "グループ各社の注文書から集計した概算です。グループ内の取引は、二重に数えないよう除いています。"}

    @r.get("/numbers")
    def numbers(month: str = "", basis: str = "", exclude_group: int = 0, scope: str = "", session: dict = Depends(check_session)):
        t = session["tenant_id"]
        today = date.today()
        m = month or today.strftime("%Y-%m")
        if not re.fullmatch(r"\d{4}-\d{2}", m):
            raise HTTPException(400, "月の形式が正しくありません")
        with _conn() as c:
            st = _settings(c, t)
            basis = basis if basis in ("order", "ship") else st["basis"]
            gts = group_tenants(c, t)
            if scope == "group":
                if len(gts) < 2:
                    raise HTTPException(400, "グループ全体の業績は、両方の会社が設定でオンにしたときだけ表示されます")
                return merge_numbers([(own(x), compute_numbers(c, x, m, basis, True, today)) for x in gts])
            out = compute_numbers(c, t, m, basis, bool(exclude_group), today)
            out["group_available"] = len(gts) >= 2
            return out

    def fin_of(c, t: str, m: str) -> dict:
        row = c.execute("SELECT * FROM dz_finance WHERE tenant_id=? AND month<=? ORDER BY month DESC LIMIT 1", (t, m)).fetchone()
        base = dict(row) if row else {}
        tgt = c.execute("SELECT monthly_target FROM sales_settings WHERE tenant_id=?", (t,)).fetchone()
        own_row = c.execute("SELECT * FROM dz_finance WHERE tenant_id=? AND month=?", (t, m)).fetchone()
        return {"sales_target": tgt["monthly_target"] if tgt else None,
                "gross_target": base.get("gross_target"), "gross_rate_target": base.get("gross_rate_target"),
                "op_target": base.get("op_target"), "fixed_cost": base.get("fixed_cost"), "cost_rate": base.get("cost_rate"),
                "inherited_from": base.get("month") if base and base.get("month") != m else None, "set_this_month": bool(own_row)}

    def compute_numbers(c, t: str, m: str, basis: str, exclude_group: bool, today: date) -> dict:
        y, mo = int(m[:4]), int(m[5:7])
        first = date(y, mo, 1)
        last = (date(y + (mo == 12), mo % 12 + 1, 1) - timedelta(days=1))
        fin = fin_of(c, t, m)
        cost_rate_default = fin["cost_rate"]
        orders = order_rows(c, t)
        # 原価（案件ごと）: 仕入先の請求書（受け取り）を優先、なければ発注書（発行）
        cost_by_deal = {}
        for d in c.execute("""SELECT d.id, d.kind, d.direction, d.amount, d.company_id, l.target_id deal_id FROM dz_docs d
                JOIN dz_links l ON l.doc_id=d.id AND l.target_type='deal' WHERE d.tenant_id=? AND d.status='confirmed'
                AND ((d.kind='請求書' AND d.direction='受け取り') OR (d.kind='発注書' AND d.direction='発行'))""", (t,)):
            cb = cost_by_deal.setdefault(d["deal_id"], {})
            k = cb.setdefault(d["company_id"], {"請求書": 0, "発注書": 0})
            k[d["kind"]] += d["amount"] or 0
        deal_cost = {did: sum(v["請求書"] or v["発注書"] for v in cos.values()) for did, cos in cost_by_deal.items()}
        co_rate = {r_["id"]: r_["cost_rate"] for r_ in c.execute("SELECT id, cost_rate FROM dz_companies WHERE tenant_id=?", (t,))}
        lines_of = {}
        for x in c.execute("SELECT doc_id, amount, due_on FROM dz_lines WHERE tenant_id=?", (t,)):
            lines_of.setdefault(x["doc_id"], []).append(x)

        def entries(f: date, to: date) -> list:
            """期間の売上の行（basis に応じて 注文日 / 明細の納期 で数える）"""
            out = []
            for o in orders:
                if exclude_group and o["is_group"]:
                    continue
                if basis == "order":
                    dd = o["doc_date"] or (o["confirmed_at"] or "")[:10]
                    if dd and f.isoformat() <= dd <= to.isoformat():
                        out.append({"order": o, "amount": o["amount"] or 0, "day": dd})
                else:
                    lines = lines_of.get(o["id"], [])
                    if lines and any(x["due_on"] for x in lines) and all(x["amount"] is not None for x in lines):
                        for x in lines:
                            dd = x["due_on"] or o["due_on"]
                            if dd and f.isoformat() <= dd <= to.isoformat():
                                out.append({"order": o, "amount": x["amount"] or 0, "day": dd})
                    else:
                        dd = o["due_on"] or o["doc_date"]
                        if dd and f.isoformat() <= dd <= to.isoformat():
                            out.append({"order": o, "amount": o["amount"] or 0, "day": dd})
            return out

        def summarize(es: list) -> dict:
            sales_amt = sum(e["amount"] for e in es)
            # 原価: 案件の原価実績を、その案件の注文額の比で各行に配る
            deal_total = {}
            for o in orders:
                if o["deal_id"]:
                    deal_total[o["deal_id"]] = deal_total.get(o["deal_id"], 0) + (o["amount"] or 0)
            cost_actual = cost_est = covered = 0
            for e in es:
                o = e["order"]
                did = o["deal_id"]
                if did and did in deal_cost and deal_total.get(did):
                    cost_actual += deal_cost[did] * e["amount"] / deal_total[did]
                    covered += e["amount"]
                else:
                    rate = co_rate.get(o["company_id"]) if co_rate.get(o["company_id"]) is not None else cost_rate_default
                    if rate is not None:
                        cost_est += e["amount"] * rate
            has_cost = covered > 0 or cost_rate_default is not None or any(co_rate.get(e["order"]["company_id"]) is not None for e in es)
            cost = cost_actual + cost_est
            gross = sales_amt - cost if has_cost else None
            op = gross - (fin["fixed_cost"] or 0) if gross is not None and fin["fixed_cost"] is not None else None
            return {"sales": sales_amt, "cost": round(cost) if has_cost else None, "cost_actual": round(cost_actual),
                    "cost_estimated": round(cost_est), "covered_ratio": round(covered / sales_amt, 3) if sales_amt else None,
                    "gross": round(gross) if gross is not None else None,
                    "gross_rate": round(gross / sales_amt, 3) if gross is not None and sales_amt else None,
                    "op": round(op) if op is not None else None, "count": len({id(e["order"]) for e in es})}

        es = entries(first, last)
        cur = summarize(es)
        # 今のペース
        bd_total = zm.business_days(first, last)
        if today < first:
            elapsed = 0
        elif today > last:
            elapsed = bd_total
        else:
            elapsed = zm.business_days(first, today)
        ratio = elapsed / bd_total if bd_total else 1
        if basis == "order":
            done = cur
            forecast_sales = round(cur["sales"] / ratio) if ratio > 0 and today <= last else cur["sales"]
        else:
            done = summarize([e for e in es if e["day"] <= today.isoformat()])
            forecast_sales = cur["sales"]
        def fc(key):
            if cur[key] is None:
                return None
            if basis == "order" and ratio > 0 and today <= last:
                return round(cur[key] / ratio) if key != "op" else None
            return cur[key]
        forecast_gross = fc("gross")
        forecast_op = (forecast_gross - (fin["fixed_cost"] or 0)) if forecast_gross is not None and fin["fixed_cost"] is not None else None

        def tile(label, actual, target, forecast, kind="yen"):
            if target in (None, 0):
                return {"label": label, "actual": actual, "target": None, "forecast": forecast, "rate": None, "state": "", "kind": kind, "remain": None}
            base = forecast if forecast is not None else actual
            rate = (actual or 0) / target if target else None
            fr = (base or 0) / target if target else None
            state = "good" if fr is not None and fr >= 1 else "warn" if fr is not None and fr >= 0.9 else "bad"
            return {"label": label, "actual": actual, "target": target, "forecast": forecast, "rate": round(rate, 3) if rate is not None else None,
                    "state": state, "kind": kind, "remain": max(0, target - (actual or 0)) if kind == "yen" else None}

        gross_target = fin["gross_target"]
        if gross_target is None and fin["gross_rate_target"] is not None and fin["sales_target"]:
            gross_target = round(fin["sales_target"] * fin["gross_rate_target"])
        tiles = [tile("売上", done["sales"], fin["sales_target"], forecast_sales),
                 tile("粗利", done["gross"], gross_target, forecast_gross),
                 tile("営業利益", done["op"], fin["op_target"], forecast_op)]
        # 直近12か月（注文書を入れ始める前の月は「使い始める前」として数字を出さない）
        starts = [o["doc_date"] for o in orders if o["doc_date"]]
        start_month = min(starts)[:7] if starts else m
        months = []
        for i in range(11, -1, -1):
            yy, mm = y, mo - i
            while mm <= 0:
                yy, mm = yy - 1, mm + 12
            f = date(yy, mm, 1)
            to = date(yy + (mm == 12), mm % 12 + 1, 1) - timedelta(days=1)
            s = summarize(entries(f, to))
            ff = fin_of(c, t, f"{yy}-{mm:02d}")
            before = f"{yy}-{mm:02d}" < start_month
            months.append({"month": f"{yy}-{mm:02d}", **s, "target": None if before else ff["sales_target"], "before_start": before,
                           "achieve": None if before or not ff["sales_target"] else round(s["sales"] / ff["sales_target"], 3)})
            if before:
                months[-1]["op"] = None
        # 客先別（今月）
        by_co = {}
        for e in es:
            o = e["order"]
            b = by_co.setdefault(o["company_id"], {"company_id": o["company_id"], "company": o["company"] or "（会社未設定）", "is_group": bool(o["is_group"]), "items": []})
            b["items"].append(e)
        customers = []
        for b in by_co.values():
            s = summarize(b.pop("items"))
            customers.append(b | s)
        customers.sort(key=lambda x: -x["sales"])
        return {"month": m, "basis": basis, "exclude_group": exclude_group, "tiles": tiles, "current": cur, "done": done,
                "pace": {"business_days": bd_total, "elapsed": elapsed, "ratio": round(ratio, 3)}, "finance": fin,
                "months": months, "customers": customers,
                "note": "注文書から集計した概算です。会計上の数字とは一致しません。"}

    class FinanceIn(BaseModel):
        month: str
        gross_target: Optional[int] = None
        gross_rate_target: Optional[float] = None
        op_target: Optional[int] = None
        fixed_cost: Optional[int] = None
        cost_rate: Optional[float] = None
        sales_target: Optional[int] = None

    @r.get("/finance")
    def get_finance(month: str = "", session: dict = Depends(check_session)):
        m = month or date.today().strftime("%Y-%m")
        with _conn() as c:
            return fin_of(c, session["tenant_id"], m) | {"month": m}

    @r.put("/finance")
    def put_finance(body: FinanceIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        if not re.fullmatch(r"\d{4}-\d{2}", body.month):
            raise HTTPException(400, "月の形式が正しくありません")
        for v, label in ((body.gross_rate_target, "粗利率の目標"), (body.cost_rate, "見込みの原価率")):
            if v is not None and not (0 <= v <= 1.5):
                raise HTTPException(400, f"{label}は0〜150%の範囲で入力してください")
        for v, label in ((body.gross_target, "粗利の目標"), (body.op_target, "営業利益の目標"), (body.fixed_cost, "月の固定費"), (body.sales_target, "売上目標")):
            if v is not None and abs(v) > 100_000_000_000:
                raise HTTPException(400, f"{label}が大きすぎます")
        with _conn() as c:
            c.execute("""INSERT INTO dz_finance (tenant_id, month, gross_target, gross_rate_target, op_target, fixed_cost, cost_rate, updated_at)
                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(tenant_id, month) DO UPDATE SET gross_target=excluded.gross_target,
                gross_rate_target=excluded.gross_rate_target, op_target=excluded.op_target, fixed_cost=excluded.fixed_cost,
                cost_rate=excluded.cost_rate, updated_at=excluded.updated_at""",
                      (t, body.month, body.gross_target, body.gross_rate_target, body.op_target, body.fixed_cost, body.cost_rate, _now()))
            if body.sales_target is not None:
                c.execute("INSERT OR IGNORE INTO sales_settings (tenant_id, updated_at) VALUES (?,?)", (t, _now()))
                c.execute("UPDATE sales_settings SET monthly_target=?, updated_at=? WHERE tenant_id=?", (body.sales_target or None, _now(), t))
            c.commit()
        return {"ok": True}

    # ── 設定・状態 ──
    class SettingsIn(BaseModel):
        extra_kinds: Optional[list] = None
        group_numbers: Optional[bool] = None
        bulk_confirm: Optional[bool] = None
        bulk_threshold: Optional[float] = None
        slack_notify: Optional[bool] = None
        basis: Optional[str] = None

    @r.get("/settings")
    def get_settings(session: dict = Depends(check_session)):
        with _conn() as c:
            return _settings(c, session["tenant_id"])

    @r.put("/settings")
    def put_settings(body: SettingsIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            c.execute("INSERT OR IGNORE INTO dz_settings (tenant_id) VALUES (?)", (t,))
            sets = {}
            if body.bulk_confirm is not None:
                sets["bulk_confirm"] = 1 if body.bulk_confirm else 0
            if body.bulk_threshold is not None:
                if not (0.5 <= body.bulk_threshold <= 1):
                    raise HTTPException(400, "基準は50〜100%の範囲で入力してください")
                sets["bulk_threshold"] = body.bulk_threshold
            if body.slack_notify is not None:
                sets["slack_notify"] = 1 if body.slack_notify else 0
            if body.group_numbers is not None:
                sets["group_numbers"] = 1 if body.group_numbers else 0
            if body.extra_kinds is not None:
                ks = []
                for k in body.extra_kinds[:12]:
                    k = re.sub(r"\s+", "", str(k))[:10]
                    if k and k not in za.KINDS and k not in ks:
                        ks.append(k)
                sets["extra_kinds"] = _j(ks)
            if body.basis is not None:
                if body.basis not in ("order", "ship"):
                    raise HTTPException(400, "集計方法の指定が正しくありません")
                sets["basis"] = body.basis
            if sets:
                c.execute(f"UPDATE dz_settings SET {', '.join(k + '=?' for k in sets)}, updated_at=? WHERE tenant_id=?", (*sets.values(), _now(), t))
            c.commit()
            return _settings(c, t)

    @r.get("/admin/status")
    def status(session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            files = c.execute("SELECT COUNT(*) n, COALESCE(SUM(size),0) s FROM dz_files WHERE tenant_id=?", (t,)).fetchone()
            q = {r_["status"]: r_["n"] for r_ in c.execute("SELECT status, COUNT(*) n FROM dz_ingest WHERE tenant_id=? GROUP BY status", (t,))}
            docs = {r_["status"]: r_["n"] for r_ in c.execute("SELECT status, COUNT(*) n FROM dz_docs WHERE tenant_id=? GROUP BY status", (t,))}
            stuck = c.execute("SELECT COUNT(*) n FROM dz_ingest WHERE status='reading' AND updated_at < ?",
                              ((datetime.now() - timedelta(minutes=30)).isoformat(),)).fetchone()["n"]
            cand = c.execute("""SELECT field, COUNT(*) n, SUM(was_top) top FROM dz_candidates WHERE tenant_id=? GROUP BY field""", (t,)).fetchall()
        backup = None
        try:
            with open(STATUS_FILE, encoding="utf-8") as fh:
                backup = json.load(fh)
        except (OSError, ValueError):
            pass
        return {"disk": storage.disk_usage(), "files": {"count": files["n"], "bytes": files["s"]}, "queue": q, "stuck": stuck, "docs": docs,
                "storage": os.getenv("DZ_STORAGE", "local"), "r2_configured": bool(os.getenv("DZ_R2_BUCKET")),
                "backup": backup, "ai_available": za.ai_available(), "search_mode": FTS["mode"],
                "accuracy": [{"field": x["field"], "count": x["n"], "top": x["top"] or 0} for x in cand]}

    @r.post("/admin/retry")
    def retry(session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            n = c.execute("""UPDATE dz_ingest SET status='queued', tries=0, updated_at=? WHERE tenant_id=? AND (status='failed'
                OR (status='reading' AND updated_at < ?))""", (_now(), t, (datetime.now() - timedelta(minutes=30)).isoformat())).rowcount
            c.commit()
        kick()
        return {"ok": True, "requeued": n}

    @r.get("/meta")
    def meta(session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with _conn() as c:
            members = [x["name"] for x in c.execute("SELECT name FROM members WHERE tenant_id=? ORDER BY sort_order, id", (t,))]
            n_review = c.execute("SELECT COUNT(*) n FROM dz_docs WHERE tenant_id=? AND status='review'", (t,)).fetchone()["n"]
        with _conn() as c:
            kinds = kinds_for(c, t)
        return {"kinds": kinds, "note_kinds": list(NOTE_KINDS), "members": members, "own": own(t), "has_prod": has_prod(t),
                "review_count": n_review, "ai_available": za.ai_available(), "undo_hours": UNDO_HOURS, "trash_days": TRASH_DAYS,
                "auto_pick": zm.AUTO_PICK}

    r.confirm_in_tx = confirm_in_tx
    r.schedule_rows = schedule
    r.card_companies = card_companies
    r.group_names = group_names
    r.compute_numbers = compute_numbers
    return r


def _settings(c, t: str) -> dict:
    row = c.execute("SELECT * FROM dz_settings WHERE tenant_id=?", (t,)).fetchone()
    if not row:
        return {"bulk_confirm": False, "bulk_threshold": 0.9, "slack_notify": False, "basis": "order", "extra_kinds": [], "group_numbers": False}
    return {"bulk_confirm": bool(row["bulk_confirm"]), "bulk_threshold": row["bulk_threshold"],
            "slack_notify": bool(row["slack_notify"]), "basis": row["basis"],
            "extra_kinds": [k for k in (_l(row["extra_kinds"], []) or []) if isinstance(k, str)],
            "group_numbers": bool(row["group_numbers"])}


def kinds_for(c, t: str) -> list:
    """書類の種類。決まった種類の「その他」の前に、会社ごとに足した種類を入れる"""
    extra = [k for k in _settings(c, t)["extra_kinds"] if k not in za.KINDS]
    return za.KINDS[:-1] + extra + ["その他"]
