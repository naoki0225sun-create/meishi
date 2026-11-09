"""資料取り込み（営業エージェント）

貼り付けた文章や Excel・Word・PowerPoint・PDF・画像から、AIが「どの会社の誰と
どんな話が進んだか」を読み取り、名刺を探して案件の追加・記録の候補を作る。
実際に追加するのは人が確認してから（画面 or Slack で「追加」と返信）。

  POST /api/sales/intake            ファイル or 文章を解析してジョブを作る
  GET  /api/sales/intake/{id}       ジョブの中身（名刺の照合は毎回やり直す）
  POST /api/sales/intake/{id}/apply 確認した内容で追加
"""
import base64
import io
import json
import logging
import os
import re
import unicodedata
import zipfile
from datetime import date
from difflib import SequenceMatcher
from typing import Callable, Optional
from xml.etree import ElementTree as ET

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

import profiles
import sales
from dedupe import email_key, name_key, phone_key

log = logging.getLogger("intake")

MAX_FILE_BYTES = 15 * 1024 * 1024
MAX_TEXT_CHARS = 80_000
TEXT_EXT = {".txt", ".csv", ".tsv", ".md", ".markdown", ".eml", ".log", ".srt", ".vtt", ".json", ".html", ".htm", ".rtf"}
IMAGE_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
              ".gif": "image/gif", ".webp": "image/webp"}
SUPPORTED = sorted(TEXT_EXT | {".docx", ".xlsx", ".xlsm", ".pptx", ".pdf"} | set(IMAGE_MIME))
KINDS = {"meeting", "call", "email", "memo"}


