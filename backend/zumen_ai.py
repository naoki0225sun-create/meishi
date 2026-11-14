"""
図面管理の AI 読み取り
====================
AI には「書いてあること」だけを読ませる。会社・図番・案件との照合はコード側（zumen_match）でやる。
テストでは call_ai を差し替える。
"""

import base64
import json
import logging
import os
import re
import unicodedata
from datetime import date
from typing import Optional

KINDS = ["図面", "注文書", "注文変更書", "見積書", "請求書", "納品書", "発注書", "検査成績書", "写真", "その他"]
COST_KINDS = {"請求書", "発注書"}
MAX_PAGES = 12

PROMPT = """あなたは製造業（板金・制御盤・キュービクル）の事務担当です。届いた書類の画像と、PDFから取り出せた文字を読み、書いてあることだけをJSONで返してください。推測で埋めないでください。

自社名: {own}
今日の日付: {today}
{extra}
1つのファイルに複数の書類が入っていることがあります（FAXやスキャンでまとめて取り込んだもの）。書類の区切りごとに documents の要素を分けてください。ページ番号は1から数えます。

返すJSONの形:
{{"documents": [{{
  "pages": [1, 2],
  "kind": "図面|注文書|注文変更書|見積書|請求書|納品書|発注書|検査成績書|写真|その他",
  "direction": "受け取り|発行",
  "counterparty": "自社ではない側の会社名（書いてあるとおり。拠点名があれば残す）",
  "doc_no": "この書類の番号（注文番号・見積番号・請求番号など）",
  "ref_no": "この書類が参照している別の書類の番号（注文書なら見積番号、変更書なら元の注文番号など）",
  "doc_date": "YYYY-MM-DD（発行日）",
  "amount": 税抜の合計金額（整数。なければ null）,
  "due_on": "YYYY-MM-DD（納期。明細ごとに違えば一番早い日）",
  "title": "件名・品名の要約（30字以内）",
  "lines": [{{"drawing_no": "", "rev": "", "name": "", "material": "", "thickness": "", "qty": 数量, "unit_price": 単価, "amount": 金額, "due_on": "YYYY-MM-DD"}}],
  "drawings": [{{"page": 1, "drawing_no": "", "rev": "", "name": "", "material": "", "thickness": "", "surface": "表面処理", "change_note": "改訂内容（改訂履歴欄の最新行）", "parts": [{{"drawing_no": "", "name": "", "qty": 1}}]}}],
  "confidence": {{"kind": 0.0〜1.0, "counterparty": 0.0〜1.0, "doc_no": 0.0〜1.0, "amount": 0.0〜1.0, "due_on": 0.0〜1.0, "drawing_no": 0.0〜1.0, "rev": 0.0〜1.0}},
  "questions": ["読み取れなかった点・担当者に確認したい点（あれば。1文ずつ、です・ます調の分かりやすい日本語で。例：納期の年が読み取れません。2026年でよいですか？）"]
}}]}}

決まりごと:
- direction は自社が受け取った書類なら「受け取り」、自社が出した書類なら「発行」。注文書は客先から届けば「受け取り」。
- 図面は表題欄から図番・品名・材質・板厚・表面処理・版（REV・改訂・版の欄）を読む。組立図に部品表があれば parts に入れる。
- 注文書・見積書は明細の行を lines に入れる。図番が書かれていればそれも。
- 年が和暦なら西暦に直す。年が書かれていなければ今日に近い年にする。
- 金額のカンマや「円」「￥」は外して整数にする。消費税は含めない。
- 読めない項目は空文字か null。confidence は自信の度合い。
- JSON以外は書かない。"""


def ai_available() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def main_model() -> str:
    return os.getenv("ZUMEN_AI_MODEL") or os.getenv("SALES_AI_MODEL", "claude-sonnet-5-5")


def call_ai(content: list, max_tokens: int = 8000, model: str = None) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    msg = client.messages.create(model=model or main_model(),
                                 max_tokens=max_tokens, messages=[{"role": "user", "content": content}])
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


def build_content(own: str, images: list, texts: list, filename: str = "", extra: str = "") -> list:
    """images: [(page_no, jpeg_bytes)]  texts: [(page_no, text)]"""
    head = PROMPT.format(own=own or "（不明）", today=date.today().isoformat(), extra=extra)
    content = [{"type": "text", "text": head + (f"\n\nファイル名: {filename}" if filename else "")}]
    tmap = dict(texts)
    for no, jpg in images[:MAX_PAGES]:
        content.append({"type": "text", "text": f"--- {no}ページ目 ---" + (f"\nPDFの文字:\n{tmap[no][:4000]}" if tmap.get(no) else "")})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(jpg).decode()}})
    if len(images) > MAX_PAGES:
        content.append({"type": "text", "text": f"（{len(images)}ページのうち先頭{MAX_PAGES}ページだけを渡しています）"})
    return content


