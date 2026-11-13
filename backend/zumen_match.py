"""
図面管理の照合ロジック（画面やDBに触らない純粋な関数だけ）
=========================================================
- 会社名・図番の正規化
- 会社の照合（別名辞書 → 正規化名 → 似た名前 → 名刺）
- 版の並び順
- 案件・書類への紐づけの点数（理由つき）
"""

import re
import unicodedata
from datetime import date
from difflib import SequenceMatcher
from typing import Optional

# ── 正規化 ──
_CORP = re.compile(r"(株式会社|有限会社|合同会社|合資会社|合名会社|一般社団法人|\(株\)|\(有\)|\(合\)|㈱|㈲)")
_CORP_EN = re.compile(r"(inc\.?|corp\.?|corporation|co\.,?\s*ltd\.?|ltd\.?|llc|k\.k\.)", re.I)
_BRANCH = re.compile(r"(本社|本店|支店|支社|営業所|工場|事業所|出張所|センター)$")


_HONOR = re.compile(r"[\s　]*(御中|様|殿|宛)[\s　]*$")


def clean_name(name: Optional[str]) -> str:
    """表示用：「テスト工業株式会社 御中」→「テスト工業株式会社」"""
    return _HONOR.sub("", unicodedata.normalize("NFKC", name or "").strip())


def norm_company(name: Optional[str]) -> str:
    n = clean_name(name)
    n = _CORP.sub("", n)
    n = _CORP_EN.sub("", n)
    n = re.sub(r"[\s()（）・.,、。\"'「」『』]", "", n).lower()
    return n


def base_company(name: Optional[str]) -> str:
    """「○○工業 尼崎工場」→「○○工業」（拠点名を外した会社名の正規化）"""
    raw = clean_name(name)
    parts = re.split(r"[\s　]+", raw)
    if len(parts) > 1 and _BRANCH.search(parts[-1]):
        raw = " ".join(parts[:-1])
    return norm_company(raw)