def init_db():
    with sales._conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS intake_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            source TEXT NOT NULL,
            filename TEXT,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL,
            analysis TEXT,
            applied TEXT,
            error TEXT
        )""")
        c.commit()


# ── 文字の取り出し ──
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PR = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def _xml(z: zipfile.ZipFile, name: str):
    try:
        return ET.fromstring(z.read(name))
    except KeyError:
        return None


def _w_text(el) -> str:
    out = []
    for n in el.iter():
        if n.tag == W + "t" and n.text:
            out.append(n.text)
        elif n.tag in (W + "tab",):
            out.append("\t")
        elif n.tag in (W + "br", W + "cr"):
            out.append("\n")
    return "".join(out)


def docx_text(raw: bytes) -> str:
    z = zipfile.ZipFile(io.BytesIO(raw))
    root = _xml(z, "word/document.xml")
    if root is None:
        raise ValueError("Wordファイルの中身が読めません")
    lines = []
    body = root.find(W + "body")
    for el in (body if body is not None else []):
        if el.tag == W + "p":
            t = _w_text(el).strip()
            if t:
                lines.append(t)
        elif el.tag == W + "tbl":
            for tr in el.iter(W + "tr"):
                cells = [_w_text(tc).strip().replace("\n", " ") for tc in tr.findall(W + "tc")]
                if any(cells):
                    lines.append(" | ".join(cells))
    comments = _xml(z, "word/comments.xml")
    if comments is not None:
        cs = []
        for cm in comments.findall(W + "comment"):
            t = _w_text(cm).strip()
            if t:
                cs.append(f"- {cm.get(W + 'author') or ''}: {t}")
        if cs:
            lines += ["", "【コメント】"] + cs
    return "\n".join(lines)


def _rich_text(si) -> str:
    """文字列セルの本文（ふりがな rPh は入れない）"""
    out = []
    for ch in si:
        if ch.tag == S + "t":
            out.append(ch.text or "")
        elif ch.tag == S + "r":
            out.extend(t.text or "" for t in ch.findall(S + "t"))
    return "".join(out)


def _col_index(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def xlsx_text(raw: bytes, max_rows: int = 3000) -> str:
    z = zipfile.ZipFile(io.BytesIO(raw))
    shared = []
    sst = _xml(z, "xl/sharedStrings.xml")
    if sst is not None:
        for si in sst.findall(S + "si"):
            shared.append(_rich_text(si))
    wb = _xml(z, "xl/workbook.xml")
    rels = _xml(z, "xl/_rels/workbook.xml.rels")
    target = {}
    if rels is not None:
        for rel in rels.findall(PR + "Relationship"):
            target[rel.get("Id")] = rel.get("Target")
    sheets = []
    if wb is not None:
        for sh in wb.iter(S + "sheet"):
            tgt = target.get(sh.get(R + "id"), "")
            tgt = tgt.lstrip("/")
            path = tgt if tgt.startswith("xl/") else "xl/" + tgt
            sheets.append((sh.get("name"), path))
    if not sheets:
        sheets = [(n.split("/")[-1], n) for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n)]
    lines, rows_total = [], 0
    for name, path in sheets:
        root = _xml(z, path)
        if root is None:
            continue
        lines.append(f"## シート: {name}")
        for row in root.iter(S + "row"):
            vals = {}
            for c in row.findall(S + "c"):
                t = c.get("t")
                v = c.find(S + "v")
                if t == "s" and v is not None:
                    try:
                        val = shared[int(v.text)]
                    except (ValueError, IndexError):
                        val = ""
                elif t == "inlineStr":
                    val = _rich_text(c.find(S + "is")) if c.find(S + "is") is not None else ""
                else:
                    val = v.text if v is not None and v.text else ""
                if val.strip():
                    vals[_col_index(c.get("r", "A1"))] = val.strip().replace("\n", " ")
            if vals:
                width = max(vals) + 1
                lines.append("\t".join(vals.get(i, "") for i in range(width)))
                rows_total += 1
                if rows_total >= max_rows:
                    lines.append("（以下省略）")
                    return "\n".join(lines)
    # セルのメモ・コメント
    notes = []
    for n in z.namelist():
        if re.match(r"xl/(comments\d*|comments/comment\d+)\.xml$", n):
            root = _xml(z, n)
            for cm in root.iter(S + "comment"):
                t = "".join(x.text or "" for x in cm.iter(S + "t")).strip()
                if t:
                    notes.append(f"- {cm.get('ref')}: {t}")
        elif re.match(r"xl/threadedComments/.*\.xml$", n):
            root = _xml(z, n)
            for el in root.iter():
                if el.tag.endswith("}text") and (el.text or "").strip():
                    notes.append(f"- {el.text.strip()}")
    if notes:
        lines += ["", "【セルのコメント】"] + notes
    return "\n".join(lines)


def pptx_text(raw: bytes) -> str:
    z = zipfile.ZipFile(io.BytesIO(raw))
    names = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                   key=lambda n: int(re.findall(r"\d+", n)[-1]))
    lines = []
    for i, n in enumerate(names, 1):
        root = _xml(z, n)
        texts = []
        for p in root.iter(A + "p"):
            t = "".join(x.text or "" for x in p.iter(A + "t")).strip()
            if t:
                texts.append(t)
        if texts:
            lines.append(f"## スライド{i}")
            lines += texts
    return "\n".join(lines)


def decode_text(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp932", "euc-jp"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def subtitle_text(t: str) -> str:
    """SRT/VTT（Plaud Note などの文字起こし）から番号と時刻を落とし、話者と発言だけにする"""
    out = []
    for line in t.splitlines():
        s_ = line.strip()
        if not s_ or s_ == "WEBVTT" or s_.isdigit() or "-->" in s_ or s_.startswith(("NOTE", "STYLE")):
            continue
        s_ = re.sub(r"<v(?:\.[^\s>]*)?\s+([^>]+)>", r"\1: ", s_)
        s_ = re.sub(r"</?[a-z][^>]*>", "", s_)
        if out and out[-1] == s_:
            continue
        out.append(s_)
    return "\n".join(out)


def to_content(filename: str, raw: bytes) -> tuple[list, str]:
    """AIに渡す content ブロックと、記録用のテキスト（PDF・画像は空）"""
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in (".doc", ".xls", ".ppt"):
        raise ValueError(f"古い形式（{ext}）は読めません。{ext}x 形式で保存し直すかPDFにしてください")
    if ext == ".pdf":
        b64 = base64.b64encode(raw).decode()
        return [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}}], ""
    if ext in IMAGE_MIME:
        b64 = base64.b64encode(raw).decode()
        return [{"type": "image", "source": {"type": "base64", "media_type": IMAGE_MIME[ext], "data": b64}}], ""
    if ext == ".docx":
        text = docx_text(raw)
    elif ext in (".xlsx", ".xlsm"):
        text = xlsx_text(raw)
    elif ext == ".pptx":
        text = pptx_text(raw)
    elif ext in (".srt", ".vtt"):
        text = subtitle_text(decode_text(raw))
    elif ext in (".html", ".htm"):
        text = re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style).*?</\1>", " ", decode_text(raw)))
        text = re.sub(r"[ \t]+", " ", text)
    elif ext in TEXT_EXT or not ext:
        text = decode_text(raw)
    else:
        raise ValueError(f"この形式（{ext}）には対応していません。対応: {' '.join(SUPPORTED)}")
    if not text.strip():
        raise ValueError("文字が見つかりませんでした")
    return [], text


# ── AI ──
PROMPT = """あなたは{own}の営業アシスタントです。以下の資料から、営業管理に登録すべき「取引先の会社×人×案件」を読み取ってください。
今日は{today}です。資料：{source}