def parse_json(text: str) -> dict:
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except ValueError:
        m = re.search(r"\{.*\}", t, re.S)
        if not m:
            raise ValueError("AIの返答を読み取れませんでした")
        return json.loads(m.group(0))


def _s(v, n=200) -> str:
    if v is None:
        return ""
    return unicodedata.normalize("NFKC", str(v)).strip()[:n]


def _int(v) -> Optional[int]:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return int(v)
    t = re.sub(r"[,，円¥￥\s]", "", unicodedata.normalize("NFKC", str(v)))
    try:
        return int(float(t))
    except ValueError:
        return None


def _num(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(re.sub(r"[,，\s]", "", unicodedata.normalize("NFKC", str(v))))
    except ValueError:
        return None


def _date(v) -> Optional[str]:
    t = _s(v, 20)
    m = re.match(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", t)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None


def _conf(v) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0.5


def clean_doc(d: dict, page_count: int, kinds: list = None) -> dict:
    kind = _s(d.get("kind"), 10)
    if kind not in (kinds or KINDS):
        kind = "その他"
    direction = _s(d.get("direction"), 6)
    if direction not in ("受け取り", "発行"):
        direction = "発行" if kind in ("見積書", "発注書") else "受け取り"
    pages = []
    for p in d.get("pages") or []:
        pi = _int(p)
        if pi and 1 <= pi <= max(1, page_count):
            pages.append(pi)
    pages = sorted(set(pages)) or list(range(1, max(1, page_count) + 1))
    lines = []
    for ln in (d.get("lines") or [])[:200]:
        if not isinstance(ln, dict):
            continue
        row = {"drawing_no": _s(ln.get("drawing_no"), 80), "rev": _s(ln.get("rev"), 20), "name": _s(ln.get("name"), 120),
               "material": _s(ln.get("material"), 60), "thickness": _s(ln.get("thickness"), 20),
               "qty": _num(ln.get("qty")), "unit_price": _int(ln.get("unit_price")), "amount": _int(ln.get("amount")),
               "due_on": _date(ln.get("due_on"))}
        if row["amount"] is None and row["qty"] and row["unit_price"] is not None:
            row["amount"] = int(round(row["qty"] * row["unit_price"]))
        if any(row[k] for k in ("drawing_no", "name", "amount")):
            lines.append(row)
    drawings = []
    for dw in (d.get("drawings") or [])[:100]:
        if not isinstance(dw, dict):
            continue
        parts = []
        for p in (dw.get("parts") or [])[:200]:
            if isinstance(p, dict) and (p.get("drawing_no") or p.get("name")):
                parts.append({"drawing_no": _s(p.get("drawing_no"), 80), "name": _s(p.get("name"), 120), "qty": _num(p.get("qty")) or 1})
        row = {"page": _int(dw.get("page")) or (pages[0] if pages else 1), "drawing_no": _s(dw.get("drawing_no"), 80),
               "rev": _s(dw.get("rev"), 20), "name": _s(dw.get("name"), 120), "material": _s(dw.get("material"), 60),
               "thickness": _s(dw.get("thickness"), 20), "surface": _s(dw.get("surface"), 60),
               "change_note": _s(dw.get("change_note"), 300), "parts": parts}
        if row["drawing_no"] or row["name"]:
            drawings.append(row)
    amount = _int(d.get("amount"))
    if amount is None and lines and all(x["amount"] is not None for x in lines):
        amount = sum(x["amount"] for x in lines)
    due = _date(d.get("due_on"))
    line_dues = [x["due_on"] for x in lines if x["due_on"]]
    if line_dues and (not due or min(line_dues) < due):
        due = min(line_dues)
    conf = d.get("confidence") if isinstance(d.get("confidence"), dict) else {}
    return {
        "pages": pages, "kind": kind, "direction": direction,
        "counterparty": _s(d.get("counterparty"), 120), "doc_no": _s(d.get("doc_no"), 60), "ref_no": _s(d.get("ref_no"), 60),
        "doc_date": _date(d.get("doc_date")), "amount": amount, "due_on": due, "title": _s(d.get("title"), 60),
        "lines": lines, "drawings": drawings,
        "confidence": {k: _conf(conf.get(k, 0.6)) for k in ("kind", "counterparty", "doc_no", "amount", "due_on", "drawing_no", "rev")},
        "questions": [_s(q, 200) for q in (d.get("questions") or []) if _s(q)][:5],
    }


def clean(data: dict, page_count: int, kinds: list = None) -> list:
    docs = data.get("documents") if isinstance(data, dict) else None
    if not isinstance(docs, list) or not docs:
        docs = [data] if isinstance(data, dict) and data.get("kind") else []
    out = [clean_doc(d, page_count, kinds) for d in docs if isinstance(d, dict)]
    return out[:20]


def _shaky(docs: list) -> bool:
    """安いモデルの答えを上位モデルで読み直すべきか（自信が低い・問いが残る・読めていない）"""
    if not docs:
        return True
    for d in docs:
        cf = [v for v in (d.get("confidence") or {}).values() if isinstance(v, (int, float))]
        if (cf and min(cf) < 0.75) or d.get("questions") or d.get("kind") == "その他":
            return True
    return False


STATS = {"fast": 0, "escalated": 0}


CHUNK_PAGES = int(os.getenv("ZUMEN_AI_CHUNK_PAGES", "10"))


def _read_once(content: list, page_count: int, kinds: list = None) -> list:
    fast = os.getenv("ZUMEN_AI_MODEL_FAST")
    if fast and fast != main_model():
        try:
            data = parse_json(call_ai(content, model=fast))
            raw_docs = data.get("documents") if isinstance(data, dict) else None
            STATS["fast"] += 1
            if isinstance(raw_docs, list) and not _shaky([d for d in raw_docs if isinstance(d, dict)]):
                return clean(data, page_count, kinds)
            STATS["escalated"] += 1
        except Exception:
            logging.getLogger("zumen_ai").exception("安いモデルでの読み取りに失敗。上位モデルで読みます")
    raw = call_ai(content, max_tokens=16000)
    return clean(parse_json(raw), page_count, kinds)


def _same_doc(a: dict, b: dict) -> bool:
    """区切りをまたいで続いている書類か（同じ種類で、番号が同じか、図面で取引先が同じ）"""
    if a["kind"] != b["kind"]:
        return False
    if a["doc_no"] and b["doc_no"]:
        return a["doc_no"] == b["doc_no"]
    return a["kind"] == "図面" and (a["counterparty"] or "") == (b["counterparty"] or "")


def read_document(own: str, images: list, texts: list, filename: str = "", extra: str = "", kinds: list = None) -> list:
    """ZUMEN_AI_MODEL_FAST を入れると、まず安いモデルで読み、迷ったものだけ上位モデルで読み直す。
    ページが多いファイル（注文書と製作図・部品図をまとめてスキャンしたもの等）は CHUNK_PAGES ずつに分けて読み、つなぎ直す"""
    n = len(images)
    if n <= CHUNK_PAGES:
        return _read_once(build_content(own, images, texts, filename, extra), n, kinds)
    out = []
    tmap = dict(texts)
    for i in range(0, n, CHUNK_PAGES):
        part = images[i:i + CHUNK_PAGES]
        a, b = part[0][0], part[-1][0]
        note = (extra + f"\nこのファイルは全{n}ページあります。今回はそのうち {a}〜{b}ページ目を渡しています。"
                f"ページ番号はファイル全体の番号（{a}〜{b}）で答えてください。前後のページから続いている書類もあります。")
        docs = _read_once(build_content(own, part, [(p, tmap[p]) for p, _ in part if p in tmap], filename, note), n, kinds)
        for d in docs:
            d["pages"] = [p for p in d["pages"] if a <= p <= b] or list(range(a, b + 1))
            for dw in d["drawings"]:
                if not (a <= (dw.get("page") or 0) <= b):
                    dw["page"] = d["pages"][0]
        docs.sort(key=lambda d: d["pages"][0])
        if out and docs and _same_doc(out[-1], docs[0]) and docs[0]["pages"][0] == max(out[-1]["pages"]) + 1:
            head = docs.pop(0)
            prev = out[-1]
            prev["pages"] = sorted(set(prev["pages"] + head["pages"]))
            prev["lines"] += head["lines"]
            prev["drawings"] += head["drawings"]
            prev["questions"] = (prev["questions"] + head["questions"])[:5]
            for k in ("doc_no", "ref_no", "doc_date", "amount", "due_on", "title", "counterparty"):
                prev[k] = prev[k] or head[k]
        out += docs
    return out[:60]


def guess_from_filename(filename: str) -> dict:
    """AIが使えないとき・CADファイルのときの手がかり（ファイル名から図番らしきものを拾う）"""
    stem = os.path.splitext(os.path.basename(filename or ""))[0]
    m = re.search(r"([A-Za-z]{1,6}[-_]?\d[\w-]{2,})", stem)
    return {"drawing_no": m.group(1) if m else "", "name": stem[:60]}
