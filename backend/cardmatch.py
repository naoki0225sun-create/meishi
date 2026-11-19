"""
名刺と、名刺のない案件（テレアポ業者が登録したものなど）を照らし合わせてつなぐ

  ・照らし合わせるもの：会社名（株式会社・(株)・全角半角・空白の違いはそろえる）、電話・携帯、
    メールのドメイン、公式サイトのドメイン（gmail などの共用ドメインは使わない）
  ・案件側は、こちらの控え・業者の入力（vendor_contact）・テレアポリスト（prospects）の値を使う
  ・確実に一致する進行中の案件が1件だけなら自動でつなぐ。複数・あいまいなら候補として出すだけ
  ・一度外したつながりは card_link_skip に残し、自動でつなぎ直さない
  ・同じ名刺がほかの案件につながっていてもよい（同じ人からの別件・リピート）
  ・テナントをまたいではつながない（必ず tenant_id で絞る）
"""
import json
import re
import unicodedata
from typing import Optional

import sales

FREE_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.co.jp", "yahoo.com", "ymail.ne.jp", "ybb.ne.jp", "hotmail.com",
    "hotmail.co.jp", "outlook.com", "outlook.jp", "live.jp", "live.com", "msn.com", "icloud.com", "me.com",
    "mac.com", "aol.com", "docomo.ne.jp", "ezweb.ne.jp", "au.com", "softbank.ne.jp", "i.softbank.jp",
    "disney.ne.jp", "nifty.com", "biglobe.ne.jp", "ocn.ne.jp", "so-net.ne.jp", "plala.or.jp", "dion.ne.jp",
    "zaq.ne.jp", "eonet.ne.jp", "kcn.ne.jp", "odn.ne.jp", "infoseek.jp", "excite.co.jp", "jcom.home.ne.jp",
    "wakwak.com", "mail.com", "proton.me", "protonmail.com",
}
# 自社グループ（名刺に自社のアドレスが紛れても照合に使わない）
OWN_DOMAINS = {"wakometal.com", "wako-metal.com", "wakou-dk.com", "nonbilarity.com", "w-blueprint.com"}
# 共用のサイト（会社の公式サイトではないもの）
SHARED_SITES = {"facebook.com", "instagram.com", "x.com", "twitter.com", "linkedin.com", "youtube.com",
                "google.com", "goo.gl", "ameblo.jp", "jimdofree.com", "wixsite.com", "studio.site",
                "mapion.co.jp", "ekiten.jp", "baseconnect.in", "ipros.jp", "tsukulink.net"}

REASON_LABEL = {"phone": "電話番号", "domain": "メール・サイトのドメイン", "company": "会社名", "company_like": "会社名が似ている"}


def _n(v) -> str:
    return unicodedata.normalize("NFKC", str(v or "")).strip()


def norm_company(v) -> str:
    n = sales._norm_company(v)
    return "" if n in ("", "不明", "なし", "-", "ー") else n


def norm_phone(v) -> Optional[str]:
    d = re.sub(r"\D", "", _n(v))
    if d.startswith("81") and len(d) >= 11:
        d = "0" + d[2:]
    return d if len(d) >= 9 else None


def _host(v) -> Optional[str]:
    s = _n(v).lower()
    if not s:
        return None
    if "@" in s and "/" not in s:
        h = s.rsplit("@", 1)[1]
    else:
        s = re.sub(r"^[a-z]+://", "", s)
        h = re.split(r"[/?#:]", s, 1)[0]
    h = h.strip(".").removeprefix("www.")
    return h if "." in h else None


def norm_domain(v) -> Optional[str]:
    """メールでもURLでも、会社を表すドメインにする。共用・自社のドメインは None"""
    h = _host(v)
    if not h:
        return None
    for bad in FREE_DOMAINS | OWN_DOMAINS | SHARED_SITES:
        if h == bad or h.endswith("." + bad):
            return None
    return h


def card_keys(card: dict) -> dict:
    return {"company": norm_company(card.get("company")),
            "phones": {p for p in (norm_phone(card.get(k)) for k in ("phone", "mobile")) if p},
            "domains": {d for d in (norm_domain(card.get(k)) for k in ("email", "website")) if d},
            "name": sales._norm_name(card.get("name"))}


def deal_keys(c, tenant_id: str, row) -> dict:
    row = dict(row)
    try:
        ct = json.loads(row.get("vendor_contact") or "{}")
        ct = ct if isinstance(ct, dict) else {}
    except (TypeError, ValueError):
        ct = {}
    companies = [row.get("company_snapshot"), ct.get("company")]
    phones = [row.get("phone_snapshot"), ct.get("phone"), ct.get("mobile")]
    domains = [row.get("email_snapshot"), ct.get("email"), ct.get("web")]
    names = [row.get("contact_snapshot"), ct.get("contact")]
    try:
        for p in c.execute("SELECT company, tel, web, official_url, contact FROM prospects WHERE tenant_id=? AND deal_id=?",
                           (tenant_id, row["id"])).fetchall():
            companies.append(p["company"])
            phones.append(p["tel"])
            domains += [p["web"], p["official_url"]]
            names.append(p["contact"])
    except Exception:
        pass   # テレアポリストの表がない環境
    return {"companies": {x for x in (norm_company(v) for v in companies) if x},
            "phones": {x for x in (norm_phone(v) for v in phones) if x},
            "domains": {x for x in (norm_domain(v) for v in domains) if x},
            "names": {x for x in (sales._norm_name(v) for v in names) if x and x not in ("不明", "なし")}}


