"""
テレアポ業者の入力が、こちらの案件詳細にそのまま出ることのテスト
  ・業者が入れた会社名・部署・担当者・電話・メール・住所・公式サイト・架電日時・アポ・メモがすべて出る
  ・業者があとで直すと、こちらにもすぐ出て「業者の更新あり」が付く（開くと消える）
  ・名刺と食い違うときは両方を並べる（上書きしない）
  ・「なし」「不明」「—」は空欄扱い
  ・ワコーメタル（company_a）・和宏電機（company_b）の両方で同じ
実行: cd backend && python -m pytest tests -q
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["PARTNER_COOKIE_SECURE"] = "0"

from fastapi import FastAPI, Header  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import sales  # noqa: E402
import vendor  # noqa: E402
import vendorreq  # noqa: E402

TENANTS = {"company_a": {"display_name": "ワコーメタル"}, "company_b": {"display_name": "和宏電機"}}
CARDS: dict = {"company_a": [], "company_b": []}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(sales, "SALES_DB_PATH", str(tmp_path / "sales.db"))
    sales.init_db()
    vendor.init_db()
    vendorreq.init_db()
    for k in CARDS:
        CARDS[k] = []

    def check_session(x_tenant: str = Header("company_a")):
        return {"tenant_id": x_tenant, "user": "staff"}

    app = FastAPI()
    sr = sales.build_router(check_session, lambda t: CARDS[t], lambda t: TENANTS.get(t), list_tenants=lambda: TENANTS)
    app.include_router(sr)
    admin, partner = vendor.build_router(check_session, lambda t: TENANTS.get(t))
    app.include_router(admin)
    app.include_router(partner)
    va, vp = vendorreq.build_router(check_session, vendor.current_vendor, vendor._fire)
    app.include_router(va)
    app.include_router(vp)
    return app


def staff(app, tenant):
    c = TestClient(app)
    c.headers["X-Tenant"] = tenant
    return c


def new_vendor(app, tenant, name="まつむらさん"):
    s = staff(app, tenant)
    v = s.post("/api/sales/vendors", json={"name": name}).json()
    p = TestClient(app)
    r = p.post("/api/partner/login", json={"k": v["slug"], "password": v["password"]})
    assert r.status_code == 200, r.text
    return s, p


TOKUSHU = {"company": "株式会社 特殊電装", "contact": "池田　様", "department": "なし", "phone": "072-961-7011",
           "zip": "578-0982", "address": "大阪府東大阪市吉田本町3-10-11", "web": "t-denso.co.jp/company/",
           "telapo_at": "2026-11-13T16:37", "result": "資料送付",
           "memo": "板金加工は外注先に依頼されています。\n会社案内送付お願いしますとのことです。",
           "docs": {"to": "池田 様", "method": "郵送", "what": "会社案内・名刺"}}


def fields(entry):
    return {f["key"]: f for f in entry["fields"]}


@pytest.mark.parametrize("tenant", ["company_a", "company_b"])
def test_everything_the_vendor_entered_is_on_the_deal(env, tenant):
    s, p = new_vendor(env, tenant)
    deal_id = p.post("/api/partner/deals", json=TOKUSHU).json()["id"]
    d = s.get(f"/api/sales/deals/{deal_id}").json()
    e = d["vendor_entry"]
    assert e and e["vendor_name"] == "まつむらさん" and e["owned"]
    f = fields(e)
    assert f["company"]["value"] == "株式会社 特殊電装"
    assert f["contact"]["value"] == "池田　様"
    assert f["phone"]["value"] == "072-961-7011"
    assert f["zip"]["value"] == "578-0982"
    assert f["address"]["value"] == "大阪府東大阪市吉田本町3-10-11"
    assert f["web"]["value"] == "https://t-denso.co.jp/company/"
    assert "department" not in f   # 「なし」は空欄
    assert e["telapo_at"] == "2026-11-13T16:37"
    assert len(e["calls"]) == 1
    call = e["calls"][0]
    assert call["result"] == "資料送付" and call["occurred_at"].startswith("2026-11-13T16:37")
    assert "板金加工は外注先" in call["body"] and "なし" not in call["body"]
    # 業者側で見える項目がこちらにもすべてある
    v = p.get(f"/api/partner/deals/{deal_id}").json()
    for k in ("company", "contact", "phone", "email", "zip", "address", "web", "industry", "fax", "mobile", "department"):
        if v.get(k):
            assert f[k]["value"] == v[k], k
    assert v["department"] is None   # 業者の画面でも「なし 池田 様」にしない
    assert e["telapo_at"] == v["telapo_at"] and e["appo_at"] == v["appo_at"]
    mine = [a for a in v["activities"] if a["mine"]]
    assert {a["id"] for a in mine} <= {c["id"] for c in e["calls"]}
    # 依頼（資料送付）とそのメモはテレアポ業者とのやりとりに出る
    rq = s.get(f"/api/sales/deals/{deal_id}/vendor-requests").json()
    assert rq["requests"] and "板金加工" in rq["requests"][0]["events"][0]["body"]


@pytest.mark.parametrize("tenant", ["company_a", "company_b"])
def test_vendor_edit_shows_up_and_marks_updated(env, tenant):
    s, p = new_vendor(env, tenant)
    deal_id = p.post("/api/partner/deals", json={"company": "大洋マシナリー", "contact": "倉成", "phone": "06-6556-1601",
                                                 "result": "アポ獲得", "appo_at": "2026-11-17T13:00"}).json()["id"]
    lst = {x["id"]: x for x in s.get("/api/sales/deals").json()["deals"]}
    assert lst[deal_id]["vendor_touched_at"]
    first = s.get(f"/api/sales/deals/{deal_id}").json()["vendor_entry"]
    assert first["new"] and first["appo_at"] == "2026-11-17T13:00"
    assert s.get(f"/api/sales/deals/{deal_id}").json()["vendor_entry"]["new"] is False   # 開いたら消える

    import time
    time.sleep(1.1)
    r = p.put(f"/api/partner/deals/{deal_id}/contact", json={"address": "大阪府大阪市大正区鶴町4-1-7", "zip": "551-0023",
                                                              "email": "k@example.co.jp", "department": "購買部"})
    assert r.status_code == 200
    lst = {x["id"]: x for x in s.get("/api/sales/deals").json()["deals"]}
    assert lst[deal_id]["vendor_touched_at"] > lst[deal_id]["vendor_staff_seen_at"]   # 一覧に「業者の更新あり」
    e = s.get(f"/api/sales/deals/{deal_id}").json()["vendor_entry"]
    assert e["new"] and set(e["changed"]) == {"address", "zip", "email", "department"}
    f = fields(e)
    assert f["address"]["value"] == "大阪府大阪市大正区鶴町4-1-7" and f["email"]["value"] == "k@example.co.jp"
    assert f["department"]["value"] == "購買部"
    fix = [c for c in e["calls"] if c["kind"] == "memo"]
    assert fix and "住所: （空欄） → 大阪府大阪市大正区鶴町4-1-7" in fix[0]["body"]
    assert s.get(f"/api/sales/deals/{deal_id}").json()["vendor_entry"]["new"] is False

    # 電話の記録を足しても「更新あり」
    time.sleep(1.1)
    assert p.post(f"/api/partner/deals/{deal_id}/calls", json={"memo": "日程の確認", "telapo_at": "2026-11-14T10:00"}).status_code == 200
    e = s.get(f"/api/sales/deals/{deal_id}").json()["vendor_entry"]
    assert e["new"] and e["changed"] == ["call"] and any(c["body"] == "日程の確認" for c in e["calls"])
    assert e["telapo_at"] == "2026-11-14T10:00"


@pytest.mark.parametrize("tenant", ["company_a", "company_b"])
def test_card_conflict_shows_both_and_overwrites_nothing(env, tenant):
    s, p = new_vendor(env, tenant)
    deal_id = p.post("/api/partner/deals", json={"company": "加美電子工業株式会社", "contact": "加田 様", "department": "購買部",
                                                 "phone": "06-6791-8151", "email": "kada@kamidenshi.co.jp"}).json()["id"]
    CARDS[tenant] = [{"id": "card1", "company": "加美電子工業(株)", "name": "加田 花子", "department_position": "資材部",
                      "phone": "06-6791-8151", "email": "kada@kamidenshi.co.jp", "website": "", "address": ""}]
    with sales._conn() as c:
        c.execute("UPDATE deals SET card_id='card1' WHERE id=?", (deal_id,))
        c.commit()
    e = s.get(f"/api/sales/deals/{deal_id}").json()["vendor_entry"]
    f = fields(e)
    assert "ours" not in f["company"] and "ours" not in f["phone"] and "ours" not in f["email"]   # 表記ゆれは同じ扱い
    assert f["department"]["value"] == "購買部" and f["department"]["ours"] == "資材部" and f["department"]["ours_label"] == "名刺"
    assert f["contact"]["value"] == "加田 様" and f["contact"]["ours"] == "加田 花子"
    assert e["conflicts"] == 2
    # 業者が直しても名刺の情報・案件の控えは変わらない
    p.put(f"/api/partner/deals/{deal_id}/contact", json={"contact": "加田 雅子 様"})
    with sales._conn() as c:
        row = c.execute("SELECT contact_snapshot FROM deals WHERE id=?", (deal_id,)).fetchone()
    assert row["contact_snapshot"] == "加田 様"
    assert CARDS[tenant][0]["name"] == "加田 花子"
    assert fields(s.get(f"/api/sales/deals/{deal_id}").json()["vendor_entry"])["contact"]["value"] == "加田 雅子 様"


def test_tenants_do_not_see_each_other(env):
    sa, pa = new_vendor(env, "company_a")
    sb, _ = new_vendor(env, "company_b")
    deal_id = pa.post("/api/partner/deals", json={"company": "A社だけ", "phone": "06-0000-0000"}).json()["id"]
    assert sb.get(f"/api/sales/deals/{deal_id}").status_code == 404


@pytest.mark.parametrize("v,out", [("なし", None), ("不明", None), ("—", None), ("-", None), ("ー", None), (" なし。", None),
                                   ("特になし", None), ("", None), (None, None), ("池田　様", "池田　様"), ("購買部", "購買部"),
                                   ("なしの木工業", "なしの木工業")])
def test_blank(v, out):
    assert vendor.blank(v) == out


def test_contact_line_and_body_cleaning():
    assert vendor.contact_line("なし", "池田 様") == "池田 様"
    assert vendor.contact_line("不明", "不明") == ""
    assert vendor.contact_line("購買", "堀内") == "購買 堀内"
    assert vendor._clean_body("部署・役職: なし\n板金は外注") == "板金は外注"
    assert vendor._clean_body("相手: なし 池田　様\nメモ") == "相手: 池田 様\nメモ"
    assert vendor._clean_body("相手: 不明\nメモ") == "メモ"


def test_backfill_for_existing_vendor_deals(env):
    """移行：いま入っている業者の案件（リストから作った案件・依頼だけの案件も）がこちらに出る。印は付けない"""
    s, p = new_vendor(env, "company_a")
    with sales._conn() as c:
        vid = c.execute("SELECT id FROM vendors").fetchone()["id"]
        card = {"id": "", "company": "日本電機産業（株）", "name": "堀内", "email": None, "phone": "06-6341-5331"}
        deal_id = sales.insert_deal(c, "company_a", card, "日本電機産業（株） テレアポ", "テレアポ", None, None, None, None,
                                    created_note="案件作成（リストから）", pipeline="sales", owner=None)
        c.execute("UPDATE deals SET vendor_id=?, vendor_contact=?, telapo_at='2025-03-04T10:00', appo_at='2025-03-04T10:00' WHERE id=?",
                  (vid, json.dumps({"company": "日本電機産業（株）", "contact": "堀内", "department": "購買", "phone": "06-6341-5331",
                                    "email": None, "title": "x"}, ensure_ascii=False), deal_id))
        act = sales.log_activity(c, "company_a", deal_id, "call", "テレアポ（まつむらさん）：アポ獲得", "相手: 購買 堀内\n堺工場 購買堀内様アポ",
                                 "2025-03-04T10:00:00")
        c.execute("UPDATE activities SET vendor_id=?, vendor_visible=1 WHERE id=?", (vid, act))
        # 依頼しただけの案件（業者の電話の記録あり）
        card2 = {"id": "", "company": "株式会社 不二鉄工所", "name": "横田 航平", "email": None, "phone": "072-892-3144"}
        req_deal = sales.insert_deal(c, "company_a", card2, "見積", "ヒアリング", None, None, None, None, pipeline="sales", owner=None)
        act2 = sales.log_activity(c, "company_a", req_deal, "call", "再テレアポ（まつむらさん）：再コール", "不在でした")
        c.execute("UPDATE activities SET vendor_id=?, vendor_visible=1 WHERE id=?", (vid, act2))
        c.execute("UPDATE deals SET vendor_touched_at=NULL, vendor_staff_seen_at=NULL")
        c.commit()
    vendor.init_db()   # 起動時の移行
    e = s.get(f"/api/sales/deals/{deal_id}").json()["vendor_entry"]
    assert not e["new"]
    f = fields(e)
    assert f["department"]["value"] == "購買" and f["contact"]["value"] == "堀内" and e["appo_at"] == "2025-03-04T10:00"
    assert e["calls"][0]["result"] == "アポ獲得"
    e2 = s.get(f"/api/sales/deals/{req_deal}").json()["vendor_entry"]
    assert e2 and not e2["owned"] and e2["vendor_name"] == "まつむらさん" and e2["calls"][0]["body"] == "不在でした"
    # 業者と関係ない案件には出さない
    with sales._conn() as c:
        plain = sales.insert_deal(c, "company_a", {"id": "", "company": "X", "name": "Y", "email": None, "phone": None},
                                  "普通の案件", "ヒアリング", None, None, None, None, pipeline="sales", owner=None)
        c.commit()
    assert s.get(f"/api/sales/deals/{plain}").json()["vendor_entry"] is None
