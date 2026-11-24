"""
「今日やること」の新着（/api/sales/news）のテスト
  ・営業メモ・電話・アポ・メールの記録が出る（Slackからの報告は Slack の印つき）
  ・最近登録された名刺が出る。つながった案件の番号も付く（「名刺をつなぎました」は名刺の行にまとめる）
  ・段階の変更・AIの事前調査などは出さない。古いもの・ほかの会社のものは出さない
  ・after を渡すとその番号より後の記録だけ（名刺は出さない）
実行: cd backend && python -m pytest tests -q
"""
from datetime import datetime, timedelta

from test_card_link import CARDS, app, card, cl, vendor_deal  # noqa: F401
import sales  # noqa: E402


def reg(dt):
    return dt.strftime("%Y/%m/%d %H:%M")


def acts(app, did, *items):
    for kind, title, body in items:
        r = cl(app).post(f"/api/sales/deals/{did}/activities", json={"kind": kind, "title": title, "body": body})
        assert r.status_code == 200, r.text


def test_memo_and_card_appear(app):
    did = vendor_deal(app, "company_a", "三星電機株式会社", "三原")
    acts(app, did, ("memo", "外注先は1社のみ", "制御盤の見積をもらえる約束"))
    with sales._conn() as c:
        sales.log_activity(c, "company_a", did, "memo", "Slackからの報告", f"要点\n\n{sales.SLACK_MARK}\n本文")
        sales.log_activity(c, "company_a", did, "memo", "AIの事前調査（リストより）", "x")
        c.commit()
    c1 = card("c1", "三星電機株式会社", "三原 将経", phone="06-4302-5656")
    c1.update(registered_date=reg(datetime.now()), assigned_to_list=["日野"])
    old = card("c9", "古い会社", "古川")
    old.update(registered_date=reg(datetime.now() - timedelta(days=20)))
    CARDS["company_a"] += [c1, old]
    cl(app).get("/api/sales/deals")   # 名刺が自動でつながる
    items = cl(app).get("/api/sales/news").json()["items"]
    keys = {i["type"] for i in items}
    cards = [i for i in items if i["type"] == "card"]
    assert [x["card_id"] for x in cards] == ["c1"]
    assert cards[0]["deal_ids"] == [did] and cards[0]["owners"] == ["日野"]
    assert "link" not in keys   # 名刺の行にまとめる
    titles = [i["title"] for i in items if i["type"] == "act"]
    assert "外注先は1社のみ" in titles and "Slackからの報告" in titles
    assert not any(t.startswith("AIの事前調査") for t in titles)
    sl = next(i for i in items if i["title"] == "Slackからの報告")
    assert sl["source"] == "slack" and sl["snippet"] == "要点"
    assert any(i["type"] == "deal" for i in items)   # 案件作成


def test_after_and_tenant(app):
    did = vendor_deal(app, "company_a", "加美電子工業株式会社")
    data = cl(app).get("/api/sales/news").json()
    mx = data["max_activity_id"]
    acts(app, did, ("call", "電話した", "不在"))
    CARDS["company_a"].append(dict(card("c2", "新しい会社", "新田"), registered_date=reg(datetime.now())))
    after = cl(app).get(f"/api/sales/news?after={mx}").json()["items"]
    assert [i["title"] for i in after] == ["電話した"]
    assert cl(app, "company_b").get("/api/sales/news").json()["items"] == []


def test_stage_change_not_in_news(app):
    did = vendor_deal(app, "company_a", "大洋マシナリー")
    with sales._conn() as c:
        sales.log_activity(c, "company_a", did, "stage", "ステージ変更: テレアポ → アポ")
        c.commit()
    items = cl(app).get("/api/sales/news").json()["items"]
    assert not any(i["title"].startswith("ステージ変更") for i in items)


def test_parse_reg_date():
    assert sales.parse_reg_date("2026/11/24 11:02") == datetime(2026, 11, 24, 11, 2)
    assert sales.parse_reg_date("2026-11-24") == datetime(2026, 11, 24)
    assert sales.parse_reg_date("2026年11月24日") == datetime(2026, 11, 24)
    assert sales.parse_reg_date("") is None and sales.parse_reg_date("不明") is None