def reasons(ck: dict, dk: dict) -> list:
    out = []
    if ck["phones"] & dk["phones"]:
        out.append("phone")
    if ck["domains"] & dk["domains"]:
        out.append("domain")
    co = ck["company"]
    if co and co in dk["companies"]:
        out.append("company")
    elif co and len(co) >= 3 and any(len(k) >= 3 and (co in k or k in co) for k in dk["companies"]):
        out.append("company_like")
    return out


def is_strong(rs: list) -> bool:
    return any(r in ("phone", "domain", "company") for r in rs)


def _skipped(c, tenant_id, deal_id, card_id) -> bool:
    return bool(c.execute("SELECT 1 FROM card_link_skip WHERE tenant_id=? AND deal_id=? AND card_id=?",
                          (tenant_id, deal_id, card_id)).fetchone())


def open_cardless_deals(c, tenant_id: str) -> list:
    closed = sorted(sales.CLOSED_STAGES)
    return c.execute(f"""SELECT * FROM deals WHERE tenant_id=? AND (card_id IS NULL OR card_id='')
        AND stage NOT IN ({','.join('?' * len(closed))}) ORDER BY updated_at DESC""", [tenant_id] + closed).fetchall()


def candidates(c, tenant_id: str, card: dict) -> list:
    """この名刺をつなげそうな、名刺のない進行中の案件。確実なものを先に"""
    ck = card_keys(card)
    if not (ck["company"] or ck["phones"] or ck["domains"]):
        return []
    out = []
    for row in open_cardless_deals(c, tenant_id):
        rs = reasons(ck, deal_keys(c, tenant_id, row))
        if not rs or (card.get("id") and _skipped(c, tenant_id, row["id"], card["id"])):
            continue
        out.append({"id": row["id"], "title": row["title"], "stage": row["stage"], "pipeline": row["pipeline"],
                    "company": row["company_snapshot"], "contact": row["contact_snapshot"],
                    "vendor": bool(row["vendor_id"]), "reasons": rs,
                    "reason_text": "・".join(REASON_LABEL[r] for r in rs), "strong": is_strong(rs)})
    out.sort(key=lambda x: (not x["strong"], -len(x["reasons"])))
    return out


def auto_target(cands: list) -> Optional[dict]:
    strong = [x for x in cands if x["strong"]]
    return strong[0] if len(strong) == 1 else None


def link(c, tenant_id: str, deal_id: int, card: dict, how: str) -> dict:
    """名刺を案件につなぐ。戻すための前の値を返す"""
    cur = dict(sales._get_deal_row(c, tenant_id, deal_id))
    prev = {k: cur.get(k) for k in ("card_id", "company_snapshot", "contact_snapshot", "email_snapshot", "phone_snapshot")}
    sets = {}
    sales.link_card(c, tenant_id, cur, card["id"], {card["id"]: card}, sets, how=how)
    if sets:
        sales.apply_sets(c, tenant_id, deal_id, sets)
    return prev


def remember_skip(c, tenant_id: str, deal_id: int, card_id: str):
    if card_id:
        c.execute("INSERT OR IGNORE INTO card_link_skip (tenant_id, deal_id, card_id, created_at) VALUES (?,?,?,?)",
                  (tenant_id, deal_id, card_id, sales._now()))


# ── 案件側から：名刺のない案件に、あとから登録された名刺を自動でつなぐ（一覧・案件を開いたとき） ──
class CardIndex:
    def __init__(self, cards: list):
        self.cards = cards
        self.by_phone, self.by_domain, self.by_company = {}, {}, {}
        for card in cards:
            k = card_keys(card)
            for p in k["phones"]:
                self.by_phone.setdefault(p, []).append(card)
            for d in k["domains"]:
                self.by_domain.setdefault(d, []).append(card)
            if k["company"]:
                self.by_company.setdefault(k["company"], []).append(card)

    def find(self, dk: dict) -> list:
        found = {}
        for p in dk["phones"]:
            for x in self.by_phone.get(p, []):
                found[x["id"]] = x
        for d in dk["domains"]:
            for x in self.by_domain.get(d, []):
                found[x["id"]] = x
        for co in dk["companies"]:
            for x in self.by_company.get(co, []):
                found[x["id"]] = x
        return list(found.values())


def auto_link_deal(c, tenant_id: str, row, index: CardIndex) -> Optional[dict]:
    """名刺のない進行中の案件に、確実に一致する名刺が1枚だけあればつなぐ。つないだ名刺を返す"""
    row = dict(row)
    if row.get("card_id") or row.get("stage") in sales.CLOSED_STAGES:
        return None
    dk = deal_keys(c, tenant_id, row)
    if not (dk["phones"] or dk["domains"] or dk["companies"]):
        return None
    found = [x for x in index.find(dk) if not _skipped(c, tenant_id, row["id"], x["id"])]
    if not found:
        return None
    if len(found) > 1:
        # 同じ会社の人が何人もいるときは、業者が聞いた担当者と同じ名前の人だけ
        found = [x for x in found if sales._norm_name(x.get("name")) in dk["names"]]
        if len(found) != 1:
            return None
    card = found[0]   # 同じ人の名刺で別の案件があってもよい（リピートや別件の引合もあるため）
    others = [x for x in candidates(c, tenant_id, card) if x["strong"]]
    if len(others) != 1 or others[0]["id"] != row["id"]:
        return None   # この名刺に当てはまる案件がほかにもある
    rs = others[0]["reason_text"]
    link(c, tenant_id, row["id"], card, how=f"自動・{rs}が一致")
    return card