ルール:
- {own}自身の社員は取引先ではありません（担当者として出てくるだけ）
- 書かれていないことは推測で埋めず null にする。日付は YYYY-MM-DD（年が無ければ今日に近い年）
- 同じ会社・同じ案件の話は1件にまとめる。別案件なら分ける
- 表・一覧（訪問リストなど）は1行を1件として扱う。ランク・分類・評価などの列は summary の先頭に「ランクB」のように書く
- 「-」や空欄は情報なしとして扱う。人名の「さん」「様」は外す
- 営業と関係ない行（社内連絡・雑談など）は含めない
{pipeline_rule}{profile_rule}- 失注なら lost_reason を次から選ぶ: {lost}
- kind は meeting（打合せ・訪問）/ call（電話）/ email（メール）/ memo（その他）
- summary は短く（1〜2行）。evidence は根拠になった原文を40字以内で抜き出す

JSONだけを返してください（コードブロックや説明は不要）:
{{"overview": "資料の概要（1行）",
  "items": [{{"pipeline": "sales", "company": "会社名", "person": "氏名 or null", "department": "部署・役職 or null",
    "email": "メール or null", "phone": "電話 or null", "title": "案件名（例: 制御盤筐体 10台）",
    "kind": "meeting", "occurred_at": "YYYY-MM-DD or null", "summary": "記録する内容",
    "stage": "見積提出 or null", "lost_reason": null, "amount": 1200000,
    "next_action": "次にやること or null", "next_action_due": "YYYY-MM-DD or null", "evidence": "原文の抜粋"}}],
  "unclear": ["読み取れなかった・判断できなかった点"]}}
