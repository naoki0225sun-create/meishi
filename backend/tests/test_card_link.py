"""
名刺と、名刺のない案件（テレアポ業者が登録したものなど）を自動でつなぐテスト
  ・電話・メール/サイトのドメイン・会社名（株式会社・(株)・空白の違いはそろえる）で照らし合わせる
  ・名刺の人が業者の聞いた担当者と別の人でも、同じ会社ならつなぐ
  ・確実な案件が1件だけのときだけ自動。複数なら候補を出すだけ
  ・gmail など共用のドメインでは照らし合わせない
  ・外したら自動でつなぎ直さない。名刺がほかの案件で使われていたらつながない
  ・テナントをまたがない
  ・Slack：自動でつなぐ／候補から「#番号」で選ぶ／「取消」で外す
実行: cd backend && python -m pytest tests -q
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Header  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import cardmatch  # noqa: E402
import sales  # noqa: E402
import slack_bot  # noqa: E402
import vendor  # noqa: E402
import vendorreq  # noqa: E402

TENANTS = {"company_a": {"display_name": "ワコーメタル"}, "company_b": {"display_name": "和宏電機"}}
CARDS: dict = {"company_a": [], "company_b": []}


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(sales, "SALES_DB_PATH", str(tmp_path / "sales.db"))
    sales.init_db()
    vendor.init_db()
    vendorreq.init_db()
    slack_bot.init_db()
    for k in CARDS:
        CARDS[k] = []

    def check_session(x_tenant: str = Header("company_a")):
        return {"tenant_id": x_tenant, "user": "staff"}

    a = FastAPI()
    a.include_router(sales.build_router(check_session, lambda t: CARDS[t], lambda t: TENANTS.get(t),
                                        list_tenants=lambda: TENANTS))
    return a


def cl(app, tenant="company_a"):
    c = TestClient(app)
    c.headers["X-Tenant"] = tenant
    return c


def card(cid, company, name, **kw):
    return {"id": cid, "company": company, "name": name, "email": kw.get("email", ""), "phone": kw.get("phone", ""),
            "mobile": kw.get("mobile", ""), "website": kw.get("website", ""), "department_position": ""}


def vendor_deal(app, tenant, company, contact="", **ct):
    """業者が登録した名刺なしの案件（業者の入力は vendor_contact に入る）"""
    r = cl(app, tenant).post("/api/sales/deals", json={"company": company, "contact": contact or None,
                                                       "phone": ct.get("phone"), "stage": "テレアポ"})
    assert r.status_code == 200, r.text
    did = r.json()["id"]
    with sales._conn() as c:
        c.execute("UPDATE deals SET vendor_contact=? WHERE id=?",
                  (json.dumps({"company": company, "contact": contact, **ct}, ensure_ascii=False), did))
        c.commit()
    return did


def card_of(app, tenant, did):
    return cl(app, tenant).get(f"/api/sales/deals/{did}").json().get("card_id")


@pytest.mark.parametrize("tenant", ["company_a", "company_b"])
def test_phone_match_other_person(app, tenant):
    did = vendor_deal(app, tenant, "株式会社 特殊電装", "池田　様", phone="072-961-7011", web="t-denso.co.jp/company/")
    CARDS[tenant].append(card("c1", "(株)特殊電装", "山田 太郎", phone="072(961)7011"))
    cl(app, tenant).get("/api/sales/deals")
    assert card_of(app, tenant, did) == "c1"
    acts = cl(app, tenant).get(f"/api/sales/deals/{did}").json()["activities"]
    assert any("名刺をつなぎました（自動" in a["title"] for a in acts)


def test_company_name_and_unknown_contact(app):
    did = vendor_deal(app, "company_a", "株式会社かんでんエンジニアリング", "不明")
    CARDS["company_a"].append(card("c1", "株式会社 かんでんエンジニアリング", "佐藤 一郎"))
    assert card_of(app, "company_a", did) == "c1"


def test_domain_match_from_website(app):
    did = vendor_deal(app, "company_a", "大信電興株式会社", "花野", web="https://www.taishin-d.co.jp/")
    CARDS["company_a"].append(card("c1", "大信電興(株) 大阪支店", "鈴木", email="suzuki@taishin-d.co.jp"))
    assert card_of(app, "company_a", did) == "c1"


def test_free_mail_is_not_used(app):
    did = vendor_deal(app, "company_a", "株式会社アルファ", "", email="alpha@gmail.com")
    CARDS["company_a"].append(card("c1", "ベータ工業", "田中", email="tanaka@gmail.com"))
    assert card_of(app, "company_a", did) is None or card_of(app, "company_a", did) == ""


def test_two_deals_same_company_is_not_automatic(app):
    d1 = vendor_deal(app, "company_a", "株式会社リョウセイ", "坂本")
    d2 = vendor_deal(app, "company_a", "株式会社リョウセイ", "木村")
    CARDS["company_a"].append(card("c1", "株式会社リョウセイ", "森"))
    cl(app).get("/api/sales/deals")
    assert not card_of(app, "company_a", d1) and not card_of(app, "company_a", d2)
    res = cl(app).get("/api/sales/cards/c1/deal-candidates").json()
    assert {x["id"] for x in res["candidates"]} == {d1, d2} and res["auto"] is None


def test_same_company_several_cards_uses_contact_name(app):
    did = vendor_deal(app, "company_a", "株式会社リョウセイ", "坂本様")
    CARDS["company_a"] += [card("c1", "株式会社リョウセイ", "森"), card("c2", "株式会社リョウセイ", "坂本")]
    assert card_of(app, "company_a", did) == "c2"


def test_unlink_is_remembered(app):
    did = vendor_deal(app, "company_a", "株式会社 特殊電装", "池田", phone="072-961-7011")
    CARDS["company_a"].append(card("c1", "株式会社特殊電装", "池田", phone="0729617011"))
    assert card_of(app, "company_a", did) == "c1"
    r = cl(app).patch(f"/api/sales/deals/{did}", json={"card_id": ""})
    assert r.status_code == 200, r.text
    cl(app).get("/api/sales/deals")
    assert not card_of(app, "company_a", did)
    # 手で選び直せばつながる
    cl(app).patch(f"/api/sales/deals/{did}", json={"card_id": "c1"})
    assert card_of(app, "company_a", did) == "c1"


def test_card_used_by_another_deal_still_links(app):
    """同じ人の名刺で別の案件があっても、業者の案件にもつなぐ（別件・リピートの引合）"""
    CARDS["company_a"].append(card("c1", "株式会社 加美電子工業", "加田"))
    r = cl(app).post("/api/sales/deals", json={"card_id": "c1", "stage": "アポ"})
    did = vendor_deal(app, "company_a", "加美電子工業株式会社", "加田")
    cl(app).get("/api/sales/deals")
    assert r.json()["id"] != did and card_of(app, "company_a", did) == "c1"


def test_tenants_do_not_mix(app):
    did = vendor_deal(app, "company_a", "株式会社 特殊電装", "池田", phone="072-961-7011")
    CARDS["company_b"].append(card("c1", "株式会社特殊電装", "池田", phone="072-961-7011"))
    cl(app, "company_b").get("/api/sales/deals")
    cl(app, "company_a").get("/api/sales/deals")
    assert not card_of(app, "company_a", did)


def test_candidates_endpoint_auto(app):
    did = vendor_deal(app, "company_a", "株式会社 特殊電装", "池田", phone="072-961-7011")
    vendor_deal(app, "company_a", "株式会社まったく別", "誰か")
    with sales._conn() as c:   # 一覧を開く前（まだ自動でつながっていない）
        res = cardmatch.candidates(c, "company_a", card("c9", "特殊電装", "山田"))
    assert [x["id"] for x in res] == [did] and res[0]["strong"]


# ── Slack ──
class FakeHandler(slack_bot.SlackHandler):
    def __init__(self):
        pass


@pytest.fixture()
def posts(monkeypatch):
    out = []
    monkeypatch.setattr(slack_bot, "post", lambda ch, ts, text: out.append(text) or {})
    return out


def test_slack_auto_link_and_undo(app, posts):
    did = vendor_deal(app, "company_a", "株式会社 特殊電装", "池田", phone="072-961-7011")
    h = FakeHandler()
    cd = card("c1", "株式会社特殊電装", "山田", phone="072-961-7011")
    res = h.link_to_deal("company_a", "C1", "100.1", cd)
    assert res["linked"] and f"#{did}" in res["text"]
    with sales._conn() as c:
        assert c.execute("SELECT card_id FROM deals WHERE id=?", (did,)).fetchone()[0] == "c1"
    h.handle_thread_reply("company_a", "C1", "100.1", "取消")
    with sales._conn() as c:
        row = c.execute("SELECT card_id, company_snapshot FROM deals WHERE id=?", (did,)).fetchone()
    assert not row[0] and row[1] == "株式会社 特殊電装"
    assert "外しました" in posts[-1]
    # 取り消したものは、一覧を開いても自動でつながない
    CARDS["company_a"].append(cd)
    cl(app).get("/api/sales/deals")
    assert not card_of(app, "company_a", did)


def test_slack_choose_by_number(app, posts):
    d1 = vendor_deal(app, "company_a", "株式会社リョウセイ", "坂本")
    d2 = vendor_deal(app, "company_a", "株式会社リョウセイ", "木村")
    h = FakeHandler()
    cd = card("c1", "株式会社リョウセイ", "森")
    res = h.link_to_deal("company_a", "C1", "200.1", cd)
    assert not res.get("linked") and f"#{d1}" in res["text"] and f"#{d2}" in res["text"]
    h.handle_thread_reply("company_a", "C1", "200.1", f"＃{d2}")
    with sales._conn() as c:
        assert c.execute("SELECT card_id FROM deals WHERE id=?", (d2,)).fetchone()[0] == "c1"
        assert not c.execute("SELECT card_id FROM deals WHERE id=?", (d1,)).fetchone()[0]
    assert "つなぎました" in posts[-1]


def test_slack_no_candidates(app, posts):
    vendor_deal(app, "company_a", "株式会社 特殊電装", "池田")
    assert FakeHandler().link_to_deal("company_a", "C1", "300.1", card("c1", "全然違う工業", "山田")) == {}
