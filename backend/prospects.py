"""
テレアポリストの共有
  ・リスト（Excel / CSV）を こちら側でも テレアポ業者側でもアップロードできる。見出しのないリストも列を中身から見分ける
  ・会社ごとに 判定（かけてよい／かけない／保留＋理由）を付ける。業者には判定と理由が見える
  ・AIがウェブ検索で公式サイト・事業・大物の有無・板金設備の有無を調べ、マッチ度（点数とA/B/C）と電話の切り口を出す
  ・電話の記録はリストの行（会社）に残す。同じ会社が別のリストに出てきても記録は会社に付いて引き継がれる
  ・「アポ獲得」で自動的に案件になる。リストでの電話の履歴とAIの調査は案件に移る
  ・訪問後に案件へ付けた評価（S/A/B/C/F）はリストに戻り、AIの調査の手本にもなる
"""
import csv
import io
import json
import logging
import os
import re
import sqlite3
import threading
import time
import unicodedata
import zipfile
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

import intake
import sales
import vendorreq

log = logging.getLogger("prospects")

VERDICTS = {"ok": "かけてよい", "ng": "かけない", "hold": "保留"}
GRADES = ["S", "A", "B", "C", "F"]
RANKS = ["A", "B", "C", "除外"]
SIZES = ["大", "中", "小", "不明"]
INHOUSE = ["有", "無とみられる", "不明"]
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_ROWS = 3000
MAX_LISTS = 300
MAX_VENDOR_LISTS = 100
MAX_UNZIPPED = 60 * 1024 * 1024
MAX_COL = 200
MAX_SHEETS = 10
MAX_SHEET_BYTES = 15 * 1024 * 1024
VENDOR_CALLS_PER_COMPANY = 30


def _daily_cap_default() -> int:
    return int(os.getenv("SALES_RESEARCH_DAILY", "150"))   # .env を読んだ後に見る


def _searches() -> int:
    return int(os.getenv("SALES_RESEARCH_SEARCHES", "4"))
REACHED_NO = {"担当者不在"}

# テストでは差し替える（tenant_id, prospect(dict), context(dict)) -> dict
RESEARCH_FN: Optional[Callable] = None


# ── 狙う会社の条件（会社ごとの初期値。画面で書き換えられる） ──
DEFAULT_ICP = {
    "sheet": """自社: 大型筐体（制御盤・配電盤・キュービクルの箱、大型の架台、装置の外装・カバー）の板金加工を受注生産でしている。
狙う会社（高）:
- 大型の盤や電気機器・装置を作るが、自社に板金・製缶・塗装の設備を持たない会社（設備一覧に板金機がない、協力工場・協力会社を使うと明記している）
- 例: 配電盤・制御盤・キュービクルのメーカーで板金を外注している会社、変圧器・リアクトル・電源装置のメーカー、大型の試験装置・炉・乾燥機・搬送設備のメーカー
- 従業員30〜200名くらい、大阪府内とその近く
溢れ狙い（中）: 大物を作り板金も内製しているが、忙しいときに外へ出しそうな会社
合わない（低・除外）:
- 板金・塗装まで自社でやる会社、同業の板金屋
- 小型の電子機器・計測器だけの会社（箱が小さい）
- 商社・卸、電気工事や設備工事の会社
- 盤を箱ごと盤屋に一式で買っている機械メーカー（箱を決めるのは盤屋）
- 価格だけで海外材を求める会社""",
    "panel": """自社: 制御盤・配電盤・分電盤・キュービクルの設計製作（受注生産）。
狙う会社（高）:
- 工場を持つ製造業・装置メーカーで、盤を自社で作らず外から買っている会社
- 電気工事・設備工事・プラントエンジニアリングの会社（盤を発注する側）
- ビル・工場の設備更新や保全をする会社
- 大阪府内とその近く
合わない（低・除外）:
- 盤メーカー（競合）、商社・卸
- 小型の電子機器だけの会社""",
}

# AIを使う前の目安（業種の言葉だけで付ける点数）
RULES = {
    "sheet": {
        "hi": ["配電盤", "制御盤", "電力制御", "変圧器", "リアクトル", "電源", "キュービクル", "受配電", "発電", "回転電気",
               "産業用電気機械", "試験機", "窯炉", "炉", "乾燥", "搬送", "物流運搬", "化学機械", "環境装置"],
        "lo": ["卸", "商社", "販売", "工事", "サービス", "設計業", "基板", "電子部品", "計量", "計器", "測定", "プレス", "板金"],
    },
    "panel": {
        "hi": ["製造", "機械", "装置", "設備", "プラント", "工事", "エンジニアリング", "工場"],
        "lo": ["配電盤", "制御盤", "電力制御", "卸", "商社", "販売", "基板", "電子部品"],
    },
}


def _prof(tenant_id: str) -> str:
    return sales.profile_key(sales.display_of(tenant_id))


def rule_score(prof: str, industry: Optional[str], company: Optional[str] = None) -> int:
    rules = RULES.get(prof) or RULES["sheet"]
    s = 40
    text = f"{industry or ''} {company or ''}"
    if any(w in text for w in rules["hi"]):
        s += 25
    if any(w in (industry or "") for w in rules["lo"]):
        s -= 20
    return max(5, min(70, s))