"""


def ai_available() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def call_ai(content: list, max_tokens: int = 16000) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    msg = client.messages.create(model=os.getenv("SALES_AI_MODEL", "claude-sonnet-5-5"),
                                 max_tokens=max_tokens, messages=[{"role": "user", "content": content}])
    return "".join(getattr(b, "text", "") for b in msg.content)


def _parse_json(text: str) -> dict:
    """AIの応答からJSONを取り出す。長すぎて途中で切れていても、読めた分の items を拾う"""
    text = text or ""
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    k = text.find('"items"')
    if k < 0:
        return {}
    k = text.find("[", k)
    if k < 0:
        return {}
    dec = json.JSONDecoder()
    items, pos = [], k + 1
    while True:
        while pos < len(text) and text[pos] in " \r\n\t,":
            pos += 1
        if pos >= len(text) or text[pos] != "{":
            break
        try:
            obj, pos = dec.raw_decode(text, pos)
        except json.JSONDecodeError:
            break
        items.append(obj)
    if not items:
        return {}
    ov = re.search(r'"overview"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    return {"overview": json.loads(f'"{ov.group(1)}"') if ov else "", "items": items,
            "unclear": ["資料が長いため途中までしか読み取れませんでした。残りは分けて取り込んでください"],
            "_cut": True}


def _date_or_none(v) -> Optional[str]:
    v = (v or "").strip() if isinstance(v, str) else ""
    return v if sales.DATE_RE.match(v) else None


def _clean_item(it: dict, pipelines: Optional[list] = None) -> Optional[dict]:
    def s(k, n):
        v = it.get(k)
        if v is None:
            return None
        v = str(v).strip()
        return v[:n] if v and v.lower() not in ("null", "none") else None
    company = s("company", 120)
    person = s("person", 60)
    if not company and not person and not s("email", 200):
        return None
    keys = [p["key"] for p in (pipelines or [sales.PIPELINES["sales"]])]
    pipeline = s("pipeline", 10)
    if pipeline not in keys:
        pipeline = "sales"
    pl = sales.PIPELINES[pipeline]
    stage = s("stage", 20)
    if stage not in pl["stages"]:
        stage = None
    lost = s("lost_reason", 20)
    if lost not in sales.LOST_REASONS:
        lost = "その他" if stage == pl["lost"] and pl["reason_required"] else None
    if person:
        person = re.sub(r"\s*(さん|様|氏)$", "", person) or None
    kind = s("kind", 10)
    amount = it.get("amount")
    try:
        amount = int(amount) if amount not in (None, "") else None
        if amount is not None and amount < 0:
            amount = None
    except (TypeError, ValueError):
        amount = None
    return {
        "pipeline": pipeline, "company": company, "person": person, "department": s("department", 120),
        "email": s("email", 200), "phone": s("phone", 40), "title": s("title", 120),
        "kind": kind if kind in KINDS else "memo",
        "occurred_at": _date_or_none(it.get("occurred_at")),
        "summary": s("summary", 3000), "stage": stage, "lost_reason": lost, "amount": amount,
        "next_action": s("next_action", 200), "next_action_due": _date_or_none(it.get("next_action_due")),
        "evidence": s("evidence", 500),
        "segment": s("segment", 40) if s("segment", 40) in profiles.SEGMENTS else None,
        "channel": s("channel", 40) if s("channel", 40) in profiles.CHANNELS else None,
        "site_name": s("site_name", 120), "end_user": s("end_user", 120),
        "delivery_on": _date_or_none(it.get("delivery_on")),
    }


def _pipeline_rule(pipelines: list) -> str:
    sp = sales.PIPELINES["sales"]
    rule = (f"- stage は次のどれか、または null: {'・'.join(sp['stages'])}\n"
            "  （見積を出した→見積提出、発注・受注→受注、断られた→失注、打合せ予定→アポ、要望を聞いた→ヒアリング）\n")
    if any(p["key"] == "bp" for p in pipelines):
        bp = sales.PIPELINES["bp"]
        rule = ("- pipeline は通常の営業なら \"sales\"、自社メディア「Blue Print（ブループリント）」の取材・掲載の話なら \"bp\"\n"
                + rule.replace("- stage は", "- sales の stage は")
                + f"- bp の stage は次のどれか、または null: {'・'.join(bp['stages'])}\n")
    else:
        rule = '- pipeline は常に "sales"\n' + rule
    return rule


def analyze(own: str, source: str, content: list, text: str) -> dict:
    today = date.today().isoformat()
    pipelines = sales.pipelines_for(own)
    profile_rule = ""
    if sales.profile_key(own) == "panel":
        profile_rule = (profiles.PANEL_INTAKE_HINT +
                        '- 各 item に "segment", "channel", "site_name", "end_user", "delivery_on"（納期・受電日 YYYY-MM-DD or null）も入れる\n')
    prompt = PROMPT.format(own=own, today=today, source=source, pipeline_rule=_pipeline_rule(pipelines),
                           profile_rule=profile_rule, lost="・".join(sales.LOST_REASONS))
    blocks = list(content)
    if text:
        if len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS] + "\n（長いため以下省略）"
        blocks.append({"type": "text", "text": f"<資料>\n{text}\n</資料>"})
    blocks.append({"type": "text", "text": prompt})
    raw = call_ai(blocks)
    data = _parse_json(raw)
    if not data:
        log.warning("取り込み: AIの応答を解析できません: %s", (raw or "")[:500])
        raise ValueError("AIの応答を読み取れませんでした。もう一度お試しください")
    items = [x for x in (_clean_item(i, pipelines) for i in (data.get("items") or []) if isinstance(i, dict)) if x]
    unclear = [str(u)[:300] for u in (data.get("unclear") or []) if u][:20]
    return {"overview": str(data.get("overview") or "")[:300], "items": items[:100], "unclear": unclear,
            "truncated": len(data.get("items") or []) > 100}


# ── 名刺の照合 ──
def norm_company(name: str) -> str:
    n = unicodedata.normalize("NFKC", name or "")
    n = re.sub(r"(株式会社|有限会社|合同会社|\(株\)|\(有\)|㈱|㈲)", "", n)
    n = re.sub(r"(inc\.?|corp\.?|co\.,?\s*ltd\.?|llc|ltd\.?)", "", n, flags=re.I)
    n = re.sub(r"[\s()（）・.,]", "", n).lower()
    return n


def _company_match(a: str, b: str) -> float:
    if not a or not b:
        return 0
    if a == b:
        return 1
    if len(a) >= 2 and len(b) >= 2 and (a in b or b in a):
        return 0.9
    r = SequenceMatcher(None, a, b).ratio()
    return r if r >= 0.75 else 0


def _hira(s: str) -> str:
    return "".join(chr(ord(ch) - 0x60) if "ァ" <= ch <= "ヶ" else ch for ch in s)


def _is_kana(s: str) -> bool:
    return bool(s) and all("ぁ" <= ch <= "ゖ" or ch == "ー" for ch in s)


def match_cards(item: dict, cards: list) -> list:
    e = email_key(item.get("email"))
    ph = phone_key(item.get("phone"))
    ic = norm_company(item.get("company"))
    ip = name_key(re.sub(r"(さん|様|氏)$", "", (item.get("person") or "").strip()))
    ip_kana = _hira(ip) if _is_kana(_hira(ip)) else ""
    scored = []
    for c in cards:
        score, why = 0, ""
        if e and e == email_key(c.get("email")):
            score, why = 100, "メールが一致"
        elif ph and ph in (phone_key(c.get("mobile")), phone_key(c.get("phone"))) and \
                (not ip or name_key(c.get("name")) == ip):
            score, why = 92, "電話番号が一致"
        else:
            cm = _company_match(ic, norm_company(c.get("company")))
            cn = name_key(c.get("name"))
            if ip_kana and _hira(name_key(c.get("name_kana"))).startswith(ip_kana):
                cn = ip  # カタカナの名字とふりがなが一致
            if cm and ip and cn == ip:
                score, why = int(80 + 15 * cm), "会社と氏名が一致"
            elif cm and ip and len(ip) >= 2 and cn and (ip in cn or cn in ip):
                score, why = int(70 + 15 * cm), "会社と名字が一致"
            elif cm and not ip:
                score, why = int(35 + 15 * cm), "会社が一致（担当者不明）"
            elif cm:
                score, why = int(20 + 15 * cm), "会社だけ一致"
            elif ip and cn == ip and len(ip) >= 3:
                score, why = 45, "氏名だけ一致"
        if score:
            scored.append((score, why, c))
    scored.sort(key=lambda x: (-x[0], x[2].get("company") or "", x[2].get("name") or ""))
    return [{"score": s, "why": w, "card": sales._card_summary(c)} for s, w, c in scored[:6]]


def decide(cands: list) -> tuple[str, Optional[str]]:
    """matched / ambiguous / not_found と、初期選択の名刺"""
    if not cands:
        return "not_found", None
    top = cands[0]
    second = cands[1]["score"] if len(cands) > 1 else 0
    if top["score"] >= 90 and top["score"] > second:
        return "matched", top["card"]["id"]
    if top["score"] >= 75 and top["score"] - second >= 15:
        return "matched", top["card"]["id"]
    if top["score"] >= 50:
        # 同点の候補があるときは人に選んでもらう
        return "ambiguous", (None if top["score"] == second else top["card"]["id"])
    return "ambiguous" if top["score"] >= 35 else "not_found", None


def enrich(tenant_id: str, analysis: dict, cards: list) -> list:
    """AIの読み取り結果に、名刺の候補と既存案件を付ける"""
    with sales._conn() as c:
        rows = c.execute("""SELECT id, card_id, title, stage, pipeline, updated_at FROM deals
            WHERE tenant_id=? ORDER BY updated_at DESC""", (tenant_id,)).fetchall()
    deals_by_card = {}
    for r in rows:
        deals_by_card.setdefault(r["card_id"], []).append(dict(r))
    out = []
    for idx, it in enumerate(analysis.get("items") or []):
        cands = match_cards(it, cards)
        status, card_id = decide(cands)
        open_deals = {}
        for cand in cands:
            cid = cand["card"]["id"]
            open_deals[cid] = [d for d in deals_by_card.get(cid, []) if d["stage"] not in sales.CLOSED_STAGES]
        deal_id = None
        same = [d for d in open_deals.get(card_id, []) if (d["pipeline"] or "sales") == it.get("pipeline", "sales")]
        if card_id and same:
            ds = same
            if it.get("title"):
                ds = sorted(ds, key=lambda d: -SequenceMatcher(None, d["title"], it["title"]).ratio())
            deal_id = ds[0]["id"]
        out.append({**it, "idx": idx, "status": status, "candidates": cands, "card_id": card_id,
                    "open_deals": open_deals, "action": ("existing" if deal_id else "new") if card_id else "skip",
                    "deal_id": deal_id})
    return out


# ── 追加 ──
class Decision(BaseModel):
    idx: int
    action: str  # new / existing / nocard（名刺なしで新規）/ skip
    pipeline: Optional[str] = None
    owner: Optional[str] = None   # 新しく作る案件の自社担当
    card_id: Optional[str] = None
    deal_id: Optional[int] = None
    title: Optional[str] = None
    stage: Optional[str] = None
    lost_reason: Optional[str] = None
    kind: Optional[str] = None
    occurred_at: Optional[str] = None
    summary: Optional[str] = None
    next_action: Optional[str] = None
    next_action_due: Optional[str] = None
    amount: Optional[int] = None


class ApplyRequest(BaseModel):
    decisions: list[Decision]


def _get_job(c, tenant_id: str, job_id: int):
    row = c.execute("SELECT * FROM intake_jobs WHERE id=? AND tenant_id=?", (job_id, tenant_id)).fetchone()
    if not row:
        raise HTTPException(404, "取り込みが見つかりません")
    return row


_OWN = {}


def own_display(tenant_id: str) -> str:
    return _OWN.get(tenant_id, "")


def apply_decisions(tenant_id: str, job_id: int, decisions: list[Decision], cards: list, own: str = "") -> dict:
    by_id = {c["id"]: c for c in cards}
    created, recorded, skipped, errors = [], [], 0, []
    with sales._conn() as c:
        job = _get_job(c, tenant_id, job_id)
        if job["status"] == "applied":
            raise HTTPException(409, "この取り込みは追加済みです")
        if job["status"] != "ready":
            raise HTTPException(409, "読み取りが終わっていません")
        source = job["filename"] or "貼り付けた文章"
        raw_items = json.loads(job["analysis"] or "{}").get("items") or []
        allowed = [p["key"] for p in sales.pipelines_for(own or own_display(tenant_id))]
        members = set(sales.member_names(c, tenant_id))
        c.execute("BEGIN")  # 全件を1つのまとまりで確定する（途中で落ちても二重登録しない）
        for d in decisions:
            if d.action == "skip":
                skipped += 1
                continue
            label = f"{d.idx + 1}件目"
            c.execute("SAVEPOINT item")
            try:
                src = raw_items[d.idx] if 0 <= d.idx < len(raw_items) else {}
                if d.action == "nocard":
                    if not (src.get("company") or src.get("person")):
                        raise HTTPException(400, "会社名も氏名も分からないため名刺なしでは追加できません")
                    card = {"id": "", "company": src.get("company"), "name": src.get("person"),
                            "email": src.get("email"), "phone": src.get("phone")}
                else:
                    card = by_id.get(d.card_id or "")
                    if not card:
                        raise HTTPException(400, "名刺を選んでください")
                pipeline = d.pipeline or src.get("pipeline") or "sales"
                if pipeline not in allowed:
                    pipeline = "sales"
                pl = sales.PIPELINES[pipeline]
                stage = d.stage if d.stage in sales.ALL_STAGES else None
                kind = d.kind if d.kind in KINDS else "memo"
                body = (d.summary or "").strip()
                body = (body + "\n\n" if body else "") + f"（{source} から取り込み）"
                occurred = _date_or_none(d.occurred_at)
                occurred = f"{occurred}T09:00:00" if occurred else None
                sets = {}
                if d.action in ("new", "nocard"):
                    if stage and stage not in pl["stages"]:
                        raise HTTPException(400, f"「{stage}」は{pl['label']}のステージではありません")
                    init_stage = stage if stage and stage not in sales.CLOSED_STAGES else pl["first"]
                    note = "名刺なし・" if d.action == "nocard" else ""
                    title = d.title or src.get("title") or re.sub(r"^ランク\s*[A-Z]\s*", "", (src.get("summary") or "").split("\n")[0])[:40]
                    deal_id = sales.insert_deal(c, tenant_id, card, title, init_stage, d.amount, None,
                                                d.next_action, d.next_action_due,
                                                created_note=f"案件作成（{init_stage}・{note}取り込み）",
                                                pipeline=pipeline,
                                                owner=d.owner if d.owner in members else None,
                                                extra={f: src.get(f) for f in sales.EXTRA_FIELDS if src.get(f)})
                    cur_stage = init_stage
                elif d.action == "existing":
                    row = sales._get_deal_row(c, tenant_id, int(d.deal_id or 0))
                    deal_id, cur_stage = row["id"], row["stage"]
                    pl = sales.pipeline_of(row)
                    if stage and stage not in pl["stages"]:
                        stage = None  # 種類が違うステージは無視
                    if d.next_action:
                        sets["next_action"] = sales._clean_text(d.next_action, 200, "次のアクション")
                        sets["next_action_due"] = _date_or_none(d.next_action_due)
                    if d.amount is not None and d.amount >= 0:
                        sets["amount"] = d.amount
                else:
                    raise HTTPException(400, "操作が不正です")
                title = {"meeting": "打合せ", "call": "電話", "email": "メール", "memo": "メモ"}[kind]
                sales.log_activity(c, tenant_id, deal_id, kind, f"{title}（取り込み）", body, occurred)
                if stage and stage != cur_stage:
                    lost = d.lost_reason if d.lost_reason in sales.LOST_REASONS else None
                    if stage == pl["lost"] and pl["reason_required"] and not lost:
                        lost = "その他"
                    sales.change_stage(c, tenant_id, deal_id, cur_stage, stage, sets,
                                       lost_reason=lost if stage == pl["lost"] else None, pipeline=pl["key"])
                if sets:
                    sales.apply_sets(c, tenant_id, deal_id, sets)
                c.execute("RELEASE item")
                (recorded if d.action == "existing" else created).append(deal_id)
            except HTTPException as e:
                c.execute("ROLLBACK TO item")
                c.execute("RELEASE item")
                errors.append(f"{label}: {e.detail}")
            except Exception:
                c.rollback()
                log.exception("取り込みの追加に失敗")
                raise HTTPException(500, "追加の途中でエラーが起きました。何も追加していません。もう一度お試しください")
        result = {"created": created, "recorded": recorded, "skipped": skipped, "errors": errors}
        if created or recorded:
            c.execute("UPDATE intake_jobs SET status='applied', applied=? WHERE id=?",
                      (json.dumps(result, ensure_ascii=False), job_id))
        c.commit()
    return result


def default_decisions(items: list, owner: Optional[str] = None) -> list[Decision]:
    """Slack で「追加」と返されたとき：名刺が確定したものだけ追加する"""
    out = []
    for it in items:
        ok = it["status"] == "matched" and it["card_id"]
        out.append(Decision(idx=it["idx"], action=it["action"] if ok else "skip", card_id=it["card_id"], owner=owner,
                            pipeline=it.get("pipeline"),
                            deal_id=it["deal_id"], title=it.get("title"), stage=it.get("stage"),
                            lost_reason=it.get("lost_reason"), kind=it.get("kind"),
                            occurred_at=it.get("occurred_at"), summary=it.get("summary"),
                            next_action=it.get("next_action"), next_action_due=it.get("next_action_due"),
                            amount=it.get("amount")))
    return out


def prepare(own: str, filename: Optional[str], raw: Optional[bytes], text: Optional[str]) -> tuple:
    """入力を確かめてAIに渡す形にする（ここまでは即座にエラーを返す）"""
    if not ai_available():
        raise HTTPException(400, "AIのキー（ANTHROPIC_API_KEY）が未設定のため読み取れません")
    content, body_text = [], ""
    source = "貼り付けた文章"
    if raw is not None:
        if len(raw) > MAX_FILE_BYTES:
            raise HTTPException(400, "ファイルが大きすぎます（15MBまで）")
        try:
            content, body_text = to_content(filename or "", raw)
        except (ValueError, zipfile.BadZipFile, ET.ParseError) as e:
            raise HTTPException(400, f"{filename}: {e}" if not isinstance(e, zipfile.BadZipFile)
                                else f"{filename}: ファイルが壊れているか、形式が違います")
        source = f"ファイル「{filename}」"
        if text and text.strip():
            body_text = (body_text + "\n\n【補足】\n" + text.strip()) if body_text else text.strip()
    else:
        body_text = (text or "").strip()
        if not body_text:
            raise HTTPException(400, "文章を入力するかファイルを選んでください")
    return source, content, body_text


def new_job(tenant_id: str, source_kind: str, filename: Optional[str]) -> int:
    with sales._conn() as c:
        cur = c.execute("""INSERT INTO intake_jobs (tenant_id, source, filename, created_at, status)
            VALUES (?,?,?,?,?)""", (tenant_id, source_kind, filename, sales._now(), "analyzing"))
        c.commit()
        return cur.lastrowid


def run_job(job_id: int, own: str, prepared: tuple) -> bool:
    """AIで読み取ってジョブに保存する。失敗したら status=error"""
    source, content, body_text = prepared
    try:
        analysis = analyze(own, source, content, body_text)
        status, err, data = "ready", None, json.dumps(analysis, ensure_ascii=False)
    except Exception as e:
        log.exception("取り込み解析エラー")
        status, err, data = "error", str(e)[:500], None
    with sales._conn() as c:
        c.execute("UPDATE intake_jobs SET status=?, analysis=?, error=? WHERE id=?", (status, data, err, job_id))
        c.commit()
    return status == "ready"


def create_job(tenant_id: str, own: str, source_kind: str, filename: Optional[str],
               raw: Optional[bytes], text: Optional[str]) -> int:
    """解析まで済ませてジョブを返す（Slack 用。Slack 側はもともと裏で動いている）"""
    _OWN[tenant_id] = own
    prepared = prepare(own, filename, raw, text)
    job_id = new_job(tenant_id, source_kind, filename)
    if not run_job(job_id, own, prepared):
        with sales._conn() as c:
            err = c.execute("SELECT error FROM intake_jobs WHERE id=?", (job_id,)).fetchone()["error"]
        raise HTTPException(502, f"AIでの読み取りに失敗しました: {err}")
    return job_id


def job_view(tenant_id: str, job_id: int, cards: list) -> dict:
    with sales._conn() as c:
        job = dict(_get_job(c, tenant_id, job_id))
    if job["status"] in ("analyzing", "error"):
        job.pop("analysis", None)
        job["items"] = []
        return job
    analysis = json.loads(job.pop("analysis") or "{}")
    job["applied"] = json.loads(job["applied"]) if job.get("applied") else None
    job["overview"] = analysis.get("overview")
    job["unclear"] = analysis.get("unclear") or []
    job["truncated"] = analysis.get("truncated", False)
    job["items"] = enrich(tenant_id, analysis, cards)
    return job


def build_router(check_session: Callable, fetch_cards: Callable, get_tenant: Optional[Callable] = None) -> APIRouter:
    r = APIRouter(prefix="/api/sales/intake")

    def own_name(t):
        name = ((get_tenant(t) if get_tenant else None) or {}).get("display_name") or t
        _OWN[t] = name
        return name

    @r.post("")
    async def create(background: BackgroundTasks, file: Optional[UploadFile] = File(None),
                     text: Optional[str] = Form(None), session: dict = Depends(check_session)):
        """読み取りは時間がかかるので裏で動かし、画面は GET で完了を待つ"""
        t = session["tenant_id"]
        raw = await file.read(MAX_FILE_BYTES + 1) if file is not None and file.filename else None
        filename = file.filename if raw is not None else None
        own = own_name(t)
        prepared = prepare(own, filename, raw, text)
        job_id = new_job(t, "web", filename)
        background.add_task(run_job, job_id, own, prepared)
        return {"id": job_id, "status": "analyzing", "filename": filename, "items": []}

    @r.get("/{job_id}")
    def get(job_id: int, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        own_name(t)
        return job_view(t, job_id, fetch_cards(t))

    @r.post("/{job_id}/apply")
    def apply(job_id: int, body: ApplyRequest, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        own_name(t)
        if not body.decisions:
            raise HTTPException(400, "追加する内容がありません")
        return apply_decisions(t, job_id, body.decisions, fetch_cards(t), own_name(t))

    return r