def company_similarity(a: str, b: str) -> float:
    na, nb = norm_company(a), norm_company(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if len(na) >= 3 and len(nb) >= 3 and (na in nb or nb in na):
        return 0.9
    return round(SequenceMatcher(None, na, nb).ratio(), 3)


def norm_drawing_no(s: Optional[str]) -> str:
    n = unicodedata.normalize("NFKC", s or "").upper()
    n = re.sub(r"[‐‑‒–—―−－ｰ﹣]", "-", n)
    n = re.sub(r"\s+", "", n)
    n = re.sub(r"^(図番|DWG\.?NO\.?|DRAWINGNO\.?|NO\.?)[:：]?", "", n)
    return n.strip("-_.")


def mail_domain(addr: Optional[str]) -> str:
    m = re.search(r"@([\w.-]+)", addr or "")
    if not m:
        return ""
    d = m.group(1).lower()
    free = ("gmail.com", "yahoo.co.jp", "outlook.jp", "outlook.com", "hotmail.com", "icloud.com", "nifty.com", "ezweb.ne.jp", "docomo.ne.jp", "softbank.ne.jp")
    return "" if d in free else d


# ── 会社の照合 ──
def match_company(raw_name: str, companies: list, aliases: list, cards: list = (), domain: str = "", fuzzy: bool = True) -> list:
    """候補を点数の高い順に返す。
    companies: [{id, name, norm_name, mail_domains}]  aliases: [{company_id, alias, norm_alias}]
    cards: 名刺の会社名の一覧（str）
    戻り値: [{kind: 'company'|'card'|'new', company_id?, name, score, reason}]"""
    out, seen = [], set()
    n = norm_company(raw_name)
    nb = base_company(raw_name)

    def add(cid, name, score, reason, kind="company"):
        key = (kind, cid if cid else name)
        if key in seen:
            return
        seen.add(key)
        out.append({"kind": kind, "company_id": cid, "name": name, "score": score, "reason": reason})

    by_id = {c["id"]: c for c in companies}
    if domain:
        for c in companies:
            doms = [d.strip() for d in (c.get("mail_domains") or "").split(",") if d.strip()]
            if domain in doms:
                add(c["id"], c["name"], 100, f"送信元のドメイン {domain} が登録済み")
    if n:
        for a in aliases:
            if a["norm_alias"] == n and a["company_id"] in by_id:
                add(a["company_id"], by_id[a["company_id"]]["name"], 98, f"別名「{a['alias']}」と一致")
        for c in companies:
            if c["norm_name"] == n:
                add(c["id"], c["name"], 96, "会社名が一致")
        if nb and nb != n:
            for c in companies:
                if c["norm_name"] == nb:
                    add(c["id"], c["name"], 90, "拠点名を除くと会社名が一致")
            for a in aliases:
                if a["norm_alias"] == nb and a["company_id"] in by_id:
                    add(a["company_id"], by_id[a["company_id"]]["name"], 88, f"拠点名を除くと別名「{a['alias']}」と一致")
        sims = []
        for c in (companies if fuzzy else ()):   # 似た名前探しは重いので、一覧では省く
            s = company_similarity(raw_name, c["name"])
            for a in aliases:
                if a["company_id"] == c["id"]:
                    s = max(s, company_similarity(raw_name, a["alias"]))
            if s >= 0.85:
                sims.append((s, c))
        for s, c in sorted(sims, key=lambda x: -x[0])[:3]:
            add(c["id"], c["name"], int(round(s * 80)), f"似た会社名（{int(s * 100)}%）")
        known = {c["norm_name"] for c in companies}
        for name in cards:
            nn = norm_company(name)
            if nn and nn not in known and (nn == n or nn == nb):
                add(None, name, 85, "名刺にある会社。フォルダはまだない", kind="card")
    out.sort(key=lambda x: -x["score"])
    if raw_name and not any(x["score"] >= 85 for x in out):
        out.append({"kind": "new", "company_id": None, "name": clean_name(raw_name), "score": 0, "reason": "新しい会社として作る"})
    return out[:4]


# ── 版の並び順 ──
_KANJI_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def rev_order(rev: Optional[str]) -> Optional[tuple]:
    """版 → 並べ替えのキー（体系, 値）。比べられない表記は None。
    初版・空・'-'・'0' は (0, 0)。英字 A→Z は (1, n)。数字・改n・第n版・Rn は (2, n)"""
    r = unicodedata.normalize("NFKC", rev or "").strip().upper()
    r = re.sub(r"^(REV\.?|REVISION|改訂|版数|版)[:：\s]*", "", r)
    if r in ("", "-", "0", "初版", "新規", "ORIGINAL", "初"):
        return (0, 0)
    m = re.fullmatch(r"[A-Z]{1,2}", r)
    if m:
        v = 0
        for ch in r:
            v = v * 26 + (ord(ch) - 64)
        return (1, v)
    m = re.fullmatch(r"(?:第|改|R|V)?\s*(\d{1,3})\s*(?:版|訂|次)?", r)
    if m:
        return (2, int(m.group(1)))
    m = re.fullmatch(r"(?:第|改)?([一二三四五六七八九十])(?:版|訂|次)?", r)
    if m:
        return (2, _KANJI_NUM[m.group(1)])
    return None


def compare_rev(a: Optional[str], b: Optional[str]) -> Optional[int]:
    """a と b を比べる。a が新しければ 1、同じなら 0、古ければ -1、比べられなければ None"""
    ka, kb = rev_order(a), rev_order(b)
    if ka is None or kb is None:
        return None
    if ka[0] != kb[0]:
        if ka[0] == 0:
            return -1
        if kb[0] == 0:
            return 1
        return None
    return (ka[1] > kb[1]) - (ka[1] < kb[1])


# ── 紐づけの点数 ──
WEIGHTS = {"no_match": 50, "drawing": 35, "amount": 20, "words": 15, "stage": 10, "near": 10, "only": 10, "amount_off": -20}
AUTO_PICK = 70
SHOW = 40
STAGE_FIT = {
    "注文書": {"見積提出", "ヒアリング", "受注", "承認図作成", "承認図提出", "承認済"},
    "注文変更書": {"受注", "承認図作成", "承認図提出", "承認済", "部材手配", "製作中"},
    "図面": {"ヒアリング", "見積提出", "受注", "承認図作成", "承認図提出", "承認済", "部材手配", "製作中"},
    "見積書": {"ヒアリング", "見積提出", "アポ"},
    "請求書": {"受注", "出荷", "据付・試運転", "完了", "製作中"},
    "納品書": {"受注", "出荷", "据付・試運転", "完了", "製作中"},
    "発注書": {"受注", "承認済", "部材手配", "製作中"},
}


def _words(s: str) -> set:
    s = unicodedata.normalize("NFKC", s or "")
    toks = set(re.findall(r"[一-龥ぁ-んァ-ヶー]{2,}|[A-Za-z0-9]{3,}", s))
    return {t.lower() for t in toks if t not in ("株式会社", "有限会社", "御中", "注文書", "見積書", "請求書")}


def _days(a: Optional[str], b: Optional[str]) -> Optional[int]:
    try:
        return abs((date.fromisoformat(a[:10]) - date.fromisoformat(b[:10])).days)
    except (TypeError, ValueError):
        return None


def score_deal(doc: dict, deal: dict, open_count: int) -> tuple:
    """doc: {kind, doc_no, ref_no, doc_date, amount, title, lines:[{drawing_no, name}], drawings:[{drawing_no, name}]}
    deal: {id, title, stage, amount, created_at, site_name, doc_nos:set, drawing_nos:set, quote_amounts:list, last_doc_date}
    戻り値: (点数, [理由])"""
    score, reasons = 0, []
    nos = {x for x in (doc.get("ref_no"), doc.get("doc_no")) if x}
    hit = nos & set(deal.get("doc_nos") or ())
    if hit:
        score += WEIGHTS["no_match"]
        reasons.append(f"番号 {sorted(hit)[0]} が一致")
    dnos = {norm_drawing_no(x.get("drawing_no")) for x in (doc.get("lines") or []) + (doc.get("drawings") or []) if x.get("drawing_no")}
    dnos.discard("")
    dhit = dnos & set(deal.get("drawing_nos") or ())
    if dhit:
        score += WEIGHTS["drawing"]
        reasons.append(f"図番 {sorted(dhit)[0]} が一致")
    amt = doc.get("amount")
    targets = [a for a in [deal.get("amount")] + list(deal.get("quote_amounts") or []) if a]
    if amt and targets:
        if any(abs(amt - t) <= max(1, t * 0.01) for t in targets):
            score += WEIGHTS["amount"]
            reasons.append(f"金額 {amt:,}円 が一致")
        elif all(abs(amt - t) > t * 0.3 for t in targets):
            score += WEIGHTS["amount_off"]
            reasons.append("金額が大きく異なる")
    dw = _words(" ".join([doc.get("title") or ""] + [x.get("name") or "" for x in (doc.get("lines") or []) + (doc.get("drawings") or [])]))
    kw = _words(" ".join([deal.get("title") or "", deal.get("site_name") or ""]))
    common = dw & kw
    if common:
        add = min(WEIGHTS["words"], 5 * len(common))
        score += add
        reasons.append("「" + "」「".join(sorted(common)[:2]) + "」が共通")
    if deal.get("stage") in STAGE_FIT.get(doc.get("kind"), set()):
        score += WEIGHTS["stage"]
        reasons.append(f"{deal['stage']}の案件")
    d = _days(doc.get("doc_date"), deal.get("last_doc_date") or deal.get("created_at"))
    if d is not None and d <= 60:
        score += round(WEIGHTS["near"] * (1 - d / 60))
        if d <= 30:
            reasons.append(f"日付が近い（{d}日違い）")
    if open_count == 1:
        score += WEIGHTS["only"]
        reasons.append("この会社で進行中の案件はこの1件だけ")
    return max(0, min(100, score)), reasons


def rank_deals(doc: dict, deals: list) -> list:
    open_count = len([d for d in deals if d.get("is_open")])
    ranked = []
    for d in deals:
        s, why = score_deal(doc, d, open_count)
        if s >= SHOW:
            ranked.append({"deal_id": d["id"], "title": d.get("title"), "stage": d.get("stage"), "score": s, "reasons": why})
    ranked.sort(key=lambda x: -x["score"])
    ranked = ranked[:3]
    pick = None
    if ranked and ranked[0]["score"] >= AUTO_PICK and (len(ranked) == 1 or ranked[0]["score"] - ranked[1]["score"] >= 10):
        pick = ranked[0]["deal_id"]
    return ranked, pick


# ── 営業日 ──
def business_days(start: date, end: date) -> int:
    """start〜end（両端含む）の平日の数"""
    if end < start:
        return 0
    n = 0
    d = start
    from datetime import timedelta
    while d <= end:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n
