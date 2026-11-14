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
- JSON以外は書かない。

書類の読み方（板金・盤の受注生産でよく届く形）:
- 注文書・注文変更書：宛名欄（「御中」「殿」が付いた側）が自社なら、発行元（「会社名」欄・社印・右上の会社名）が counterparty。自社名を counterparty にしない。
  「注番」「注文番号」「発注番号」「注文No.」は doc_no。「依頼日」「発行日」「注文日」は doc_date。「希望納期」「納期」「納入日」は due_on。「件名」「工事名」は title。
  明細は品名・形式（寸法 W×H×D など）・定格・数量・単価を lines に入れる（形式・寸法は name に続けて書く）。
- 注文書のあとに続く「鈑金製作図」「製作仕様書」「仕様書」などの表紙と、部品図・組立図は、その注文の図面一式。1つの document（kind は「図面」）にまとめ、ページごとの図面を drawings に入れる。
  この document の ref_no には、表紙や表題欄に書かれた注文番号（なければ前の注文書の doc_no）を入れ、counterparty は注文書の発行元と同じにする。
- 製作仕様書の表紙：件名・名称・製作数・材質（フレーム・外板・扉など）・塗装色を読み、title に「名称（件名）」を入れる。drawings にはこのページも1件として、name に名称を入れる。
- 部品図：右下の表題欄から図番・図名（品名）・材質・板厚（t2.3 なら thickness は 2.3）・数量・表面処理（三価クロメートなど）を読む。図番の欄が空なら drawing_no は空にして、name に図名を入れる。
- 図面の表題欄にある「〇〇 殿」「〇〇 様」は最終のお客様や現場の名前なので counterparty にしない（title に入れる）。図面の counterparty は、図面を送ってきた会社（注文書の発行元）。
- ページの左右・上下の余白に縦書きで小さく書かれた図番や品名も読む。拡大した画像（下部・上部）があるときは、細かい字はそちらで読む。
- 1枚の注文書を複数の document に分けない。図面のページを1枚ずつ別の document にしない。"""


def ai_available() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def main_model() -> str:
    return os.getenv("ZUMEN_AI_MODEL") or os.getenv("SALES_AI_MODEL", "claude-sonnet-5-5")


def call_ai(content: list, max_tokens: int = 8000, model: str = None) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    kw = dict(model=model or main_model(), max_tokens=max_tokens, messages=[{"role": "user", "content": content}])
    # 長い返答（ページの多い図面一式）でも時間切れにならないよう、少しずつ受け取る
    if hasattr(client.messages, "stream"):
        with client.messages.stream(**kw) as st:
            msg = st.get_final_message()
    else:
        msg = client.messages.create(**kw)
    if getattr(msg, "stop_reason", "") == "max_tokens":
        logging.getLogger("zumen_ai").warning("AIの返答が長さの上限で切れました（書けていた書類だけを使います）")
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


def _lower_crop(jpg: bytes) -> Optional[bytes]:
    """ページの下35%を切り出して拡大する（表題欄の図番・材質・数量や、注文書の希望納期は小さな字で下にあることが多い）"""
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(jpg)).convert("RGB")
        w, h = im.size
        if h < 400:
            return None
        crop = im.crop((0, int(h * 0.65), w, h))
        scale = min(1568 / crop.width, 3.0)
        if scale > 1.05:
            crop = crop.resize((int(crop.width * scale), int(crop.height * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        crop.save(buf, "JPEG", quality=85)
        return buf.getvalue()
    except Exception:
        return None


def build_content(own: str, images: list, texts: list, filename: str = "", extra: str = "") -> list:
    """images: [(page_no, jpeg_bytes)]  texts: [(page_no, text)]"""
    head = PROMPT.format(own=own or "（不明）", today=date.today().isoformat(), extra=extra)
    content = [{"type": "text", "text": head + (f"\n\nファイル名: {filename}" if filename else "")}]
    tmap = dict(texts)
    for item in images[:MAX_PAGES]:
        no, jpg = item[0], item[1]
        crops = item[2] if len(item) > 2 else None
        content.append({"type": "text", "text": f"--- {no}ページ目 ---" + (f"\nPDFの文字:\n{tmap[no][:4000]}" if tmap.get(no) else "")})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(jpg).decode()}})
        if crops is None:   # 原本から拡大画像を作れなかったときは、表示用の画像の下部を使う
            low = _lower_crop(jpg)
            crops = [("下部＝表題欄・納期欄", low)] if low else []
        for label, cj in crops:
            content.append({"type": "text", "text": f"（{no}ページ目の{label}を拡大したもの）"})
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(cj).decode()}})
    if len(images) > MAX_PAGES:
        content.append({"type": "text", "text": f"（{len(images)}ページのうち先頭{MAX_PAGES}ページだけを渡しています）"})
    return content


def parse_json(text: str) -> dict:
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except ValueError:
        pass
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except ValueError:
            pass
    # 返答が途中で切れたとき：最後まで書けている書類だけを拾う
    start = t.find("[")
    if start >= 0:
        for i in range(len(t) - 1, start, -1):
            if t[i] == "}":
                try:
                    docs = json.loads(t[start:i + 1] + "]")
                    if isinstance(docs, list) and docs:
                        return {"documents": [x for x in docs if isinstance(x, dict)]}
                except ValueError:
                    continue
    raise ValueError("AIの返答を読み取れませんでした")


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


HONORIFIC = re.compile(r"\s*(御中|殿|様|さま|宛|行)\s*$")
NO_LABEL = re.compile(r"^\s*(注番|注文番号|注文No\.?|発注番号|発注No\.?|見積番号|見積No\.?|請求番号|伝票番号|図番|No\.?|NO\.?|№|#)\s*[:：.]?\s*", re.I)


def _party(v) -> str:
    t = _s(v, 120)
    for _ in range(2):
        t = HONORIFIC.sub("", t).strip()
    return t


def _no(v, n=60) -> str:
    t = _s(v, n)
    for _ in range(2):
        t = NO_LABEL.sub("", t).strip()
    return t


def _date(v) -> Optional[str]:
    t = _s(v, 20)
    w = re.match(r"(?:令和|R)\s*(\d{1,2}|元)\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})", t)
    if w:   # 和暦（令和）のまま返ってきたとき
        y = 1 if w.group(1) == "元" else int(w.group(1))
        t = f"{2018 + y}-{w.group(2)}-{w.group(3)}"
    m = re.match(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})", t)
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
        "counterparty": _party(d.get("counterparty")), "doc_no": _no(d.get("doc_no")), "ref_no": _no(d.get("ref_no")),
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
    return out[:60]


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


ORDER_KINDS = ("注文書", "注文変更書")
SET_KINDS = ("図面", "仕様書", "承認図", "その他")


def _norm(s: str) -> str:
    try:
        import zumen_match
        return zumen_match.norm_company(s or "")
    except Exception:
        return re.sub(r"\s|株式会社|有限会社|\(株\)|\(有\)|（株）|（有）", "", s or "")


def consolidate(docs: list, own: str = "") -> list:
    """1つのファイルの中での書類同士のつながりを整える
    - 自社名を取引先として読んでしまったものは空にする
    - 注文書のあとに続く図面一式には、注文書の取引先・注文番号・納期・件名を引き継ぐ
    - 同じ注文の図面が区切りで分かれていたら1つにまとめる
    - 図番の欄が空の部品図には「注文番号-図名」を仮の図番として付ける"""
    if not docs:
        return docs
    on = _norm(own)
    for d in docs:
        if on and d.get("counterparty") and _norm(d["counterparty"]) == on:
            d["counterparty"] = ""
    docs.sort(key=lambda d: min(d["pages"]))
    order = None
    for d in docs:
        if d["kind"] in ORDER_KINDS:
            order = d
            continue
        if order and d["kind"] in SET_KINDS:
            # 同じファイルで注文書のあとに続く図面一式は、注文書と同じ会社から届いたもの
            # （表題欄の「〇〇 殿」＝最終のお客様を取引先として読んでしまうのを防ぐ）
            if order.get("counterparty") and _norm(d.get("counterparty")) != _norm(order["counterparty"]):
                if d.get("counterparty") and not d.get("title"):
                    d["title"] = d["counterparty"][:60]
                d["counterparty"] = order["counterparty"]
            if not d.get("ref_no") and order.get("doc_no"):
                d["ref_no"] = order["doc_no"]
            if not d.get("due_on"):
                d["due_on"] = order.get("due_on")
            if not d.get("title"):
                d["title"] = order.get("title") or ""
            if d["kind"] == "その他" and d.get("drawings"):
                d["kind"] = "図面"
    out = []
    for d in docs:
        prev = out[-1] if out else None
        same_set = (prev and prev["kind"] in SET_KINDS and d["kind"] in SET_KINDS and "図面" in (prev["kind"], d["kind"])
                    and (prev.get("ref_no") or "") == (d.get("ref_no") or "")
                    and _norm(prev.get("counterparty")) == _norm(d.get("counterparty")) and (prev.get("ref_no") or prev.get("counterparty")))
        dup = prev and prev["kind"] == d["kind"] and set(d["pages"]) & set(prev["pages"])   # 同じページを2回読んだ
        if same_set or dup:
            if prev["kind"] != d["kind"]:
                if not any(x.get("page") in d["pages"] for x in prev["drawings"]) and d["kind"] != "図面" and not d["drawings"]:
                    prev["drawings"].append({"page": min(d["pages"]), "drawing_no": "", "rev": "", "name": d.get("title") or d["kind"], "material": "",
                                             "thickness": "", "surface": "", "change_note": "", "parts": []})
                if prev["kind"] != "図面" and d["kind"] == "図面":
                    if not prev["drawings"]:
                        prev["drawings"].append({"page": min(prev["pages"]), "drawing_no": "", "rev": "", "name": prev.get("title") or prev["kind"],
                                                 "material": "", "thickness": "", "surface": "", "change_note": "", "parts": []})
                    prev["kind"] = "図面"
            if dup:
                have = {(x.get("page"), x.get("drawing_no"), x.get("name")) for x in prev["drawings"]}
                d["drawings"] = [x for x in d["drawings"] if (x.get("page"), x.get("drawing_no"), x.get("name")) not in have]
            prev["pages"] = sorted(set(prev["pages"] + d["pages"]))
            prev["drawings"] += d["drawings"]
            prev["lines"] += d["lines"]
            prev["questions"] = (prev["questions"] + [q for q in d["questions"] if q not in prev["questions"]])[:5]
            for k in ("doc_no", "doc_date", "due_on", "title"):
                prev[k] = prev.get(k) or d.get(k)
            continue
        out.append(d)
    for d in out:
        if d["kind"] != "図面":
            continue
        filled = 0
        seen = set()
        for dw in d["drawings"]:
            if not dw.get("drawing_no") and dw.get("name") and d.get("ref_no"):
                base = f"{d['ref_no']}-{dw['name']}"[:80]
                no, k = base, 2
                while no in seen:
                    no, k = f"{base}-{k}", k + 1
                dw["drawing_no"] = no
                filled += 1
            seen.add(dw.get("drawing_no"))
        if filled:
            d["confidence"]["drawing_no"] = min(d["confidence"].get("drawing_no", 0.6), 0.6)
            q = f"図番の欄が空の図面{filled}枚に、「注文番号-図名」を仮の図番として付けました。社内で使う図番があれば直してください"
            if q not in d["questions"]:
                d["questions"] = (d["questions"] + [q])[:5]
    return out


def _context_of(docs: list) -> str:
    lines = []
    for d in docs:
        if d["kind"] in ORDER_KINDS or d.get("doc_no") or d.get("ref_no"):
            lines.append(f"- {min(d['pages'])}〜{max(d['pages'])}ページ：{d['kind']}　番号 {d.get('doc_no') or '—'}　参照 {d.get('ref_no') or '—'}"
                         f"　取引先 {d.get('counterparty') or '—'}　件名 {d.get('title') or '—'}　納期 {d.get('due_on') or '—'}")
    return ("\nこれより前のページで読み取った書類（続きのページはこれらの続きのことが多い）:\n" + "\n".join(lines[-6:])) if lines else ""


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
    try:
        return clean(parse_json(call_ai(content, max_tokens=16000)), page_count, kinds)
    except ValueError:   # 返答の形が崩れたときは1回だけ読み直す
        logging.getLogger("zumen_ai").warning("AIの返答を読み取れなかったので読み直します")
        return clean(parse_json(call_ai(content, max_tokens=16000)), page_count, kinds)


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
        return consolidate(_read_once(build_content(own, images, texts, filename, extra), n, kinds), own)
    out = []
    tmap = dict(texts)
    for i in range(0, n, CHUNK_PAGES):
        part = images[i:i + CHUNK_PAGES]
        a, b = part[0][0], part[-1][0]
        note = (extra + f"\nこのファイルは全{n}ページあります。今回はそのうち {a}〜{b}ページ目を渡しています。"
                f"ページ番号はファイル全体の番号（{a}〜{b}）で答えてください。前後のページから続いている書類もあります。" + _context_of(out))
        docs = _read_once(build_content(own, part, [(x[0], tmap[x[0]]) for x in part if x[0] in tmap], filename, note), n, kinds)
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
    return consolidate(out, own)[:60]


def guess_from_filename(filename: str) -> dict:
    """AIが使えないとき・CADファイルのときの手がかり（ファイル名から図番らしきものを拾う）"""
    stem = os.path.splitext(os.path.basename(filename or ""))[0]
    m = re.search(r"([A-Za-z]{1,6}[-_]?\d[\w-]{2,})", stem)
    return {"drawing_no": m.group(1) if m else "", "name": stem[:60]}