# ── DB ──
def init_db():
    with sales._conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS prospect_lists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            name TEXT NOT NULL,
            vendor_id INTEGER,                 -- 共有している業者（NULL は自社だけ）
            uploaded_by_vendor INTEGER,        -- 業者がアップロードしたときの業者
            filename TEXT,
            note TEXT,
            archived INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS prospects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            norm TEXT NOT NULL,
            company TEXT NOT NULL,
            tel TEXT, zip TEXT, address TEXT, pref TEXT, industry TEXT, web TEXT, contact TEXT,
            verdict TEXT, verdict_reason TEXT, verdict_at TEXT,
            ai_status TEXT, ai_json TEXT, ai_at TEXT, ai_error TEXT, ai_requested_at TEXT,
            score INTEGER, rank TEXT, official_url TEXT,
            existing TEXT,
            deal_id INTEGER,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE (tenant_id, norm)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS prospect_list_rows (
            list_id INTEGER NOT NULL,
            prospect_id INTEGER NOT NULL,
            row_no INTEGER,
            memo TEXT,                          -- リストに書いてあった備考
            PRIMARY KEY (list_id, prospect_id)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS prospect_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            prospect_id INTEGER NOT NULL,
            list_id INTEGER,
            vendor_id INTEGER,                  -- 業者の記録（NULL は自社）
            source TEXT NOT NULL DEFAULT 'call', -- call | import（リストの備考から取り込み）
            called_at TEXT,
            result TEXT,
            contact TEXT, department TEXT,
            appo_at TEXT, next_call_on TEXT,
            memo TEXT,
            created_at TEXT NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS prospect_settings (
            tenant_id TEXT PRIMARY KEY,
            icp TEXT,
            daily_cap INTEGER
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS prospect_research_log (
            tenant_id TEXT NOT NULL,
            prospect_id INTEGER NOT NULL,
            requested_at TEXT NOT NULL
        )""")
        for col, ddl in (("tel", "tel TEXT"), ("address", "address TEXT"), ("industry", "industry TEXT"), ("web", "web TEXT"),
                         ("linked_at", "linked_at TEXT"), ("gated", "gated INTEGER NOT NULL DEFAULT 0"),
                         ("gated_orig", "gated_orig INTEGER NOT NULL DEFAULT 0")):
            sales._add_column(c, "prospect_list_rows", col, ddl)   # 行に書いてあった値 / 業者に見せる範囲
        sales._add_column(c, "prospects", "verdict_reason_at", "verdict_reason_at TEXT")
        c.execute("CREATE INDEX IF NOT EXISTS idx_research_log ON prospect_research_log(tenant_id, requested_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_prospects_tenant ON prospects(tenant_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_prospects_ai ON prospects(ai_status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_plrows_p ON prospect_list_rows(prospect_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pcalls_p ON prospect_calls(prospect_id)")
        sales._add_column(c, "deals", "visit_grade", "visit_grade TEXT")       # 訪問の評価 S/A/B/C/F
        sales._add_column(c, "deals", "prospect_id", "prospect_id INTEGER")    # リストから来た案件
        sales._add_column(c, "prospects", "ai_started_at", "ai_started_at TEXT")
        sales._add_column(c, "prospects", "done_at", "done_at TEXT")   # 対応が終わった会社（リストの赤い文字）
        c.commit()
        if os.getenv("SALES_DB_WAL") == "1":   # 使うときだけ（ファイルの持ち主が違う環境では切り替えない）
            try:
                c.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                pass


def norm_company(name: str) -> str:
    n = unicodedata.normalize("NFKC", name or "")
    n = re.sub(r"(合資会社|合名会社|一般社団法人|一般財団法人|公益社団法人|公益財団法人|医療法人|社会福祉法人|\(資\)|\(名\)|㈾|㈴)", "", n)
    n = sales._norm_company(n)
    return re.sub(r"[‐－―\-−–—]", "", n)


PREF_RE = re.compile(r"(北海道|東京都|京都府|大阪府|.{2,3}県)")


def pref_of(address: Optional[str]) -> Optional[str]:
    m = PREF_RE.search(unicodedata.normalize("NFKC", address or ""))
    return m.group(1) if m else None


# ── 表の読み込み ──
# ── セルの色（リストの塗り分け・文字色を読む） ──
# 業者のリストは「行の塗り＝状況」「文字が赤＝完了」で管理していることが多い
_IDX = {0: "000000", 1: "FFFFFF", 2: "FF0000", 3: "00FF00", 4: "0000FF", 5: "FFFF00", 6: "FF00FF", 7: "00FFFF",
        8: "000000", 9: "FFFFFF", 10: "FF0000", 11: "00FF00", 12: "0000FF", 13: "FFFF00", 14: "FF00FF", 15: "00FFFF",
        16: "800000", 17: "008000", 18: "000080", 19: "808000", 20: "800080", 21: "008080", 22: "C0C0C0", 23: "808080",
        40: "00CCFF", 41: "CCFFFF", 42: "CCFFCC", 43: "FFFF99", 44: "99CCFF", 45: "FF99CC", 46: "CC99FF", 47: "FFCC99",
        48: "3366FF", 49: "33CCCC", 50: "99CC00", 51: "FFCC00", 52: "FF9900", 53: "FF6600"}
COLOR_KINDS = {"red": "赤", "yellow": "黄", "blue": "青", "green": "緑", "orange": "橙", "purple": "紫"}
# リストの色の意味（テレアポ業者と決めた塗り分け）。文字が赤い行は「完了」
COLOR_MEANING = {"red": "ng", "yellow": "appo", "blue": "follow", "green": "hold"}
STATUS_LABEL = {"new": "未着手", "calling": "架電中", "follow": "資料送付・再架電", "appo": "アポ", "hold": "保留", "ng": "かけない"}


def _theme_colors(z) -> list:
    """テーマの色（lt1, dk1, lt2, dk2, accent1..6 の順。セルの theme 番号と同じ並び）"""
    root = intake._xml(z, "xl/theme/theme1.xml")
    if root is None:
        return []
    A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    sch = root.find(f".//{A}clrScheme")
    if sch is None:
        return []
    vals = {}
    for el in sch:
        tag = el.tag.replace(A, "")
        c = el.find(f"{A}srgbClr")
        s_ = el.find(f"{A}sysClr")
        vals[tag] = (c.get("val") if c is not None else (s_.get("lastClr") if s_ is not None else None))
    return [vals.get(k) for k in ("lt1", "dk1", "lt2", "dk2", "accent1", "accent2", "accent3", "accent4", "accent5", "accent6")]


def _color_of(el, theme: list) -> Optional[str]:
    if el is None:
        return None
    if el.get("rgb"):
        v = el.get("rgb")[-6:]
        return v.upper() if re.match(r"^[0-9A-Fa-f]{6}$", v) else None
    if el.get("indexed") is not None:
        try:
            return _IDX.get(int(el.get("indexed")))
        except ValueError:
            return None
    if el.get("theme") is not None:
        try:
            i = int(el.get("theme"))
        except ValueError:
            return None
        if 0 <= i < len(theme) and theme[i] and not el.get("tint"):
            return theme[i].upper()   # 明るさを変えた色（tint）は淡い飾りのことが多いので読まない
    return None


def _style_table(z) -> list:
    """セルのスタイル番号 → (塗りの色, 文字の色)"""
    S = intake.S
    st = intake._xml(z, "xl/styles.xml")
    if st is None:
        return []
    theme = _theme_colors(z)
    fonts, fills = [], []
    fe = st.find(S + "fonts")
    for f in (fe.findall(S + "font") if fe is not None else []):
        fonts.append(_color_of(f.find(S + "color"), theme))
    fl = st.find(S + "fills")
    for f in (fl.findall(S + "fill") if fl is not None else []):
        pf = f.find(S + "patternFill")
        if pf is not None and (pf.get("patternType") or "") not in ("", "none", "gray125"):
            fills.append(_color_of(pf.find(S + "fgColor"), theme))
        else:
            fills.append(None)
    out = []
    xfs = st.find(S + "cellXfs")
    for xf in (xfs.findall(S + "xf") if xfs is not None else []):
        try:
            fi, fo = int(xf.get("fillId") or 0), int(xf.get("fontId") or 0)
        except ValueError:
            fi = fo = 0
        out.append((fills[fi] if fi < len(fills) else None, fonts[fo] if fo < len(fonts) else None))
    return out


def color_kind(rgb: Optional[str]) -> Optional[str]:
    """色を大まかな種類に（白・黒・灰色・淡い色は None）"""
    if not rgb or len(rgb) != 6:
        return None
    try:
        r, g, b = (int(rgb[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return None
    mx, mn = max(r, g, b), min(r, g, b)
    if mx < 0.35 or mx - mn < 0.25 or (mx - mn) / mx < 0.35:
        return None
    if mx == r:
        h = (60 * ((g - b) / (mx - mn))) % 360
    elif mx == g:
        h = 60 * ((b - r) / (mx - mn)) + 120
    else:
        h = 60 * ((r - g) / (mx - mn)) + 240
    if h < 18 or h >= 330:
        return "red"
    if h < 42:
        return "orange"
    if h < 70:
        return "yellow"
    if h < 165:
        return "green"
    if h < 260:
        return "blue"
    return "purple"


def _xlsx_sheets(raw: bytes) -> list:
    """[(シート名, 行のリスト, {(行, 列): リンク}, 行ごとの色 [{列: (塗り, 文字)}])]"""
    S, R, PR = intake.S, intake.R, intake.PR
    z = zipfile.ZipFile(io.BytesIO(raw))
    if sum(i.file_size for i in z.infolist()) > MAX_UNZIPPED:
        raise HTTPException(400, "ファイルの中身が大きすぎます。不要なシートや列を消してから取り込んでください")
    shared = []
    sst = intake._xml(z, "xl/sharedStrings.xml")
    if sst is not None:
        for si in sst.findall(S + "si"):
            shared.append(intake._rich_text(si))
    wb = intake._xml(z, "xl/workbook.xml")
    rels = intake._xml(z, "xl/_rels/workbook.xml.rels")
    target = {}
    if rels is not None:
        for rel in rels.findall(PR + "Relationship"):
            target[rel.get("Id")] = rel.get("Target")
    sheets = []
    if wb is not None:
        for sh in wb.iter(S + "sheet"):
            tgt = (target.get(sh.get(R + "id"), "") or "").lstrip("/")
            path = tgt if tgt.startswith("xl/") else "xl/" + tgt
            if path not in {p for _, p in sheets}:
                sheets.append((sh.get("name"), path))
    if not sheets:
        sheets = [(n.split("/")[-1], n) for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n)]
    out, total = [], 0
    sizes = {i.filename: i.file_size for i in z.infolist()}
    try:
        xf = _style_table(z)
    except Exception:
        xf = []   # 色が読めなくても中身は取り込む
    for name, path in sheets[:MAX_SHEETS]:
        if sizes.get(path, 0) > MAX_SHEET_BYTES:
            raise HTTPException(400, f"シート「{name}」が大きすぎます。会社の一覧だけを残して取り込んでください")
        root = intake._xml(z, path)
        if root is None:
            continue
        # セルのリンク（URL欄に会社名だけ書いてあってもリンク先を拾う）
        links = {}
        rel_path = re.sub(r"([^/]+)$", r"_rels/\1.rels", path)
        srels = intake._xml(z, rel_path)
        rid_url = {}
        if srels is not None:
            for rel in srels.findall(PR + "Relationship"):
                if (rel.get("TargetMode") or "") == "External":
                    rid_url[rel.get("Id")] = rel.get("Target")
        rows, colors = [], []
        index_of = {}
        for row in root.iter(S + "row"):
            vals, cols = {}, {}
            pos = -1
            for c in row.findall(S + "c"):
                pos += 1
                t = c.get("t")
                v = c.find(S + "v")
                if t == "s" and v is not None:
                    try:
                        val = shared[int(v.text)]
                    except (ValueError, IndexError):
                        val = ""
                elif t == "inlineStr":
                    val = intake._rich_text(c.find(S + "is")) if c.find(S + "is") is not None else ""
                else:
                    val = v.text if v is not None and v.text else ""
                ci = intake._col_index(c.get("r")) if c.get("r") else pos   # 位置の書いていないセルは並び順で
                pos = ci
                if val.strip() and ci < MAX_COL:
                    vals[ci] = val.strip()
                if xf and c.get("s") and ci < 12:
                    try:
                        sty = xf[int(c.get("s"))]
                    except (ValueError, IndexError):
                        sty = None
                    if sty and (sty[0] or sty[1]):
                        cols[ci] = sty
            if not vals:
                continue
            try:
                rnum = int(row.get("r") or 0)
            except ValueError:
                rnum = 0
            index_of[rnum] = len(rows)
            width = max(vals) + 1
            rows.append([vals.get(i, "") for i in range(width)])
            colors.append(cols)
            total += 1
            if total > MAX_ROWS + 50:
                break
        for hl in root.iter(S + "hyperlink"):
            url = rid_url.get(hl.get(R + "id"))
            ref = hl.get("ref") or ""
            m = re.match(r"([A-Z]+)(\d+)", ref)
            if url and m and int(m.group(2)) in index_of:
                links[(index_of[int(m.group(2))], intake._col_index(m.group(1) + "1"))] = url
        out.append((name, rows, links, colors))
    return out


def read_table(filename: str, raw: bytes) -> list:
    ext = os.path.splitext((filename or "").lower())[1]
    if ext in (".xlsx", ".xlsm"):
        try:
            return _xlsx_sheets(raw)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(400, "Excelファイルを読めませんでした（.xlsx で保存し直してください）")
    if ext in (".csv", ".tsv", ".txt"):
        text = intake.decode_text(raw)
        delim = "\t" if ext == ".tsv" or text.count("\t") > text.count(",") else ","
        try:
            rows = []
            for r in csv.reader(io.StringIO(text), delimiter=delim):
                if any(r):
                    rows.append([x.strip() for x in r[:MAX_COL]])
                if len(rows) > MAX_ROWS + 50:
                    break
        except csv.Error:
            raise HTTPException(400, "CSVを読めませんでした（区切りや引用符を確かめてください）")
        return [("CSV", rows, {}, [])]
    raise HTTPException(400, "Excel（.xlsx）か CSV を選んでください（.xls は Excel で .xlsx に保存し直してください）")


HEAD = {
    "company": re.compile(r"会社名|企業名|社名|法人名|取引先名?|^会社$"),
    "memo": re.compile(r"備考|メモ|コメント|結果|状況|対応|経緯"),
    "tel": re.compile(r"電話番号|^電話$|^tel|ＴＥＬ|^ＴＥＬ|代表電話|^電話\s*$", re.I),
    "zip": re.compile(r"郵便|〒"),
    "address": re.compile(r"住所|所在地"),
    "industry": re.compile(r"業種|業界|業態|事業内容"),
    "web": re.compile(r"web|url|ＵＲＬ|hp|ホームページ|サイト|ウェブ", re.I),
    "contact": re.compile(r"担当|部署"),
}
HEAD_ONLY = re.compile(r"^(会社名|企業名|社名|法人名|取引先名?|会社|No\.?|番号)[ァ-ヶー]*$")
TITLE_ROW = re.compile(r"^第\d+弾|リスト(確認)?$|確認済み|よろしくお願い|^[↓↑※＊*]|^下記")
CONTENT = {
    "web": re.compile(r"^(https?://|www\.)|\.(co\.jp|or\.jp|ne\.jp|jp|com|net)(/|$)", re.I),
    "tel": re.compile(r"^\(?0\d{1,4}[-‐ー−)\s]?\d{1,4}[-‐ー−\s]?\d{3,4}$"),
    "zip": re.compile(r"^〒?\s?\d{3}-?\d{4}$"),
    "address": re.compile(r"(都|道|府|県).*(市|区|町|村|郡)"),
    "company": re.compile(r"株式会社|有限会社|合同会社|\(株\)|\(有\)|（株）|（有）|㈱|㈲"),
    "industry": re.compile(r"(業|製造|卸売|メーカー)$"),
}


def detect_columns(rows: list) -> tuple:
    """(見出しの行の位置 or -1, {項目: 列})"""
    head_idx, cols = -1, {}
    for i, r in enumerate(rows[:12]):
        hit = {}
        for j, cell in enumerate(r):
            t = unicodedata.normalize("NFKC", cell or "").strip()
            if not t or len(t) > 20:
                continue
            for k, rx in HEAD.items():
                if k not in hit and rx.search(t):
                    hit[k] = j
                    break
        if len(hit) >= 2 and "company" in hit:
            head_idx, cols = i, hit
            break
    data = rows[head_idx + 1:] if head_idx >= 0 else rows
    sample = data[:300]
    width = max((len(r) for r in sample), default=0)
    used = set(cols.values())
    order = ["web", "tel", "zip", "address", "company", "industry"]
    for k in order:
        if k in cols:
            continue
        best, best_j = 0.0, None
        for j in range(width):
            if j in used:
                continue
            vals = [unicodedata.normalize("NFKC", r[j]).strip() for r in sample if j < len(r) and (r[j] or "").strip()]
            if len(vals) < max(2, len(sample) * 0.2):
                continue
            share = sum(1 for v in vals if CONTENT[k].search(v) and (k != "industry" or len(v) <= 30)) / len(vals)
            if share > best:
                best, best_j = share, j
        if best_j is not None and best >= (0.3 if k == "company" else 0.45):
            cols[k] = best_j
            used.add(best_j)
    if "memo" not in cols and width:
        # 長めの文で日付っぽいものが入っている列
        best, best_j = 0.0, None
        for j in range(width):
            if j in used:
                continue
            vals = [r[j] for r in sample if j < len(r) and (r[j] or "").strip()]
            if not vals:
                continue
            share = sum(1 for v in vals if len(v) >= 6 and re.search(r"\d{1,2}/\d{1,2}|不在|資料|アポ|必要|席外", v)) / max(len(vals), 1)
            if share > best and len(vals) >= 2:
                best, best_j = share, j
        if best_j is not None and best >= 0.4:
            cols["memo"] = best_j
    return head_idx, cols


def guess_result(memo: str) -> Optional[str]:
    m = memo or ""
    neg = re.search(r"アポ(イント)?.{0,6}(NG|ＮＧ|不可|難し|無理|断|取れ|とれ|なし|無し|ダメ|だめ|未|打診|取りたい|以降|検討|の電話|予定|待ち)"
                    r"|アポ取りもあり|アポ(イント)?(の)?(電話|依頼)", m)
    if not neg and re.search(r"\d{1,2}[/月]\d{1,2}.{0,24}アポ(イント)?(\s|　|$|。|、|\(|（)|アポ(イント)?(獲得|取得|確定|OK|ＯＫ)"
                             r"|アポ(イント)?$|様\s*アポ|さん\s*アポ", m):
        return "アポ獲得"
    if re.search(r"資料|会社案内|メールで", m):
        return "資料送付"
    if re.search(r"間に合|決まった|協力工場|必要な|新規.{0,6}(お断り|NG|受け|受付)|NG|結構|お断り|断られ", m):
        return "見込みなし"
    if re.search(r"席外|不在|外出|会議|打ち合わせ|出張|休み|わかるもの", m):
        return "担当者不在"
    if m.strip():
        return "その他"
    return None


def _excel_date(v: str) -> str:
    """見出しが日付の列に入っている Excel の日付（数字）を 2025/2/19 の形に"""
    try:
        n = float(v)
    except ValueError:
        return v
    if 20000 < n < 80000:
        d = date(1899, 12, 30) + timedelta(days=int(n))
        return f"{d.year}/{d.month}/{d.day}"
    return v


def _row_color(crow: dict, company_col: int) -> tuple:
    """(塗りの種類, 文字が赤＝完了か)。会社名のセル → 左端のセルの順に見る"""
    kind = None
    for j in (company_col, 0, 1):
        st = crow.get(j)
        if st and color_kind(st[0]):
            kind = color_kind(st[0])
            break
    st = crow.get(company_col)
    return kind, bool(st and color_kind(st[1]) == "red")


def parse_rows(sheets: list) -> tuple:
    """会社ごとの行と、見分けた列の説明。
    同じ会社が複数のシートにあれば1社にまとめる（備考はつなげる・空いている項目は埋める）。
    色（塗り・赤い文字）は「備考と電話の列がある結果のシート」のものを優先する（確認用に塗っただけのシートの色で上書きしない）"""
    out, by_norm, desc = [], {}, []
    for sh in sheets:
        name, rows, links = sh[0], sh[1], sh[2]
        colors = sh[3] if len(sh) > 3 else []
        if not rows:
            continue
        head_idx, cols = detect_columns(rows)
        if "company" not in cols:
            desc.append({"sheet": name, "ok": False, "columns": {}})
            continue
        level = 2 if ("memo" in cols and "tel" in cols) else 1   # 結果を書き込んでいるシートか
        heads = rows[head_idx] if head_idx >= 0 else []
        used = set(cols.values())
        width = max((len(r) for r in rows[head_idx + 1:head_idx + 301]), default=0)
        extra = []   # ほかの列（訪問日・分類・担当・2つ目のメモなど）も備考に入れる
        for j in range(width):
            if j in used:
                continue
            label = unicodedata.normalize("NFKC", heads[j]).strip() if j < len(heads) else ""
            if re.match(r"^(no\.?|番号|#)$", label, re.I):
                continue
            vals = [unicodedata.normalize("NFKC", r[j]).strip() for r in rows[head_idx + 1:head_idx + 301] if j < len(r) and (r[j] or "").strip()]
            if not vals:
                continue
            if not label and sum(1 for v in vals if re.fullmatch(r"[\d.\-/〒]+", v)) > len(vals) * 0.6:
                continue   # 見出しのない数字だけの列（通し番号など）
            extra.append((j, label))
        n_color = n_done = 0
        desc.append({"sheet": name, "ok": True, "columns": {k: v + 1 for k, v in cols.items()}, "header": head_idx >= 0,
                     "result": level == 2})
        has_contact_cols = any(k in cols for k in ("tel", "address", "web"))
        for i in range(head_idx + 1, len(rows)):
            r = rows[i]
            get = lambda k: (r[cols[k]].strip() if k in cols and cols[k] < len(r) else "")   # noqa: E731
            company = re.sub(r"\s+", " ", get("company")).strip()
            if not company or len(company) < 2 or len(company) > 120 or HEAD_ONLY.match(unicodedata.normalize("NFKC", company)):
                continue
            # 見出し・区切り・注意書きの行（第5弾・遠方リスト・↓… など）は飛ばす
            filled = sum(1 for x in r if (x or "").strip())
            if has_contact_cols and not (get("tel") or get("address") or get("web")) and filled < 3:
                continue
            if TITLE_ROW.search(company) and not CONTENT["company"].search(company):
                continue
            n = norm_company(company)
            if not n:
                continue
            web = get("web")
            if not CONTENT["web"].search(unicodedata.normalize("NFKC", web or "")):
                web = links.get((i, cols.get("web", -1))) or next((u for (ri, _), u in links.items() if ri == i), "") or ""
            web = web if re.match(r"^https?://", web or "", re.I) else (("http://" + web) if web.lower().startswith("www.") else "")
            memo_parts = [get("memo")] if get("memo") and get("memo") not in ("-", "－", "ー", "―") else []
            for j, label in extra:
                v = r[j].strip() if j < len(r) else ""
                if not v or v == company or v in ("-", "－", "ー", "―", "なし"):
                    continue
                if label:
                    memo_parts.append(f"{label}: {_excel_date(v) if '日' in label else v}")
                elif len(v) >= 4 and not re.fullmatch(r"[\d.\-/〒\s]+", v) and not CONTENT["web"].search(v):
                    memo_parts.append(v)
            memo = "\n".join(memo_parts)[:3000]
            kind, done = _row_color(colors[i], cols["company"]) if i < len(colors) else (None, False)
            n_color += 1 if kind else 0
            n_done += 1 if done else 0
            row = {"company": company, "norm": n, "tel": get("tel")[:40] or None, "zip": get("zip")[:12] or None,
                   "address": get("address")[:200] or None, "industry": get("industry")[:80] or None,
                   "web": web[:300] or None, "memo": memo or None, "contact": get("contact")[:80] or None,
                   "row_no": len(out) + 1, "color": kind, "done": done, "_level": level, "_memos": [memo] if memo else []}
            if n in by_norm:
                cur = out[by_norm[n]]
                for k in ("tel", "zip", "address", "industry", "web", "contact"):
                    if not cur[k] and row[k]:
                        cur[k] = row[k]
                if row["memo"]:
                    ms = cur["_memos"]
                    if any(row["memo"] in x for x in ms):
                        pass   # もう入っている
                    elif any(x in row["memo"] for x in ms):
                        cur["_memos"] = [row["memo"] if x in row["memo"] else x for x in ms]   # 書き足された新しい方にする
                        cur["_memos"] = list(dict.fromkeys(cur["_memos"]))
                    else:
                        ms.append(row["memo"])
                if level > cur["_level"]:
                    cur.update(color=kind, done=done, _level=level)
                elif level == cur["_level"]:
                    cur["color"] = kind or cur["color"]
                    cur["done"] = cur["done"] or done
                continue
            by_norm[n] = len(out)
            out.append(row)
            if len(out) >= MAX_ROWS:
                desc.append({"sheet": name, "truncated": True})
                return out, desc
        desc[-1].update(colored=n_color, done=n_done) if desc and desc[-1].get("sheet") == name else None
    for r in out:
        r.pop("_level", None)
        r["memo"] = "\n".join(r.pop("_memos"))[:3000] or None
    return out, desc


def vendor_sees(c, vendor_id: int, pid: int) -> bool:
    """業者がこの会社の情報を（こちらが共有したリストで）見られるか"""
    return bool(c.execute("""SELECT 1 FROM prospect_list_rows r JOIN prospect_lists l ON l.id=r.list_id
        WHERE r.prospect_id=? AND l.vendor_id=? AND l.archived=0 AND r.gated=0 LIMIT 1""", (pid, vendor_id)).fetchone())


def vendor_scope(c, vendor_id: int, pids: list) -> dict:
    """{会社: (見せてよいか, 見せ始めた日時)}。見せてよくない会社は、業者がアップロードした後の情報だけ見せる"""
    out = {}
    for i in range(0, len(pids), 500):
        part = pids[i:i + 500]
        q = ",".join("?" * len(part))
        for r in c.execute(f"""SELECT r.prospect_id, MIN(r.gated) g, MIN(r.linked_at) since FROM prospect_list_rows r
                JOIN prospect_lists l ON l.id=r.list_id WHERE l.vendor_id=? AND l.archived=0 AND r.prospect_id IN ({q})
                GROUP BY r.prospect_id""", [vendor_id] + part):
            out[r["prospect_id"]] = (r["g"] == 0, r["since"] or "")
    return out


# ── 既存との突き合わせ ──
def existing_for(c, tenant_id: str, norms: set, cards: list) -> dict:
    res = {n: {"cards": 0, "deals": []} for n in norms}
    for cd in cards or []:
        n = norm_company(cd.get("company") or "")
        if n in res:
            res[n]["cards"] += 1
    for d in c.execute("SELECT id, title, stage, company_snapshot FROM deals WHERE tenant_id=?", (tenant_id,)):
        n = norm_company(d["company_snapshot"] or "")
        if n in res and len(res[n]["deals"]) < 5:
            res[n]["deals"].append({"id": d["id"], "title": d["title"], "stage": d["stage"]})
    return res


def import_list(c, tenant_id: str, name: str, filename: str, rows: list, vendor_id: Optional[int],
                uploaded_by_vendor: Optional[int], import_memo: bool, cards: Optional[list]) -> dict:
    if uploaded_by_vendor:
        if c.execute("SELECT COUNT(*) n FROM prospect_lists WHERE tenant_id=? AND uploaded_by_vendor=?",
                     (tenant_id, uploaded_by_vendor)).fetchone()["n"] >= MAX_VENDOR_LISTS:
            raise HTTPException(400, f"アップロードできるリストは{MAX_VENDOR_LISTS}件までです。先方に古いリストの整理を頼んでください")
    elif c.execute("SELECT COUNT(*) n FROM prospect_lists WHERE tenant_id=? AND uploaded_by_vendor IS NULL",
                   (tenant_id,)).fetchone()["n"] >= MAX_LISTS:
        raise HTTPException(400, f"リストは{MAX_LISTS}件までです。使わないリストを消してください")
    now = sales._now()
    cur = c.execute("""INSERT INTO prospect_lists (tenant_id, name, vendor_id, uploaded_by_vendor, filename, created_at)
        VALUES (?,?,?,?,?,?)""", (tenant_id, name, vendor_id, uploaded_by_vendor, filename, now))
    list_id = cur.lastrowid
    ex = existing_for(c, tenant_id, {r["norm"] for r in rows}, cards)
    added = merged = 0
    by_color = {k: 0 for k in STATUS_LABEL}
    n_done = 0
    for r in rows:
        p = c.execute("SELECT * FROM prospects WHERE tenant_id=? AND norm=?", (tenant_id, r["norm"])).fetchone()
        gated = 0
        if p:
            merged += 1
            sets = {"existing": json.dumps(ex.get(r["norm"]), ensure_ascii=False), "updated_at": now}
            if not uploaded_by_vendor:   # 業者のアップロードでは、こちらの控えを書き換えない
                sets.update({k: r[k] for k in ("tel", "zip", "address", "industry", "web", "contact") if r.get(k) and not p[k]})
                if r.get("address") and not p["pref"]:
                    sets["pref"] = pref_of(r["address"])
            c.execute(f"UPDATE prospects SET {', '.join(k + '=?' for k in sets)} WHERE id=?", list(sets.values()) + [p["id"]])
            pid = p["id"]
            if uploaded_by_vendor and not vendor_sees(c, uploaded_by_vendor, pid):
                gated = 1   # もともと業者に見せていない会社。アップロードより前のこちらの情報は見せない
        else:
            added += 1
            pid = c.execute("""INSERT INTO prospects (tenant_id, norm, company, tel, zip, address, pref, industry, web, contact,
                existing, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (tenant_id, r["norm"], r["company"], r.get("tel"), r.get("zip"), r.get("address"), pref_of(r.get("address")),
                 r.get("industry"), r.get("web"), r.get("contact"), json.dumps(ex.get(r["norm"]), ensure_ascii=False),
                 now, now)).lastrowid
        c.execute("""INSERT OR IGNORE INTO prospect_list_rows (list_id, prospect_id, row_no, memo, tel, address, industry, web,
            linked_at, gated, gated_orig) VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (list_id, pid, r.get("row_no"), r.get("memo"),
                                                           r.get("tel"), r.get("address"), r.get("industry"), r.get("web"), now,
                                                           gated, gated))
        meaning = COLOR_MEANING.get(r.get("color") or "")
        result = guess_result(r["memo"]) if (import_memo and r.get("memo")) else None
        if meaning == "appo":
            result = "アポ獲得"
        elif meaning == "follow":
            result = "資料送付" if re.search(r"資料|会社案内|カタログ", r.get("memo") or "") else "再コール"
        if meaning:
            by_color[meaning] += 1
        if meaning in ("ng", "hold"):
            cur_v = c.execute("SELECT verdict FROM prospects WHERE id=?", (pid,)).fetchone()["verdict"]
            if not cur_v:   # こちらがもう判定していれば変えない
                c.execute("UPDATE prospects SET verdict=?, verdict_reason=?, verdict_at=?, verdict_reason_at=?, updated_at=? WHERE id=?",
                          (meaning, f"リストの色（{COLOR_KINDS[r['color']]}）から", now, now, now, pid))
        if r.get("done"):
            n_done += 1
            c.execute("UPDATE prospects SET done_at=COALESCE(done_at, ?) WHERE id=?", (now, pid))
        memo = r.get("memo") if import_memo else None
        if memo or meaning in ("appo", "follow"):
            # 同じ会社をもう一度取り込んだとき: 前の備考に書き足したものなら置き換える（二重にしない）
            old = None
            for x in c.execute("SELECT id, memo, result FROM prospect_calls WHERE prospect_id=? AND source='import'", (pid,)):
                xm = x["memo"] or ""
                if xm == (memo or "") or (memo and xm and xm in memo):
                    old = x
                    break
                if memo and xm and memo in xm:
                    old = "skip"
                    break
            if old == "skip":
                pass
            elif old is not None:
                c.execute("UPDATE prospect_calls SET memo=?, result=COALESCE(?, result), list_id=? WHERE id=?",
                          (memo, result, list_id, old["id"]))
            else:
                c.execute("""INSERT INTO prospect_calls (tenant_id, prospect_id, list_id, vendor_id, source, result, memo, created_at)
                    VALUES (?,?,?,?,?,?,?,?)""", (tenant_id, pid, list_id, uploaded_by_vendor, "import", result, memo, now))
    return {"id": list_id, "rows": len(rows), "added": added, "merged": merged,
            "colors": {k: v for k, v in by_color.items() if v}, "done": n_done}


# ── 表示の形 ──
def _ai(p) -> dict:
    try:
        v = json.loads(p["ai_json"] or "{}")
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def _json(v, default):
    try:
        x = json.loads(v or "null")
        return x if x is not None else default
    except (TypeError, ValueError):
        return default


def priority(p, prof: str) -> tuple:
    """(点数, AIの点数か)"""
    if p["score"] is not None:
        return p["score"], True
    return rule_score(prof, p["industry"], p["company"]), False


def call_stats(c, pids: list) -> dict:
    out = {}
    if not pids:
        return out
    for i in range(0, len(pids), 500):
        part = pids[i:i + 500]
        q = ",".join("?" * len(part))
        for r in c.execute(f"""SELECT prospect_id, COUNT(*) n, MAX(COALESCE(called_at, created_at)) last,
                SUM(CASE WHEN source='call' THEN 1 ELSE 0 END) calls, MAX(CASE WHEN result='アポ獲得' THEN 1 ELSE 0 END) appo
                FROM prospect_calls WHERE prospect_id IN ({q}) GROUP BY prospect_id""", part):
            out[r["prospect_id"]] = {"n": r["n"], "calls": r["calls"], "last": r["last"], "appo": bool(r["appo"])}
        for r in c.execute(f"""SELECT pc.prospect_id, pc.result, pc.vendor_id, pc.memo, pc.source,
                COALESCE(pc.called_at, pc.created_at) at FROM prospect_calls pc
                JOIN (SELECT prospect_id, MAX(id) mid FROM prospect_calls WHERE prospect_id IN ({q}) GROUP BY prospect_id) x
                ON x.mid = pc.id""", part):
            o = out.setdefault(r["prospect_id"], {})
            o["result"] = r["result"]
            o["last_memo"] = (r["memo"] or "")[:240] or None
            o["last_src"] = "import" if r["source"] == "import" else ("vendor" if r["vendor_id"] else "us")
    return out


def deal_info(c, tenant_id: str, ids: list) -> dict:
    ids = [i for i in ids if i]
    if not ids:
        return {}
    q = ",".join("?" * len(ids))
    return {r["id"]: dict(r) for r in c.execute(
        f"SELECT id, title, stage, visit_grade, appo_at, vendor_id FROM deals WHERE tenant_id=? AND id IN ({q})", [tenant_id] + ids)}


def status_of(verdict, has_deal: bool, appo: bool, last_result, records: int) -> str:
    """一覧の「状況」（リストの色と同じ分け方）"""
    if verdict == "ng":
        return "ng"
    if has_deal or appo:
        return "appo"
    if verdict == "hold":
        return "hold"
    if last_result in ("資料送付", "再コール"):
        return "follow"
    return "calling" if records else "new"


def row_view(p, prof: str, cs: dict, deals: dict, memo: Optional[str] = None) -> dict:
    ai = _ai(p)
    sc, is_ai = priority(p, prof)
    d = deals.get(p["deal_id"]) if p["deal_id"] else None
    st = cs.get(p["id"]) or {}
    return {"status": status_of(p["verdict"], bool(d), st.get("appo"), st.get("result"), st.get("n") or 0),
            "done": bool(p["done_at"]), "last_memo": st.get("last_memo"), "last_src": st.get("last_src"),
            "id": p["id"], "company": p["company"], "tel": p["tel"], "address": p["address"], "pref": p["pref"],
            "industry": p["industry"], "web": p["official_url"] or p["web"], "verdict": p["verdict"],
            "verdict_reason": p["verdict_reason"], "verdict_at": p["verdict_at"], "verdict_reason_at": p["verdict_reason_at"],
            "ai_at": p["ai_at"],
            "rule_score": rule_score(prof, p["industry"], p["company"]), "score": sc, "score_ai": is_ai, "rank": p["rank"],
            "ai_status": p["ai_status"], "summary": ai.get("summary"), "size": ai.get("size"), "inhouse": ai.get("inhouse"),
            "calls": (cs.get(p["id"]) or {}).get("calls") or 0, "records": (cs.get(p["id"]) or {}).get("n") or 0,
            "last_result": (cs.get(p["id"]) or {}).get("result"), "last_call": (cs.get(p["id"]) or {}).get("last"),
            "existing": _json(p["existing"], None), "memo": memo,
            "deal": {"id": d["id"], "stage": d["stage"], "grade": d["visit_grade"], "appo_at": d["appo_at"],
                     "vendor_id": d["vendor_id"], "closed": d["stage"] in sales.CLOSED_STAGES} if d else None}


def list_stats(rows: list) -> dict:
    """このリストでの電話・アポの数字（判定・調査・訪問評価は会社ごとに共通）"""
    total = len(rows)
    called = [r for r in rows if r.get("l_calls")]
    reached = [r for r in called if r.get("l_reached")]
    appo = [r for r in rows if r.get("l_appo")]
    grades = {g: sum(1 for r in appo if r["deal"] and r["deal"]["grade"] == g) for g in GRADES}
    by_ind = {}
    for r in rows:
        k = (r["industry"] or "（業種なし）")[:30]
        b = by_ind.setdefault(k, {"industry": k, "n": 0, "called": 0, "appo": 0})
        b["n"] += 1
        b["called"] += 1 if r.get("l_calls") else 0
        b["appo"] += 1 if r.get("l_appo") else 0
    inds = sorted(by_ind.values(), key=lambda x: (-x["appo"], -x["n"]))[:12]
    return {"status": {k: sum(1 for r in rows if r["status"] == k) for k in STATUS_LABEL},
            "done": sum(1 for r in rows if r["done"]),
            "total": total, "ok": sum(1 for r in rows if r["verdict"] == "ok"), "ng": sum(1 for r in rows if r["verdict"] == "ng"),
            "hold": sum(1 for r in rows if r["verdict"] == "hold"), "undecided": sum(1 for r in rows if not r["verdict"]),
            "researched": sum(1 for r in rows if r["ai_status"] == "done"),
            "queued": sum(1 for r in rows if r["ai_status"] in ("queued", "running")),
            "rank": {k: sum(1 for r in rows if r["rank"] == k) for k in RANKS},
            "called": len(called), "reached": len(reached), "appo": len(appo),
            "appo_rate": round(len(appo) / len(called) * 100, 1) if called else None,
            "grades": grades, "industries": inds}


def list_rows(c, tenant_id: str, list_id: int) -> list:
    prof = _prof(tenant_id)
    ps = c.execute("""SELECT p.*, r.memo AS row_memo, r.row_no, r.tel AS r_tel, r.address AS r_address, r.industry AS r_industry,
        r.web AS r_web FROM prospect_list_rows r JOIN prospects p ON p.id=r.prospect_id
        WHERE r.list_id=? AND p.tenant_id=? ORDER BY r.row_no""", (list_id, tenant_id)).fetchall()
    cs = call_stats(c, [p["id"] for p in ps])
    deals = deal_info(c, tenant_id, [p["deal_id"] for p in ps])
    # このリストでの電話（ほかのリストでの記録は会社の行には出すが、このリストの数字には入れない）
    # 取り込んだ備考は、結果が読み取れたものだけ数える（「その他」や空は数えない）
    mine = {r["prospect_id"]: r for r in c.execute("""SELECT prospect_id, COUNT(*) n,
            MAX(CASE WHEN result IS NOT NULL AND result NOT IN ('担当者不在', 'その他') THEN 1 ELSE 0 END) reached,
            MAX(CASE WHEN result='アポ獲得' THEN 1 ELSE 0 END) appo
            FROM prospect_calls WHERE list_id=? AND (source='call' OR (result IS NOT NULL AND result<>'その他'))
            GROUP BY prospect_id""", (list_id,))}
    out = []
    for p in ps:
        v = row_view(p, prof, cs, deals, p["row_memo"])
        m = mine.get(p["id"])
        v["l_calls"] = m["n"] if m else 0
        v["l_reached"] = bool(m and m["reached"])
        v["l_appo"] = bool(m and m["appo"])
        v["row"] = {"tel": p["r_tel"], "address": p["r_address"], "industry": p["r_industry"], "web": p["r_web"]}
        out.append(v)
    return out


def index_stats(c, tenant_id: str) -> dict:
    """リスト一覧の数字をまとめて出す（リストごとに全部の行を作らない）"""
    out = {}
    for r in c.execute("""SELECT r.list_id, COUNT(*) total,
            SUM(p.verdict='ok') ok, SUM(p.verdict='ng') ng, SUM(p.verdict='hold') hold, SUM(p.verdict IS NULL) undecided,
            SUM(p.ai_status='done') researched, SUM(p.ai_status IN ('queued','running')) queued,
            SUM(p.rank='A') ra, SUM(p.rank='B') rb, SUM(p.rank='C') rc, SUM(p.rank='除外') rx
            FROM prospect_list_rows r JOIN prospects p ON p.id=r.prospect_id JOIN prospect_lists l ON l.id=r.list_id
            WHERE l.tenant_id=? GROUP BY r.list_id""", (tenant_id,)):
        out[r["list_id"]] = {"total": r["total"], "ok": r["ok"] or 0, "ng": r["ng"] or 0, "hold": r["hold"] or 0,
                             "undecided": r["undecided"] or 0, "researched": r["researched"] or 0, "queued": r["queued"] or 0,
                             "rank": {"A": r["ra"] or 0, "B": r["rb"] or 0, "C": r["rc"] or 0, "除外": r["rx"] or 0},
                             "called": 0, "reached": 0, "appo": 0, "appo_rate": None, "grades": {g: 0 for g in GRADES}}
    for r in c.execute("""SELECT pc.list_id, COUNT(DISTINCT pc.prospect_id) called,
            COUNT(DISTINCT CASE WHEN pc.result IS NOT NULL AND pc.result NOT IN ('担当者不在','その他') THEN pc.prospect_id END) reached,
            COUNT(DISTINCT CASE WHEN pc.result='アポ獲得' THEN pc.prospect_id END) appo
            FROM prospect_calls pc JOIN prospect_lists l ON l.id=pc.list_id
            WHERE l.tenant_id=? AND (pc.source='call' OR (pc.result IS NOT NULL AND pc.result<>'その他'))
            GROUP BY pc.list_id""", (tenant_id,)):
        st = out.get(r["list_id"])
        if st:
            st.update(called=r["called"], reached=r["reached"], appo=r["appo"],
                      appo_rate=round(r["appo"] / r["called"] * 100, 1) if r["called"] else None)
    # 状況（色）と完了の数
    cs = {}
    for r in c.execute("""SELECT prospect_id, COUNT(*) n, MAX(CASE WHEN result='アポ獲得' THEN 1 ELSE 0 END) appo
            FROM prospect_calls WHERE tenant_id=? GROUP BY prospect_id""", (tenant_id,)):
        cs[r["prospect_id"]] = {"n": r["n"], "appo": r["appo"]}
    for r in c.execute("""SELECT pc.prospect_id, pc.result FROM prospect_calls pc JOIN (SELECT prospect_id, MAX(id) mid
            FROM prospect_calls WHERE tenant_id=? GROUP BY prospect_id) x ON x.mid=pc.id""", (tenant_id,)):
        cs.setdefault(r["prospect_id"], {})["result"] = r["result"]
    for r in c.execute("""SELECT r.list_id, p.id, p.verdict, p.deal_id, p.done_at FROM prospect_list_rows r
            JOIN prospects p ON p.id=r.prospect_id JOIN prospect_lists l ON l.id=r.list_id WHERE l.tenant_id=?""", (tenant_id,)):
        st = out.get(r["list_id"])
        if not st:
            continue
        x = cs.get(r["id"]) or {}
        k = status_of(r["verdict"], bool(r["deal_id"]), x.get("appo"), x.get("result"), x.get("n") or 0)
        st.setdefault("status", {kk: 0 for kk in STATUS_LABEL})[k] += 1
        st["done"] = st.get("done", 0) + (1 if r["done_at"] else 0)
    for r in c.execute("""SELECT x.list_id, d.visit_grade g, COUNT(*) n FROM
            (SELECT DISTINCT pc.list_id, pc.prospect_id FROM prospect_calls pc JOIN prospect_lists l ON l.id=pc.list_id
             WHERE l.tenant_id=? AND pc.result='アポ獲得') x
            JOIN prospects p ON p.id=x.prospect_id JOIN deals d ON d.id=p.deal_id
            WHERE d.visit_grade IS NOT NULL GROUP BY x.list_id, d.visit_grade""", (tenant_id,)):
        st = out.get(r["list_id"])
        if st and r["g"] in st["grades"]:
            st["grades"][r["g"]] = r["n"]
    return out


def get_list(c, tenant_id: str, list_id: int):
    row = c.execute("SELECT * FROM prospect_lists WHERE id=? AND tenant_id=?", (list_id, tenant_id)).fetchone()
    if not row:
        raise HTTPException(404, "リストが見つかりません")
    return row


def get_prospect(c, tenant_id: str, pid: int):
    row = c.execute("SELECT * FROM prospects WHERE id=? AND tenant_id=?", (pid, tenant_id)).fetchone()
    if not row:
        raise HTTPException(404, "会社が見つかりません")
    return row


def calls_of(c, pid: int) -> list:
    return [dict(r) for r in c.execute("""SELECT * FROM prospect_calls WHERE prospect_id=?
        ORDER BY COALESCE(called_at, created_at) DESC, id DESC""", (pid,))]


# ── 案件にする ──
def _call_title(who: str, result: Optional[str], source: str) -> str:
    if source == "import":
        return "リストの備考" + (f"：{result}" if result else "")
    return f"テレアポ（{who}）" + (f"：{result}" if result else "")


def vendor_phone(c, vendor: Optional[dict], pid: int):
    """業者に渡す電話番号。業者に見せていない会社なら、業者の行に書いてあった番号だけ（こちらの控えは渡さない）"""
    if not vendor or vendor_sees(c, vendor["id"], pid):
        return "__ours__"
    raw = c.execute("""SELECT r.tel FROM prospect_list_rows r JOIN prospect_lists l ON l.id=r.list_id
        WHERE r.prospect_id=? AND l.vendor_id=? ORDER BY r.linked_at LIMIT 1""", (pid, vendor["id"])).fetchone()
    return raw["tel"] if raw else None


def open_deal(c, tenant_id: str, deal_id) -> bool:
    """まだ動いている案件か（失注・受注・保留で終わった案件はリストからまた当たれる）"""
    if not deal_id:
        return False
    d = c.execute("SELECT stage FROM deals WHERE id=? AND tenant_id=?", (deal_id, tenant_id)).fetchone()
    return bool(d) and d["stage"] not in sales.CLOSED_STAGES


def make_deal(c, tenant_id: str, p, vendor: Optional[dict] = None, appo_at: Optional[str] = None,
              contact: Optional[str] = None, department: Optional[str] = None, telapo_at: Optional[str] = None,
              vendor_phone: Optional[str] = "__ours__", appo: bool = False) -> int:
    """リストの会社を案件にする。電話の履歴とAIの調査を案件に移す"""
    if open_deal(c, tenant_id, p["deal_id"]):
        return p["deal_id"]
    vnames = sales.vendor_names(c, tenant_id)
    card = {"id": "", "company": p["company"], "name": contact, "phone": p["tel"]}
    title = f"{p['company']} テレアポ"
    note = f"案件作成（リストから・{vendor['name']}）" if vendor else "案件作成（リストから）"
    deal_id = sales.insert_deal(c, tenant_id, card, title, "テレアポ", created_note=note, pipeline="sales", owner=None)
    sets = {"prospect_id": p["id"], "telapo_at": telapo_at or datetime.now().isoformat(timespec="minutes")}
    if vendor:
        sets["vendor_id"] = vendor["id"]
        sets["vendor_contact"] = json.dumps({"company": p["company"], "contact": contact, "department": department,
                                             "phone": p["tel"] if vendor_phone == "__ours__" else vendor_phone,
                                             "email": None, "title": title}, ensure_ascii=False)
    for cl in reversed(calls_of(c, p["id"])):
        who = vnames.get(cl["vendor_id"]) or "業者" if cl["vendor_id"] else "自社"
        body = "\n".join(x for x in [f"相手: {' '.join(v for v in (cl['department'], cl['contact']) if v)}"
                                     if (cl["department"] or cl["contact"]) else None, cl["memo"]] if x)
        when = cl["called_at"] or cl["created_at"]
        act = sales.log_activity(c, tenant_id, deal_id, "call", _call_title(who, cl["result"], cl["source"]), body or None,
                                 (when + ":00") if when and len(when) == 16 else when)
        if vendor and cl["vendor_id"] == vendor["id"]:
            c.execute("UPDATE activities SET vendor_id=?, vendor_visible=1 WHERE id=?", (vendor["id"], act))
    ai = _ai(p)
    if ai:
        lines = [f"マッチ度: {p['rank'] or '—'}（{p['score'] if p['score'] is not None else '—'}点）",
                 f"公式サイト: {p['official_url'] or p['web'] or '不明'}",
                 f"事業: {ai.get('summary') or '—'}",
                 f"大物: {ai.get('size') or '不明'} ／ 自社板金: {ai.get('inhouse') or '不明'} ／ 規模: {ai.get('employees') or '不明'}",
                 f"理由: {ai.get('reason') or '—'}"]
        if ai.get("talk_hint"):
            lines.append(f"切り口: {ai['talk_hint']}")
        for e in (ai.get("evidence") or [])[:5]:
            lines.append(f"・{e.get('note') or ''} {e.get('url') or ''}".strip())
        sales.log_activity(c, tenant_id, deal_id, "memo", "AIの事前調査（リストより）", "\n".join(lines))
    if p["verdict_reason"]:
        sales.log_activity(c, tenant_id, deal_id, "memo", f"リストでの判定: {VERDICTS.get(p['verdict'] or '', '未判定')}",
                           p["verdict_reason"])
    if appo_at or appo:
        sales.change_stage(c, tenant_id, deal_id, "テレアポ", "アポ", sets, pipeline="sales")
        if appo_at:
            sets["appo_at"] = appo_at
            sets["next_action"] = f"アポ訪問（{vendor['name']}経由）" if vendor else "アポ訪問"
            sets["next_action_due"] = appo_at[:10]
        else:
            sets["next_action"] = "アポの日時を確かめる"
            sets["next_action_due"] = date.today().isoformat()
    sales.apply_sets(c, tenant_id, deal_id, sets)
    c.execute("UPDATE prospects SET deal_id=?, updated_at=? WHERE id=?", (deal_id, sales._now(), p["id"]))
    return deal_id


# ── AIの調査 ──
PROMPT = """あなたは{own}の営業リサーチ担当です。テレアポリストの1社について、ウェブで調べて「この会社に営業する価値」を判定してください。

【狙う会社の条件】
{icp}

【過去の結果（この会社の判定の参考に。同じ型なら同じ評価に寄せる）】
{examples}

【調べる会社】
会社名: {company}
住所: {address}
電話: {tel}
業種（リストの記載）: {industry}
リストに書いてあったURL: {web}

調べ方:
- 会社名と住所で検索し、公式サイトを見つける（同名の別会社に注意。住所・電話で照合する）
- 会社概要・事業内容・製品・設備一覧・協力会社募集のページを見る
- わからないことは推測で埋めず「不明」にする

最後に、次のJSONだけを出力してください（説明文は不要）。
{{"official_url": "公式サイトのURL（見つからなければ空）",
 "summary": "何を作っている会社か（1〜2文）",
 "products": "主な製品",
 "size": "製品に大型の筐体・架台・外装が要るか: 大/中/小/不明",
 "inhouse": "自社に板金・製缶・塗装の設備があるか: 有/無とみられる/不明",
 "employees": "従業員数（わかれば。例: 約120名）",
 "locations": "工場・拠点",
 "rank": "A（すぐ当たる）/B（条件を確かめて当たる）/C（優先度低い）/除外（合わない）",
 "score": 0から100の整数,
 "reason": "判定の理由（根拠になった事実を具体的に。2〜3文）",
 "talk_hint": "電話で最初に確かめることと切り口（1〜2文）",
 "evidence": [{{"url": "根拠のページ", "note": "そのページで確認したこと"}}],
 "confidence": "高/中/低（別会社と取り違えていないか・情報量）"}}"""


def settings_of(c, tenant_id: str) -> dict:
    row = c.execute("SELECT * FROM prospect_settings WHERE tenant_id=?", (tenant_id,)).fetchone()
    prof = _prof(tenant_id)
    return {"icp": (row["icp"] if row and row["icp"] else None) or DEFAULT_ICP.get(prof, DEFAULT_ICP["sheet"]),
            "icp_default": DEFAULT_ICP.get(prof, DEFAULT_ICP["sheet"]),
            "daily_cap": (row["daily_cap"] if row and row["daily_cap"] else None) or _daily_cap_default()}


def examples_for(c, tenant_id: str, limit: int = 14) -> str:
    """訪問評価の付いた案件と、理由つきの判定を手本として渡す"""
    lines = []
    for r in c.execute("""SELECT p.company, p.industry, p.ai_json, d.visit_grade FROM deals d JOIN prospects p ON p.id = d.prospect_id
            WHERE d.tenant_id=? AND d.visit_grade IS NOT NULL ORDER BY d.updated_at DESC LIMIT ?""", (tenant_id, limit)):
        summ = (_ai(r).get("summary") or "")[:80]
        lines.append(f"- 訪問評価{r['visit_grade']}: {r['company']}（{r['industry'] or '業種不明'}）{summ}")
    for r in c.execute("""SELECT company, industry, verdict, verdict_reason FROM prospects WHERE tenant_id=? AND verdict IN ('ok','ng')
            AND verdict_reason IS NOT NULL ORDER BY verdict_at DESC LIMIT ?""", (tenant_id, max(4, limit // 2))):
        lines.append(f"- 判定「{VERDICTS[r['verdict']]}」: {r['company']}（{r['industry'] or '業種不明'}）理由: {r['verdict_reason'][:100]}")
    return "\n".join(lines) or "（まだありません）"


def _clean_ai(raw: dict) -> dict:
    def s(k, n=400):
        v = raw.get(k)
        return str(v).strip()[:n] if isinstance(v, (str, int, float)) and str(v).strip() else None
    out = {k: s(k) for k in ("summary", "products", "employees", "locations", "reason", "talk_hint")}
    out["size"] = s("size") if s("size") in SIZES else "不明"
    out["inhouse"] = s("inhouse") if s("inhouse") in INHOUSE else "不明"
    out["confidence"] = s("confidence") if s("confidence") in ("高", "中", "低") else "低"
    rank = s("rank") or ""
    out["rank"] = next((r for r in RANKS if rank.startswith(r)), None)
    try:
        out["score"] = max(0, min(100, int(float(raw.get("score")))))
    except (TypeError, ValueError):
        out["score"] = {"A": 80, "B": 60, "C": 35, "除外": 10}.get(out["rank"] or "", None)
    if out["rank"] is None and out["score"] is not None:
        out["rank"] = "A" if out["score"] >= 75 else "B" if out["score"] >= 55 else "C" if out["score"] >= 25 else "除外"
    url = s("official_url", 300) or ""
    out["official_url"] = url if re.match(r"^https?://", url) else None
    ev = []
    for e in raw.get("evidence") or []:
        if isinstance(e, dict) and re.match(r"^https?://", str(e.get("url") or "")):
            ev.append({"url": str(e["url"])[:300], "note": str(e.get("note") or "")[:160]})
    out["evidence"] = ev[:6]
    return out


def _answer_json(text: str) -> dict:
    """AIの答えから判定のJSONを取り出す（前後の文・コードブロック・キーの順番の違いに強く）"""
    want = ("rank", "score", "summary", "official_url")
    dec = json.JSONDecoder()
    best = {}
    for m in re.finditer(r"\{", text or ""):
        try:
            obj, _ = dec.raw_decode(text, m.start())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and sum(1 for k in want if k in obj) >= 2:
            best = obj   # 最後に出てきた答えを使う
    return best


def ai_research(tenant_id: str, p: dict, ctx: dict) -> dict:
    """Claude のウェブ検索で1社を調べる"""
    import anthropic
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"), timeout=180, max_retries=2)
    prompt = PROMPT.format(own=ctx["own"], icp=ctx["icp"], examples=ctx["examples"], company=p["company"],
                           address=p.get("address") or "不明", tel=p.get("tel") or "不明", industry=p.get("industry") or "不明",
                           web=p.get("web") or "なし")
    model = os.getenv("SALES_RESEARCH_MODEL") or os.getenv("SALES_AI_MODEL", "claude-sonnet-5-5")
    tool = {"type": "web_search_20250305", "name": "web_search", "max_uses": _searches(),
            "user_location": {"type": "approximate", "country": "JP", "region": "Osaka", "timezone": "Asia/Tokyo"}}
    searched = True
    try:
        msgs = [{"role": "user", "content": prompt}]
        msg = client.messages.create(model=model, max_tokens=3000, tools=[tool], messages=msgs)
        for _ in range(2):   # 検索が長くて途中で止まったら続きを頼む
            if getattr(msg, "stop_reason", None) != "pause_turn":
                break
            msgs = msgs + [{"role": "assistant", "content": msg.content}]
            msg = client.messages.create(model=model, max_tokens=3000, tools=[tool], messages=msgs)
    except anthropic.BadRequestError as e:
        if "web_search" not in str(e) and "tool" not in str(e):
            raise
        searched = False   # ウェブ検索が使えない設定 → 会社名と業種だけで推定
        msg = client.messages.create(model=model, max_tokens=2000, messages=[{"role": "user", "content": prompt}])
    text = "".join(getattr(b, "text", "") or "" for b in msg.content if getattr(b, "type", "") == "text")
    urls = []
    for b in msg.content:
        if getattr(b, "type", "") == "web_search_tool_result" and isinstance(getattr(b, "content", None), list):
            for r in b.content:
                u = getattr(r, "url", None)
                if u and u not in urls:
                    urls.append(u)
    data = _answer_json(text)
    if not data:
        raise ValueError("AIの答えを読み取れませんでした")
    out = _clean_ai(data)
    if not out["evidence"]:
        out["evidence"] = [{"url": u, "note": "検索で見たページ"} for u in urls[:4]]
    out["searched"] = searched
    try:
        out["searches"] = int(getattr(getattr(msg.usage, "server_tool_use", None), "web_search_requests", 0) or 0)
    except (TypeError, ValueError):
        out["searches"] = 0
    return out


def research_one(pid: int) -> bool:
    with sales._conn() as c:
        p = c.execute("SELECT * FROM prospects WHERE id=?", (pid,)).fetchone()
        if not p:
            return False
        t = p["tenant_id"]
        st = settings_of(c, t)
        ctx = {"own": sales.display_of(t) or "自社", "icp": st["icp"], "examples": examples_for(c, t)}
        pd = dict(p)
    try:
        fn = RESEARCH_FN or ai_research
        res = fn(t, pd, ctx)
        if not isinstance(res, dict):
            raise ValueError("調査の結果が空です")
        res = dict(res, **{k: v for k, v in _clean_ai(res).items() if k not in ("searched", "searches")})
    except Exception as e:
        log.exception("調査に失敗 %s", pid)
        _write_retry("UPDATE prospects SET ai_status='error', ai_error=?, updated_at=? WHERE id=?",   # 前の結果の日時は変えない
                     (str(e)[:300], sales._now(), pid))
        return False
    _write_retry("""UPDATE prospects SET ai_status='done', ai_json=?, ai_at=?, ai_error=NULL, score=?, rank=?, official_url=?,
        updated_at=? WHERE id=?""", (json.dumps(res, ensure_ascii=False), sales._now(), res.get("score"), res.get("rank"),
                                    res.get("official_url"), sales._now(), pid))
    return True


def _write_retry(sql: str, args: tuple, tries: int = 6):
    """調査の結果は料金がかかっているので、ロックで失敗しても何度か書き直す"""
    for i in range(tries):
        try:
            with sales._conn() as c:
                c.execute(sql, args)
                c.commit()
            return
        except sqlite3.OperationalError:
            if i == tries - 1:
                log.exception("調査の結果を書けませんでした")
                return
            time.sleep(2 * (i + 1))


STALE_MIN = 15


def _claim() -> Optional[int]:
    with sales._conn() as c:
        c.execute("BEGIN IMMEDIATE")
        # 15分以上「調査中」のまま（再起動・書き込みの失敗）→ 失敗にして、もう一度頼めるようにする（勝手には調べ直さない）
        stale = (datetime.now() - timedelta(minutes=STALE_MIN)).isoformat(timespec="seconds")
        c.execute("""UPDATE prospects SET ai_status='error', ai_error='調査が途中で止まりました。もう一度「AIで調べる」を押してください'
            WHERE ai_status='running' AND (ai_started_at IS NULL OR ai_started_at < ?)""", (stale,))
        row = c.execute("SELECT id FROM prospects WHERE ai_status='queued' ORDER BY ai_requested_at, id LIMIT 1").fetchone()
        if not row:
            c.commit()
            return None
        c.execute("UPDATE prospects SET ai_status='running', ai_started_at=? WHERE id=?", (sales._now(), row["id"]))
        c.commit()
        return row["id"]


def run_pending(limit: int = 1000) -> int:
    n = 0
    while n < limit:
        pid = _claim()
        if pid is None:
            break
        research_one(pid)
        n += 1
    return n


_WORKER = {"started": False, "event": threading.Event()}


def _worker_loop():
    while True:
        _WORKER["event"].wait(30)
        _WORKER["event"].clear()
        try:
            run_pending()
        except Exception:
            log.exception("調査の順番待ちで失敗")
            time.sleep(5)


def start_worker():
    if _WORKER["started"] or os.getenv("SALES_RESEARCH_WORKER", "1") == "0":
        return
    _WORKER["started"] = True
    for _ in range(max(1, int(os.getenv("SALES_RESEARCH_THREADS", "2")))):
        threading.Thread(target=_worker_loop, daemon=True).start()
    _WORKER["event"].set()


def kick():
    _WORKER["event"].set()


def queue_research(c, tenant_id: str, pids: list, force: bool = False) -> dict:
    st = settings_of(c, tenant_id)
    today = date.today().isoformat()
    used = c.execute("""SELECT COUNT(*) n FROM prospect_research_log WHERE tenant_id=? AND requested_at >= ?""",
                     (tenant_id, today)).fetchone()["n"]
    left = max(0, st["daily_cap"] - used)
    queued = skipped = 0
    now = sales._now()
    for pid in pids:
        p = c.execute("SELECT ai_status FROM prospects WHERE id=? AND tenant_id=?", (pid, tenant_id)).fetchone()
        if not p or p["ai_status"] in ("queued", "running") or (p["ai_status"] == "done" and not force):
            skipped += 1
            continue
        if left <= 0:
            break
        c.execute("UPDATE prospects SET ai_status='queued', ai_requested_at=?, ai_error=NULL WHERE id=?", (now, pid))
        c.execute("INSERT INTO prospect_research_log (tenant_id, prospect_id, requested_at) VALUES (?,?,?)", (tenant_id, pid, now))
        queued += 1
        left -= 1
    return {"queued": queued, "skipped": skipped, "left_today": left, "daily_cap": st["daily_cap"],
            "capped": queued < len(pids) - skipped}


# ── 入力 ──
class ListUpdate(BaseModel):
    name: Optional[str] = None
    vendor_id: Optional[int] = None
    share: Optional[bool] = None      # False で業者との共有をやめる
    archived: Optional[bool] = None
    note: Optional[str] = None


class ProspectUpdate(BaseModel):
    verdict: Optional[str] = None     # ok | ng | hold | ""（未判定に戻す）
    verdict_reason: Optional[str] = None
    company: Optional[str] = None
    tel: Optional[str] = None
    address: Optional[str] = None
    industry: Optional[str] = None
    web: Optional[str] = None
    done: Optional[bool] = None       # 対応が終わった（完了）／まだ


class DoneIn(BaseModel):
    done: bool = True
    ids: Optional[list[int]] = None


class BulkVerdict(BaseModel):
    ids: list[int]
    verdict: str
    reason: Optional[str] = None


class ResearchIn(BaseModel):
    ids: Optional[list[int]] = None
    list_id: Optional[int] = None
    limit: Optional[int] = 20
    force: bool = False


class PCallIn(BaseModel):
    list_id: Optional[int] = None
    telapo_at: Optional[str] = None
    result: Optional[str] = None
    appo_at: Optional[str] = None
    next_call_on: Optional[str] = None
    contact: Optional[str] = None
    department: Optional[str] = None
    memo: Optional[str] = None
    docs: Optional[vendorreq.DocsIn] = None        # 「資料送付」のとき（宛先・送り方・送り先・資料）


class SettingsIn(BaseModel):
    icp: Optional[str] = None
    daily_cap: Optional[int] = None


class GradeIn(BaseModel):
    grade: Optional[str] = None       # S/A/B/C/F、空で外す
    note: Optional[str] = None


def _verdict(v: Optional[str]) -> Optional[str]:
    if v in (None, ""):
        return None
    if v not in VERDICTS:
        raise HTTPException(400, "判定が不正です")
    return v


def add_call(c, tenant_id: str, p, body: PCallIn, vendor: Optional[dict]) -> dict:
    """電話の記録。アポ獲得なら案件にする"""
    import vendor as vmod
    result = sales._clean_text(body.result, 20, "結果")
    if result and result not in vmod.RESULTS:
        raise HTTPException(400, "結果が不正です")
    memo = sales._clean_text(body.memo, 5000, "電話のメモ")
    appo = sales._clean_dt(body.appo_at, "アポ日時")
    tel_at = sales._clean_dt(body.telapo_at, "架電日時") or datetime.now().isoformat(timespec="minutes")
    nxt = sales._clean_date(body.next_call_on)
    contact = sales._clean_text(body.contact, 60, "相手の名前")
    dept = sales._clean_text(body.department, 80, "部署・役職")
    if appo and not result:
        result = "アポ獲得"   # アポ日時だけ入れた → アポ獲得として数える
    if not result and not memo and not appo:
        raise HTTPException(400, "結果かメモを入れてください")
    if result and result != "アポ獲得":
        appo = None   # アポ日時は「アポ獲得」のときだけ使う
    if open_deal(c, tenant_id, p["deal_id"]):
        raise HTTPException(409, "この会社はもう案件になっています。案件の画面から記録してください")
    list_id = body.list_id
    if list_id and not c.execute("SELECT 1 FROM prospect_list_rows WHERE list_id=? AND prospect_id=?", (list_id, p["id"])).fetchone():
        list_id = None
    c.execute("""INSERT INTO prospect_calls (tenant_id, prospect_id, list_id, vendor_id, source, called_at, result, contact, department,
        appo_at, next_call_on, memo, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (tenant_id, p["id"], list_id, vendor["id"] if vendor else None, "call", tel_at, result, contact, dept, appo,
               nxt, memo, sales._now()))
    deal_id = None
    if result == "アポ獲得" or appo:
        p2 = c.execute("SELECT * FROM prospects WHERE id=?", (p["id"],)).fetchone()
        vphone = vendor_phone(c, vendor, p["id"])
        deal_id = make_deal(c, tenant_id, p2, vendor=vendor, appo_at=appo, contact=contact, department=dept, telapo_at=tel_at, appo=True,
                            vendor_phone=vphone)
    elif result == "資料送付" and vendor:
        # 業者が「資料を送って」と言われた → 案件にして、こちらに送付を頼む
        docs = vendorreq._clean_docs(body.docs)
        p2 = c.execute("SELECT * FROM prospects WHERE id=?", (p["id"],)).fetchone()
        vphone = vendor_phone(c, vendor, p["id"])
        deal_id = make_deal(c, tenant_id, p2, vendor=vendor, contact=contact, department=dept, telapo_at=tel_at, vendor_phone=vphone)
        sets = {"next_action": vendorreq.SEND_DOCS, "next_action_due": date.today().isoformat()}
        sales.apply_sets(c, tenant_id, deal_id, sets)
        vendorreq.create_docs(c, tenant_id, deal_id, vendor, docs,
                              {"company": p["company"], "contact": contact, "department": dept,
                               "phone": p["tel"] if vphone == "__ours__" else vphone}, memo, tel_at)
    return {"deal_id": deal_id, "docs": result == "資料送付" and bool(deal_id)}


def build_router(check_session: Callable, fetch_cards: Callable, get_tenant: Optional[Callable] = None) -> tuple:
    admin = APIRouter(prefix="/api/sales")
    partner = APIRouter(prefix="/api/partner")
    import vendor as vmod

    def _vendor_ok(c, t, vid):
        if vid is None:
            return None
        if not c.execute("SELECT 1 FROM vendors WHERE id=? AND tenant_id=?", (vid, t)).fetchone():
            raise HTTPException(400, "業者が見つかりません")
        return vid

    def _cards(t):
        try:
            return fetch_cards(t)
        except Exception:
            log.exception("名刺の読み込みに失敗")
            return []

    def _read_upload(file: UploadFile) -> tuple:
        raw = file.file.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise HTTPException(400, "ファイルが大きすぎます（10MBまで）")
        sheets = read_table(file.filename or "", raw)
        rows, desc = parse_rows(sheets)
        if any(d.get("truncated") for d in desc):
            raise HTTPException(400, f"1つのリストは{MAX_ROWS}社までです。ファイルを分けてアップロードしてください")
        if not rows:
            raise HTTPException(400, "会社名の列が見つかりませんでした。1行目に「会社名」「電話」などの見出しを付けて、もう一度アップロードしてください")
        return rows, desc

    # ── こちら側 ──
    @admin.get("/lists")
    def lists(session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with sales._conn() as c:
            vn = sales.vendor_names(c, t)
            summary = index_stats(c, t)
            empty = {"total": 0, "ok": 0, "ng": 0, "hold": 0, "undecided": 0, "researched": 0, "queued": 0, "called": 0, "reached": 0,
                     "appo": 0, "appo_rate": None, "grades": {g: 0 for g in GRADES}, "rank": {k: 0 for k in RANKS}}
            out = []
            for l in c.execute("SELECT * FROM prospect_lists WHERE tenant_id=? ORDER BY archived, id DESC", (t,)).fetchall():
                st = summary.get(l["id"]) or empty
                out.append({"id": l["id"], "name": l["name"], "vendor_id": l["vendor_id"], "vendor": vn.get(l["vendor_id"]),
                            "uploaded_by": vn.get(l["uploaded_by_vendor"]) if l["uploaded_by_vendor"] else None,
                            "filename": l["filename"], "archived": bool(l["archived"]), "note": l["note"],
                            "created_at": l["created_at"], "stats": {k: st[k] for k in st if k != "industries"}})
            vendors = [{"id": v["id"], "name": v["name"]} for v in c.execute(
                "SELECT id, name FROM vendors WHERE tenant_id=? AND active=1 ORDER BY id", (t,))]
        return {"lists": out, "vendors": vendors, "ai": sales._ai_available() or RESEARCH_FN is not None}

    @admin.post("/lists")
    def upload(file: UploadFile = File(...), name: str = Form(""), vendor_id: Optional[int] = Form(None),
                     import_memo: bool = Form(True), session: dict = Depends(check_session)):
        t = session["tenant_id"]
        rows, desc = _read_upload(file)
        cards = _cards(t)
        with sales._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            vid = _vendor_ok(c, t, vendor_id)
            nm = sales._clean_text(name, 80, "リスト名") or os.path.splitext(file.filename or "リスト")[0][:80]
            res = import_list(c, t, nm, (file.filename or "")[:200], rows, vid, None, import_memo, cards)
            c.commit()
        return dict(res, columns=desc)

    @admin.get("/lists/{list_id}")
    def list_detail(list_id: int, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with sales._conn() as c:
            l = get_list(c, t, list_id)
            rows = list_rows(c, t, list_id)
            vn = sales.vendor_names(c, t)
        return {"list": {"id": l["id"], "name": l["name"], "vendor_id": l["vendor_id"], "vendor": vn.get(l["vendor_id"]),
                         "uploaded_by": vn.get(l["uploaded_by_vendor"]) if l["uploaded_by_vendor"] else None,
                         "archived": bool(l["archived"]), "note": l["note"], "created_at": l["created_at"], "filename": l["filename"]},
                "rows": rows, "stats": list_stats(rows), "verdicts": VERDICTS, "grades": GRADES}

    @admin.put("/lists/{list_id}")
    def update_list(list_id: int, body: ListUpdate, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with sales._conn() as c:
            cur_l = get_list(c, t, list_id)
            sets = {}
            if body.name is not None:
                nm = sales._clean_text(body.name, 80, "リスト名")
                if not nm:
                    raise HTTPException(400, "リスト名を入れてください")
                sets["name"] = nm
            if body.share is False:
                sets["vendor_id"] = None
            elif body.vendor_id is not None:
                sets["vendor_id"] = _vendor_ok(c, t, body.vendor_id)
            if body.archived is not None:
                sets["archived"] = 1 if body.archived else 0
            if body.note is not None:
                sets["note"] = sales._clean_text(body.note, 1000, "メモ")
            if sets:
                c.execute(f"UPDATE prospect_lists SET {', '.join(k + '=?' for k in sets)} WHERE id=?", list(sets.values()) + [list_id])
                if sets.get("vendor_id") and sets["vendor_id"] != cur_l["uploaded_by_vendor"]:
                    # こちらが別の業者と共有した → こちらの判断で見せる
                    c.execute("UPDATE prospect_list_rows SET gated=0 WHERE list_id=?", (list_id,))
                elif sets.get("vendor_id") and sets["vendor_id"] == cur_l["uploaded_by_vendor"]:
                    # アップロードした業者に戻した → アップロードしたときの見せ方に戻す
                    c.execute("UPDATE prospect_list_rows SET gated=gated_orig WHERE list_id=?", (list_id,))
                c.commit()
        return {"ok": True}

    @admin.delete("/lists/{list_id}")
    def delete_list(list_id: int, session: dict = Depends(check_session)):
        """リストを消す。会社は、ほかのリストに無く・記録も判定も案件も無いものだけ消す"""
        t = session["tenant_id"]
        with sales._conn() as c:
            get_list(c, t, list_id)
            pids = [r["prospect_id"] for r in c.execute("SELECT prospect_id FROM prospect_list_rows WHERE list_id=?", (list_id,))]
            c.execute("DELETE FROM prospect_list_rows WHERE list_id=?", (list_id,))
            c.execute("DELETE FROM prospect_lists WHERE id=?", (list_id,))
            removed = 0
            for pid in pids:
                if c.execute("SELECT 1 FROM prospect_list_rows WHERE prospect_id=?", (pid,)).fetchone():
                    continue
                p = c.execute("SELECT verdict, deal_id, ai_json FROM prospects WHERE id=?", (pid,)).fetchone()
                has_calls = c.execute("SELECT 1 FROM prospect_calls WHERE prospect_id=? AND source='call'", (pid,)).fetchone()
                live_deal = p and p["deal_id"] and c.execute("SELECT 1 FROM deals WHERE id=? AND tenant_id=?", (p["deal_id"], t)).fetchone()
                if p and not p["verdict"] and not live_deal and not has_calls and not p["ai_json"]:
                    c.execute("DELETE FROM prospect_calls WHERE prospect_id=?", (pid,))
                    c.execute("DELETE FROM prospects WHERE id=?", (pid,))
                    removed += 1
            c.commit()
        return {"ok": True, "removed": removed}

    @admin.get("/lists/{list_id}/export.csv")
    def export(list_id: int, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with sales._conn() as c:
            l = get_list(c, t, list_id)
            rows = list_rows(c, t, list_id)
        buf = io.StringIO()
        raw_w = csv.writer(buf)

        class _W:   # Excel で数式として動かないように
            @staticmethod
            def writerow(vals):
                raw_w.writerow([("'" + v) if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v for v in vals])
        w = _W()
        w.writerow(["会社名", "電話", "住所", "業種", "公式サイト", "判定", "判定の理由", "マッチ度", "点数", "事業（AI）", "大物", "自社板金",
                    "電話の回数", "最後の結果", "案件の段階", "訪問評価", "リストの備考"])
        for r in sorted(rows, key=lambda x: -(x["score"] or 0)):
            w.writerow([r["company"], r["tel"] or "", r["address"] or "", r["industry"] or "", r["web"] or "",
                        VERDICTS.get(r["verdict"] or "", ""), r["verdict_reason"] or "", r["rank"] or "", r["score"] if r["score_ai"] else "",
                        r["summary"] or "", r["size"] or "", r["inhouse"] or "", r["calls"], r["last_result"] or "",
                        (r["deal"] or {}).get("stage") or "", (r["deal"] or {}).get("grade") or "", r["memo"] or ""])
        from urllib.parse import quote
        return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(l['name'])}.csv"})

    @admin.get("/prospects/settings")
    def get_settings(session: dict = Depends(check_session)):
        with sales._conn() as c:
            return settings_of(c, session["tenant_id"])

    @admin.put("/prospects/settings")
    def put_settings(body: SettingsIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        icp = sales._clean_text(body.icp, 4000, "狙う会社の条件") if body.icp is not None else None
        cap = body.daily_cap
        if cap is not None and not (1 <= cap <= 1000):
            raise HTTPException(400, "1日に調べる上限は1〜1000社です")
        with sales._conn() as c:
            c.execute("""INSERT INTO prospect_settings (tenant_id, icp, daily_cap) VALUES (?,?,?)
                ON CONFLICT(tenant_id) DO UPDATE SET icp=COALESCE(?, icp), daily_cap=COALESCE(?, daily_cap)""",
                      (t, icp, cap, icp, cap))
            if body.icp is not None and not icp:
                c.execute("UPDATE prospect_settings SET icp=NULL WHERE tenant_id=?", (t,))
            c.commit()
            return settings_of(c, t)

    @admin.post("/prospects/research")
    def research(body: ResearchIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        if not (sales._ai_available() or RESEARCH_FN):
            raise HTTPException(400, "AIの設定（APIキー）がありません")
        with sales._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            if body.ids:
                pids = [int(x) for x in body.ids[:500]]
            elif body.list_id:
                get_list(c, t, body.list_id)
                prof = _prof(t)
                ps = c.execute("""SELECT p.* FROM prospect_list_rows r JOIN prospects p ON p.id=r.prospect_id
                    WHERE r.list_id=? AND p.tenant_id=?""", (body.list_id, t)).fetchall()
                ps = [p for p in ps if p["verdict"] != "ng" and not open_deal(c, t, p["deal_id"])
                      and (body.force or p["ai_status"] not in ("done", "queued", "running"))]
                ps.sort(key=lambda p: -rule_score(prof, p["industry"], p["company"]))
                pids = [p["id"] for p in ps[:max(1, min(body.limit or 20, 300))]]
            else:
                raise HTTPException(400, "調べる会社を選んでください")
            res = queue_research(c, t, pids, force=body.force)
            c.commit()
        kick()
        return res

    @admin.get("/prospects/{pid}")
    def prospect(pid: int, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with sales._conn() as c:
            p = get_prospect(c, t, pid)
            vn = sales.vendor_names(c, t)
            calls = calls_of(c, pid)
            for cl in calls:
                cl["who"] = vn.get(cl["vendor_id"], "業者") if cl["vendor_id"] else "自社"
            ls = [dict(r) for r in c.execute("""SELECT l.id, l.name, r.memo FROM prospect_list_rows r JOIN prospect_lists l ON l.id=r.list_id
                WHERE r.prospect_id=? AND l.tenant_id=? ORDER BY l.id""", (pid, t))]
            deals = deal_info(c, t, [p["deal_id"]])
            prof = _prof(t)
            sc, is_ai = priority(p, prof)
            # 業者がアップロードしたリストに載っていて、こちらの判定などがまだ見えていない業者
            hidden = []
            for r in c.execute("""SELECT DISTINCT l.vendor_id, MIN(r.linked_at) since FROM prospect_list_rows r JOIN prospect_lists l ON l.id=r.list_id
                    WHERE r.prospect_id=? AND l.tenant_id=? AND l.vendor_id IS NOT NULL AND l.archived=0 GROUP BY l.vendor_id""", (pid, t)):
                if vendor_sees(c, r["vendor_id"], pid):
                    continue
                if (p["verdict"] and not (p["verdict_at"] or "") > r["since"]) or \
                        (p["verdict_reason"] and not (p["verdict_reason_at"] or "") > r["since"]):
                    hidden.append(vn.get(r["vendor_id"], "業者"))
        d = deals.get(p["deal_id"]) if p["deal_id"] else None
        return {"id": p["id"], "company": p["company"], "tel": p["tel"], "zip": p["zip"], "address": p["address"], "pref": p["pref"],
                "industry": p["industry"], "web": p["web"], "official_url": p["official_url"], "contact": p["contact"],
                "verdict": p["verdict"], "verdict_reason": p["verdict_reason"], "verdict_at": p["verdict_at"],
                "done": bool(p["done_at"]), "done_at": p["done_at"],
                "score": sc, "score_ai": is_ai, "rank": p["rank"], "ai": _ai(p), "ai_status": p["ai_status"], "ai_at": p["ai_at"],
                "ai_error": p["ai_error"], "existing": _json(p["existing"], None), "calls": calls, "lists": ls, "hidden_from": hidden,
                "deal": {"id": d["id"], "title": d["title"], "stage": d["stage"], "grade": d["visit_grade"], "appo_at": d["appo_at"],
                         "closed": d["stage"] in sales.CLOSED_STAGES} if d else None}

    @admin.post("/prospects/{pid}/reveal")
    def reveal(pid: int, session: dict = Depends(check_session)):
        """業者がアップロードする前に付けた判定と理由を、業者にも見せる（こちらが決めたときだけ）"""
        t = session["tenant_id"]
        with sales._conn() as c:
            p = get_prospect(c, t, pid)
            now = sales._now()
            c.execute("UPDATE prospects SET verdict_at=CASE WHEN verdict IS NULL THEN verdict_at ELSE ? END, "
                      "verdict_reason_at=CASE WHEN verdict_reason IS NULL THEN verdict_reason_at ELSE ? END, updated_at=? WHERE id=?",
                      (now, now, now, p["id"]))
            c.commit()
        return {"ok": True}

    @admin.patch("/prospects/{pid}")
    def update_prospect(pid: int, body: ProspectUpdate, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        data = sales._dump(body)
        with sales._conn() as c:
            p = get_prospect(c, t, pid)
            sets = {}
            if "verdict" in data:
                nv = _verdict(data["verdict"])
                if nv != p["verdict"]:   # 同じ判定をもう一度押しても日時は変えない
                    sets["verdict"] = nv
                    sets["verdict_at"] = sales._now()
                    if "verdict_reason" not in data:
                        sets["verdict_reason"] = None   # 判定を変えたら前の理由は消す（業者に古い理由が見えないように）
            if "verdict_reason" in data:
                nr = sales._clean_text(data["verdict_reason"], 1000, "判定の理由")
                if nr != p["verdict_reason"]:
                    sets["verdict_reason"] = nr
                    sets["verdict_reason_at"] = sales._now()
            for f, n, label in (("tel", 40, "電話"), ("address", 200, "住所"), ("industry", 80, "業種"), ("web", 300, "URL")):
                if f in data:
                    sets[f] = sales._clean_text(data[f], n, label)
            if "web" in sets and sets["web"] and not re.match(r"^https?://", sets["web"]):
                raise HTTPException(400, "URLは http から入れてください")
            if "address" in sets:
                sets["pref"] = pref_of(sets["address"])
            if data.get("done") is not None and bool(p["done_at"]) != data["done"]:
                sets["done_at"] = sales._now() if data["done"] else None
            if "company" in data:
                nm = sales._clean_text(data["company"], 120, "会社名")
                if not nm:
                    raise HTTPException(400, "会社名は空にできません")
                n = norm_company(nm)
                other = c.execute("SELECT id FROM prospects WHERE tenant_id=? AND norm=? AND id<>?", (t, n, pid)).fetchone()
                if other:
                    raise HTTPException(409, "同じ名前の会社がほかのリストにあります")
                sets["company"], sets["norm"] = nm, n
            if sets:
                sets["updated_at"] = sales._now()
                c.execute(f"UPDATE prospects SET {', '.join(k + '=?' for k in sets)} WHERE id=?", list(sets.values()) + [pid])
                c.commit()
        return {"ok": True}

    @admin.post("/prospects/done")
    def bulk_done(body: DoneIn, session: dict = Depends(check_session)):
        """まとめて「完了」にする・外す"""
        t = session["tenant_id"]
        ids = list(dict.fromkeys(body.ids or []))[:MAX_ROWS]
        if not ids:
            raise HTTPException(400, "会社を選んでください")
        with sales._conn() as c:
            q = ",".join("?" * len(ids))
            if body.done:
                cur = c.execute(f"UPDATE prospects SET done_at=?, updated_at=? WHERE tenant_id=? AND done_at IS NULL AND id IN ({q})",
                                [sales._now(), sales._now(), t] + ids)
            else:
                cur = c.execute(f"UPDATE prospects SET done_at=NULL, updated_at=? WHERE tenant_id=? AND done_at IS NOT NULL AND id IN ({q})",
                                [sales._now(), t] + ids)
            c.commit()
        return {"updated": cur.rowcount}

    @admin.post("/prospects/verdict")
    def bulk_verdict(body: BulkVerdict, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        v = _verdict(body.verdict)
        reason = sales._clean_text(body.reason, 1000, "判定の理由")
        ids = [int(x) for x in body.ids[:3000]]
        with sales._conn() as c:
            n = 0
            for i in range(0, len(ids), 500):
                part = ids[i:i + 500]
                q = ",".join("?" * len(part))
                now = sales._now()
                if reason is not None:
                    sets = """verdict_at=CASE WHEN verdict IS ? THEN verdict_at ELSE ? END, verdict=?, updated_at=?,
                        verdict_reason_at=CASE WHEN verdict_reason IS ? THEN verdict_reason_at ELSE ? END, verdict_reason=?"""
                    vals = [v, now, v, now, reason, now, reason]
                else:
                    sets = """verdict_reason=CASE WHEN verdict IS ? THEN verdict_reason ELSE NULL END,
                        verdict_at=CASE WHEN verdict IS ? THEN verdict_at ELSE ? END, verdict=?, updated_at=?"""
                    vals = [v, v, now, v, now]
                n += c.execute(f"UPDATE prospects SET {sets} WHERE tenant_id=? AND id IN ({q})", vals + [t] + part).rowcount
            c.commit()
        return {"ok": True, "updated": n}

    @admin.post("/prospects/{pid}/calls")
    def our_call(pid: int, body: PCallIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with sales._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            p = get_prospect(c, t, pid)
            res = add_call(c, t, p, body, None)
            c.commit()
        return dict(res, ok=True)

    @admin.post("/prospects/{pid}/deal")
    def to_deal(pid: int, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with sales._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            p = get_prospect(c, t, pid)
            # 業者が電話していて、その業者に見せているリストの会社なら、業者の案件にする（業者も進み具合を見られる）
            last_vendor = c.execute("""SELECT v.* FROM prospect_calls pc JOIN vendors v ON v.id=pc.vendor_id
                WHERE pc.prospect_id=? AND pc.source='call' AND v.active=1 AND v.tenant_id=? ORDER BY pc.id DESC LIMIT 1""",
                                    (pid, t)).fetchone()
            lv = dict(last_vendor) if last_vendor and vendor_sees(c, last_vendor["id"], pid) else None
            deal_id = make_deal(c, t, p, vendor=lv)
            c.commit()
        return {"ok": True, "deal_id": deal_id}

    @admin.get("/deals/{deal_id}/prospect")
    def deal_prospect(deal_id: int, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with sales._conn() as c:
            d = c.execute("SELECT id, prospect_id, visit_grade FROM deals WHERE id=? AND tenant_id=?", (deal_id, t)).fetchone()
            if not d:
                raise HTTPException(404, "案件が見つかりません")
            p = c.execute("SELECT * FROM prospects WHERE id=? AND tenant_id=?", (d["prospect_id"], t)).fetchone() if d["prospect_id"] else None
            ls = [dict(r) for r in c.execute("""SELECT l.id, l.name FROM prospect_list_rows r JOIN prospect_lists l ON l.id=r.list_id
                WHERE r.prospect_id=?""", (p["id"],))] if p else []
        return {"grade": d["visit_grade"], "grades": GRADES,
                "prospect": {"id": p["id"], "rank": p["rank"], "score": p["score"], "official_url": p["official_url"] or p["web"],
                             "summary": _ai(p).get("summary"), "verdict": p["verdict"], "lists": ls} if p else None}

    @admin.put("/deals/{deal_id}/grade")
    def set_grade(deal_id: int, body: GradeIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        g = (body.grade or "").strip().upper() or None
        if g and g not in GRADES:
            raise HTTPException(400, "評価は S・A・B・C・F から選んでください")
        note = sales._clean_text(body.note, 1000, "メモ")
        with sales._conn() as c:
            d = c.execute("SELECT visit_grade FROM deals WHERE id=? AND tenant_id=?", (deal_id, t)).fetchone()
            if not d:
                raise HTTPException(404, "案件が見つかりません")
            if g != d["visit_grade"] or note:
                sales.apply_sets(c, t, deal_id, {"visit_grade": g})
                sales.log_activity(c, t, deal_id, "memo", f"訪問評価: {g or 'なし'}" + (f"（前: {d['visit_grade']}）" if d["visit_grade"] else ""), note)
                c.commit()
        return {"ok": True, "grade": g}

    # ── 業者側 ──
    current_vendor = vmod.CURRENT_VENDOR

    def vendor_list(c, vendor: dict, list_id: int):
        l = c.execute("SELECT * FROM prospect_lists WHERE id=? AND tenant_id=? AND vendor_id=? AND archived=0",
                      (list_id, vendor["tenant_id"], vendor["id"])).fetchone()
        if not l:
            raise HTTPException(404, "リストが見つかりません")
        return l

    def vendor_prospect(c, vendor: dict, pid: int):
        p = c.execute("""SELECT p.* FROM prospects p WHERE p.id=? AND p.tenant_id=? AND EXISTS (
            SELECT 1 FROM prospect_list_rows r JOIN prospect_lists l ON l.id=r.list_id
            WHERE r.prospect_id=p.id AND l.vendor_id=? AND l.archived=0)""", (pid, vendor["tenant_id"], vendor["id"])).fetchone()
        if not p:
            raise HTTPException(404, "会社が見つかりません")
        return p

    def _after(ts, since: str) -> bool:
        return bool(ts) and ts > since

    def vendor_row(r: dict, vendor: dict, own_calls: dict, scope: dict, list_row) -> dict:
        """業者に見せる行。こちらが共有していない会社は、業者がアップロードした後の情報と、業者の行に書いてあった値だけ"""
        mine = own_calls.get(r["id"]) or {}
        open_, since = scope.get(r["id"], (False, ""))
        raw = r.get("row") or {}
        show_v = open_ or _after(r.get("verdict_at"), since)
        show_r = show_v and (open_ or _after(r.get("verdict_reason_at"), since))
        show_ai = r["score_ai"] and (open_ or _after(r.get("ai_at"), since))
        d = r["deal"]
        own_deal = bool(d and d.get("vendor_id") == vendor["id"])
        other_vendor_upload = list_row["uploaded_by_vendor"] and list_row["uploaded_by_vendor"] != vendor["id"]
        v_ = r["verdict"] if show_v else None
        deal_vis = own_deal or bool(d and open_)
        return {"id": r["id"], "company": r["company"],
                "status": status_of(v_, deal_vis, mine.get("appo"), mine.get("result"), mine.get("n") or 0),
                "done": r["done"], "last_memo": mine.get("memo") or (None if other_vendor_upload else (r["memo"] or "")[:240] or None),
                "tel": r["tel"] if open_ else raw.get("tel"), "address": r["address"] if open_ else raw.get("address"),
                "industry": r["industry"] if open_ else raw.get("industry"), "web": r["web"] if open_ else raw.get("web"),
                "verdict": r["verdict"] if show_v else None, "verdict_reason": r["verdict_reason"] if show_r else None,
                "rank": r["rank"] if show_ai else None,
                "priority": r["rule_score"] if open_ else rule_score(_prof(vendor["tenant_id"]), raw.get("industry"), r["company"]),
                "calls": mine.get("n") or 0, "last_result": mine.get("result"), "last_call": mine.get("last"),
                "memo": None if other_vendor_upload else r["memo"],
                "deal": ({"stage": d["stage"], "id": d["id"], "closed": d["stage"] in sales.CLOSED_STAGES} if own_deal else {"stage": "案件化済み", "id": None, "closed": d["stage"] in sales.CLOSED_STAGES} if (d and open_) else None)}

    def own_call_stats(c, vendor: dict, pids: list) -> dict:
        """業者自身の記録と、こちらが共有したリストの備考（取り込み）だけで数える（ほかの業者の記録は入れない）"""
        out = {}
        if not pids:
            return out
        shared = [r["id"] for r in c.execute("SELECT id FROM prospect_lists WHERE tenant_id=? AND vendor_id=? AND archived=0",
                                             (vendor["tenant_id"], vendor["id"]))] or [-1]
        sq = ",".join("?" * len(shared))
        mine = f"(vendor_id=? OR (vendor_id IS NULL AND source='import' AND list_id IN ({sq})))"
        for i in range(0, len(pids), 500):
            part = pids[i:i + 500]
            q = ",".join("?" * len(part))
            for r in c.execute(f"""SELECT prospect_id, COUNT(*) n, MAX(COALESCE(called_at, created_at)) last,
                    MAX(CASE WHEN result='アポ獲得' THEN 1 ELSE 0 END) appo FROM prospect_calls
                    WHERE {mine} AND prospect_id IN ({q}) GROUP BY prospect_id""", [vendor["id"]] + shared + part):
                out[r["prospect_id"]] = {"n": r["n"], "last": r["last"], "appo": bool(r["appo"])}
            for r in c.execute(f"""SELECT pc.prospect_id, pc.result, pc.memo FROM prospect_calls pc JOIN (SELECT prospect_id, MAX(id) mid
                    FROM prospect_calls WHERE {mine} AND prospect_id IN ({q}) GROUP BY prospect_id) x ON x.mid=pc.id""",
                                  [vendor["id"]] + shared + part):
                o = out.setdefault(r["prospect_id"], {})
                o["result"] = r["result"]
                o["memo"] = (r["memo"] or "")[:240] or None
        return out

    @partner.get("/lists")
    def p_lists(vendor: dict = Depends(current_vendor)):
        t = vendor["tenant_id"]
        with sales._conn() as c:
            out = []
            for l in c.execute("SELECT * FROM prospect_lists WHERE tenant_id=? AND vendor_id=? AND archived=0 ORDER BY id DESC",
                               (t, vendor["id"])).fetchall():
                rows = list_rows(c, t, l["id"])
                mine = own_call_stats(c, vendor, [r["id"] for r in rows])
                scope = vendor_scope(c, vendor["id"], [r["id"] for r in rows])
                vr = [vendor_row(r, vendor, mine, scope, l) for r in rows]
                out.append({"id": l["id"], "name": l["name"], "created_at": l["created_at"], "total": len(vr),
                            "ok": sum(1 for r in vr if r["verdict"] == "ok"), "ng": sum(1 for r in vr if r["verdict"] == "ng"),
                            "undecided": sum(1 for r in vr if not r["verdict"]),
                            "called": sum(1 for r in vr if r["calls"]),
                            "appo": sum(1 for r in vr if r["deal"] and r["deal"]["id"]), "mine": l["uploaded_by_vendor"] == vendor["id"]})
        return {"lists": out}

    @partner.post("/lists")
    def p_upload(file: UploadFile = File(...), name: str = Form(""), vendor: dict = Depends(current_vendor)):
        t = vendor["tenant_id"]
        rows, desc = _read_upload(file)
        cards = _cards(t)
        with sales._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            n = c.execute("SELECT COUNT(*) n FROM prospect_lists WHERE tenant_id=? AND uploaded_by_vendor=? AND created_at >= ?",
                          (t, vendor["id"], date.today().isoformat())).fetchone()["n"]
            if n >= 20:
                raise HTTPException(429, "1日にアップロードできるリストは20件までです")
            nm = sales._clean_text(name, 80, "リスト名") or os.path.splitext(file.filename or "リスト")[0][:80]
            res = import_list(c, t, nm, (file.filename or "")[:200], rows, vendor["id"], vendor["id"], True, cards)
            c.commit()
        vmod._fire({"tenant_id": t, "vendor": vendor["name"], "deal_id": None, "company": nm, "new": True, "kind": "list",
                    "title": f"リストを共有しました（{res['rows']}社）", "stage": None, "appo_at": None})
        return {"id": res["id"], "rows": res["rows"], "colors": res["colors"], "done": res["done"]}

    @partner.get("/lists/{list_id}")
    def p_list(list_id: int, vendor: dict = Depends(current_vendor)):
        t = vendor["tenant_id"]
        with sales._conn() as c:
            l = vendor_list(c, vendor, list_id)
            rows = list_rows(c, t, list_id)
            mine = own_call_stats(c, vendor, [r["id"] for r in rows])
            scope = vendor_scope(c, vendor["id"], [r["id"] for r in rows])
            out = [vendor_row(r, vendor, mine, scope, l) for r in rows]
        return {"list": {"id": l["id"], "name": l["name"]}, "rows": out, "verdicts": VERDICTS}

    @partner.get("/prospects/{pid}")
    def p_prospect(pid: int, vendor: dict = Depends(current_vendor)):
        t = vendor["tenant_id"]
        with sales._conn() as c:
            p = vendor_prospect(c, vendor, pid)
            open_, since = vendor_scope(c, vendor["id"], [pid]).get(pid, (False, ""))
            shared = {r["id"] for r in c.execute("SELECT id FROM prospect_lists WHERE tenant_id=? AND vendor_id=? AND archived=0",
                                                 (t, vendor["id"]))}
            raw = c.execute("""SELECT r.tel, r.address, r.industry, r.web FROM prospect_list_rows r JOIN prospect_lists l ON l.id=r.list_id
                WHERE r.prospect_id=? AND l.vendor_id=? AND l.archived=0 ORDER BY r.gated, r.linked_at LIMIT 1""",
                            (pid, vendor["id"])).fetchone()
            calls = []
            for cl in calls_of(c, pid):
                if cl["vendor_id"] == vendor["id"]:
                    calls.append({"mine": True, "called_at": cl["called_at"] or cl["created_at"], "result": cl["result"],
                                  "contact": cl["contact"], "department": cl["department"], "appo_at": cl["appo_at"],
                                  "next_call_on": cl["next_call_on"], "memo": cl["memo"], "source": cl["source"]})
                elif cl["vendor_id"] or not (open_ or _after(cl["created_at"], since)):
                    continue   # ほかの業者の記録・共有前のこちらの記録は見せない
                elif cl["source"] == "import" and cl["list_id"] in shared:
                    calls.append({"mine": False, "called_at": cl["created_at"], "result": cl["result"], "memo": cl["memo"], "source": "import"})
                elif cl["source"] == "call":
                    calls.append({"mine": False, "called_at": cl["called_at"] or cl["created_at"], "result": cl["result"]})
            d = deal_info(c, t, [p["deal_id"]]).get(p["deal_id"]) if p["deal_id"] else None
            ai = _ai(p)
        show_v = open_ or _after(p["verdict_at"], since)
        show_r = show_v and (open_ or _after(p["verdict_reason_at"], since))
        show_ai = open_ or _after(p["ai_at"], since)
        own_deal = bool(d and d["vendor_id"] == vendor["id"])
        return {"id": p["id"], "company": p["company"], "done": bool(p["done_at"]),
                "tel": p["tel"] if open_ else (raw["tel"] if raw else None),
                "address": p["address"] if open_ else (raw["address"] if raw else None),
                "industry": p["industry"] if open_ else (raw["industry"] if raw else None),
                "web": (p["official_url"] or p["web"]) if open_ else (p["official_url"] if show_ai else (raw["web"] if raw else None)),
                "verdict": p["verdict"] if show_v else None, "verdict_reason": p["verdict_reason"] if show_r else None,
                "summary": ai.get("summary") if show_ai else None, "talk_hint": ai.get("talk_hint") if show_ai else None, "calls": calls,
                "deal": ({"stage": d["stage"], "id": d["id"], "closed": d["stage"] in sales.CLOSED_STAGES} if own_deal else {"stage": "案件化済み", "id": None, "closed": d["stage"] in sales.CLOSED_STAGES} if (d and open_) else None)}

    @partner.post("/prospects/{pid}/done")
    def p_done(pid: int, body: DoneIn, vendor: dict = Depends(current_vendor)):
        """業者が「完了」にする・外す（リストの赤い文字と同じ意味）"""
        with sales._conn() as c:
            p = vendor_prospect(c, vendor, pid)
            if bool(p["done_at"]) != body.done:
                c.execute("UPDATE prospects SET done_at=?, updated_at=? WHERE id=?",
                          (sales._now() if body.done else None, sales._now(), p["id"]))
                c.commit()
        return {"ok": True}

    @partner.post("/prospects/{pid}/calls")
    def p_call(pid: int, body: PCallIn, vendor: dict = Depends(current_vendor)):
        t = vendor["tenant_id"]
        with sales._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            p = vendor_prospect(c, vendor, pid)
            if not vendor_sees(c, vendor["id"], pid):
                live = open_deal(c, t, p["deal_id"]) and c.execute(
                    "SELECT 1 FROM deals WHERE id=? AND (vendor_id IS NULL OR vendor_id<>?)", (p["deal_id"], vendor["id"])).fetchone()
                if p["verdict"] == "ng" or live:
                    raise HTTPException(400, "この会社には記録できません。先方に確認してください")
            elif p["verdict"] == "ng":
                raise HTTPException(400, "この会社には電話しないでください（先方の判断です）")
            n1 = c.execute("SELECT COUNT(*) n FROM prospect_calls WHERE vendor_id=? AND prospect_id=? AND source='call' AND created_at >= ?",
                           (vendor["id"], pid, date.today().isoformat())).fetchone()["n"]
            if n1 >= VENDOR_CALLS_PER_COMPANY:
                raise HTTPException(429, "この会社に1日に記録できる件数を超えました")
            if body.list_id and not c.execute("SELECT 1 FROM prospect_lists WHERE id=? AND tenant_id=? AND vendor_id=? AND archived=0",
                                              (body.list_id, t, vendor["id"])).fetchone():
                body.list_id = None
            n = c.execute("SELECT COUNT(*) n FROM prospect_calls WHERE vendor_id=? AND source='call' AND created_at >= ?",
                          (vendor["id"], date.today().isoformat())).fetchone()["n"]
            if n >= vmod.DAILY_CALL_LIMIT:
                raise HTTPException(429, "1日に記録できる件数を超えました")
            res = add_call(c, t, p, body, vendor)
            c.commit()
            final = c.execute("SELECT stage, appo_at FROM deals WHERE id=?", (res["deal_id"],)).fetchone() if res["deal_id"] else None
        if res["deal_id"]:
            vmod._fire({"tenant_id": t, "vendor": vendor["name"], "deal_id": res["deal_id"], "company": p["company"], "new": True,
                        "title": f"テレアポ（{vendor['name']}）：{'資料送付' if res.get('docs') else 'アポ獲得'}（リストから）", "stage": final["stage"] if final else "アポ",
                        "appo_at": final["appo_at"] if final else None})
        return dict(res, ok=True)

    return admin, partner
