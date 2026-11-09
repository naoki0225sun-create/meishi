"""
テレアポ業者とのやりとり（依頼）
  ・再テレアポ: こちらが案件（営業にある会社）から業者に頼む。用件（再訪問の連絡・お仕事ないですか・現状の確認…）を選び、
    会社名・担当者・電話番号を業者に渡す → 業者が電話して結果とコメントを記録 → こちらに届く
  ・資料送付: 業者が電話で「資料を送って」と言われたら記録（こちらから始めることもできる）
    → こちらが送ったら「送付しました」 → 業者が1週間後ぐらいに「届きましたか？」の電話 → 記録
  ・どちらもコメントを書き合える
  ・相手側の新しい動きは「新着」。一覧の一番上に出て、「確認しました」で通常の並びに戻る

業者が見られるのは「依頼された会社」の、こちらが渡した会社名・担当者・電話番号と、その依頼のやりとりだけ
（金額・名刺・社内の記録・段階は見えない。もともと業者が記録した案件は今まで通り）
"""
import json
import logging
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import sales

log = logging.getLogger("vendorreq")

PURPOSES = ["再訪問の連絡", "お仕事ないですかの連絡", "現状の確認", "その他"]
RECALL_RESULTS = ["アポ獲得", "引き合いあり", "資料送付", "話せた（今は特になし）", "再コール", "担当者不在", "見込みなし", "その他"]
ARRIVAL_RESULTS = ["届いていた・興味あり", "届いていた・今は不要", "アポ獲得", "届いていない", "担当者不在", "その他"]
DOC_METHODS = ["メール", "郵送", "FAX", "どれでも"]
KEEP_OPEN = {"再コール", "担当者不在"}          # 再テレアポ: まだ続く
STATUS_LABEL = {
    ("recall", "open"): "電話待ち", ("recall", "done"): "電話済み", ("recall", "cancelled"): "取り消し",
    ("docs", "requested"): "送付待ち", ("docs", "sent"): "送付済み・到着確認待ち", ("docs", "done"): "到着確認済み",
    ("docs", "cancelled"): "取り消し",
}
FOLLOW_DAYS = 7
SEND_DOCS = "資料を送る"
DAILY_EVENT_LIMIT = 500
EVENTS_PER_REQUEST = 200


def init_db():
    with sales._conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS vendor_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            deal_id INTEGER NOT NULL,
            vendor_id INTEGER NOT NULL,
            kind TEXT NOT NULL,                -- recall / docs
            purpose TEXT,
            status TEXT NOT NULL,
            share TEXT,                        -- 業者に渡す会社名・担当者・電話（JSON）
            docs TEXT,                         -- 資料の送り先・方法・内容（JSON）
            due_on TEXT,                       -- いつまでに（再テレアポ）
            follow_on TEXT,                    -- 到着確認の電話の目安（資料）
            created_by TEXT NOT NULL,          -- staff / vendor
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            staff_seen INTEGER NOT NULL DEFAULT 0,
            vendor_seen INTEGER NOT NULL DEFAULT 0
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS vendor_request_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL,
            tenant_id TEXT NOT NULL,
            actor TEXT NOT NULL,               -- staff / vendor
            action TEXT NOT NULL,              -- create / report / sent / arrival / comment / cancel / reopen
            result TEXT,
            body TEXT,
            at TEXT,                           -- 電話した日時など
            created_at TEXT NOT NULL
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_vreq_deal ON vendor_requests(tenant_id, deal_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_vreq_vendor ON vendor_requests(tenant_id, vendor_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_vreq_ev ON vendor_request_events(request_id)")
        c.commit()


# ── 共通 ──
def _j(v) -> dict:
    try:
        x = json.loads(v or "{}")
        return x if isinstance(x, dict) else {}
    except (TypeError, ValueError):
        return {}


def _event(c, req: dict, actor: str, action: str, result=None, body=None, at=None) -> int:
    n = c.execute("SELECT COUNT(*) n FROM vendor_request_events WHERE request_id=?", (req["id"],)).fetchone()["n"]
    if n >= EVENTS_PER_REQUEST:
        raise HTTPException(400, "この依頼のやりとりが多すぎます。新しく依頼し直してください")
    cur = c.execute("""INSERT INTO vendor_request_events (request_id, tenant_id, actor, action, result, body, at, created_at)
        VALUES (?,?,?,?,?,?,?,?)""", (req["id"], req["tenant_id"], actor, action, result, body, at, sales._now()))
    eid = cur.lastrowid
    # 自分の書き込みは自分にとって新着ではない（＝見たことにする）
    col = "staff_seen" if actor == "staff" else "vendor_seen"
    c.execute(f"UPDATE vendor_requests SET {col}=?, updated_at=? WHERE id=?", (eid, sales._now(), req["id"]))
    return eid


def _clean_share(s) -> dict:
    s = s or {}
    get = (lambda k: getattr(s, k, None)) if not isinstance(s, dict) else s.get
    return {"company": sales._clean_text(get("company"), 120, "会社名"),
            "contact": sales._clean_text(get("contact"), 60, "担当者"),
            "department": sales._clean_text(get("department"), 80, "部署・役職"),
            "phone": sales._clean_text(get("phone"), 40, "電話")}


def _clean_docs(s) -> dict:
    s = s or {}
    get = (lambda k: getattr(s, k, None)) if not isinstance(s, dict) else s.get
    m = sales._clean_text(get("method"), 10, "送り方")
    if m and m not in DOC_METHODS:
        raise HTTPException(400, "送り方が不正です")
    return {"to": sales._clean_text(get("to"), 80, "宛先"), "method": m,
            "address": sales._clean_text(get("address"), 200, "送り先"),
            "what": sales._clean_text(get("what"), 500, "資料の内容")}


def view(c, req, side: str, vendor_names: Optional[dict] = None) -> dict:
    """side=staff / vendor。相手の新しい書き込みに new を付ける"""
    seen = req["staff_seen"] if side == "staff" else req["vendor_seen"]
    other = "vendor" if side == "staff" else "staff"
    evs = []
    for e in c.execute("SELECT * FROM vendor_request_events WHERE request_id=? ORDER BY id", (req["id"],)):
        evs.append({"id": e["id"], "actor": e["actor"], "action": e["action"], "result": e["result"], "body": e["body"],
                    "at": e["at"], "created_at": e["created_at"], "new": e["actor"] == other and e["id"] > seen})
    out = {"id": req["id"], "deal_id": req["deal_id"], "kind": req["kind"], "purpose": req["purpose"], "status": req["status"],
           "status_label": STATUS_LABEL.get((req["kind"], req["status"]), req["status"]),
           "share": _j(req["share"]), "docs": _j(req["docs"]), "due_on": req["due_on"], "follow_on": req["follow_on"],
           "created_by": req["created_by"], "created_at": req["created_at"], "updated_at": req["updated_at"],
           "events": evs, "new": any(e["new"] for e in evs), "todo": todo_of(req, side)}
    if side == "staff":
        out["vendor_id"] = req["vendor_id"]
        out["vendor_name"] = (vendor_names or {}).get(req["vendor_id"])
    return out


def todo_of(req, side: str) -> Optional[str]:
    """その側がやること（なければ None）"""
    k, s = req["kind"], req["status"]
    if side == "vendor":
        if k == "recall" and s == "open":
            return "電話する"
        if k == "docs" and s == "sent":
            return "到着確認の電話"
    else:
        if k == "docs" and s == "requested":
            return "資料を送る"
    return None


def _apply_result(c, req, vendor: dict, result: Optional[str], memo: Optional[str], appo: Optional[str],
                  nxt: Optional[str], tel_at: Optional[str], label: str):
    """業者の電話の結果を案件の記録に残す（アポならアポ日時も）"""
    t = req["tenant_id"]
    row = c.execute("SELECT * FROM deals WHERE id=? AND tenant_id=?", (req["deal_id"], t)).fetchone()
    if not row:
        raise HTTPException(404, "案件が見つかりません")
    sets = {}
    lin = sales.PIPELINES["sales"]["linear"]
    target = row["id"]
    if appo:
        sets["appo_at"] = appo
        sets["next_action"] = f"アポ訪問（{vendor['name']}経由）"
        sets["next_action_due"] = appo[:10]
        st = row["stage"]
        is_sales = (row["pipeline"] or "sales") == "sales"
        if is_sales and st == sales.PIPELINES["sales"]["won"]:
            # 受注済みのお客様からまたアポ → 受注した案件はそのままにして、新しい案件を作る
            card = None if not row["card_id"] else {"id": row["card_id"], "company": row["company_snapshot"],
                                                     "name": row["contact_snapshot"], "email": row["email_snapshot"],
                                                     "phone": row["phone_snapshot"]}
            if card is None:
                card = {"id": "", "company": row["company_snapshot"], "name": row["contact_snapshot"],
                        "email": row["email_snapshot"], "phone": row["phone_snapshot"]}
            target = sales.insert_deal(c, t, card, f"{row['company_snapshot'] or ''} 新しい引合".strip(), "アポ",
                                       created_note=f"案件作成（{vendor['name']}の再テレアポでアポ・#{row['id']}の続き）",
                                       pipeline="sales", owner=row["owner"])
            sales.log_activity(c, t, row["id"], "memo", f"{vendor['name']}の再テレアポでアポ → 新しい案件 #{target} を作りました")
        elif is_sales and (st in (sales.PIPELINES["sales"]["lost"], "保留") or (st in lin and lin.index(st) < lin.index("アポ"))):
            # 失注・保留の会社でもアポが取れたら、案件を動かし直す（終わった案件のままだと「今日」に出ない）
            sales.change_stage(c, t, row["id"], st, "アポ", sets, pipeline="sales")
    elif nxt and row["stage"] not in sales.CLOSED_STAGES:
        sets["next_action"] = f"再コール（{vendor['name']}）"
        sets["next_action_due"] = nxt
    title = f"{label}（{vendor['name']}）" + (f"：{result}" if result else "")
    act = sales.log_activity(c, t, target, "call", title, memo,
                             (tel_at + ":00") if tel_at and len(tel_at) == 16 else None)
    c.execute("UPDATE activities SET vendor_id=?, vendor_visible=1 WHERE id=?", (vendor["id"], act))
    if sets:
        sales.apply_sets(c, t, target, sets)
    return title


def create_docs(c, tenant_id: str, deal_id: int, vendor: dict, docs: dict, share: dict, memo: Optional[str], tel_at=None) -> int:
    """業者が「資料を送って」と言われた → 送付待ちの依頼を作る"""
    now = sales._now()
    cur = c.execute("""INSERT INTO vendor_requests (tenant_id, deal_id, vendor_id, kind, purpose, status, share, docs,
        created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (tenant_id, deal_id, vendor["id"], "docs", "資料送付", "requested", json.dumps(share, ensure_ascii=False),
                     json.dumps(docs, ensure_ascii=False), "vendor", now, now))
    req = {"id": cur.lastrowid, "tenant_id": tenant_id}
    _event(c, req, "vendor", "create", None, memo, tel_at)
    return cur.lastrowid


def docs_from_body(body) -> dict:
    d = getattr(body, "docs", None)
    return _clean_docs(d)


def deal_summary(c, tenant_id: str, deal_id: Optional[int] = None) -> dict:
    """こちらの案件一覧に付ける札（deal_id → {new, labels}）"""
    out = {}
    rows = c.execute("""SELECT r.*, (SELECT MAX(e.id) FROM vendor_request_events e WHERE e.request_id=r.id AND e.actor='vendor') AS vmax
        FROM vendor_requests r WHERE r.tenant_id=? AND r.status<>'cancelled'""" + (" AND r.deal_id=?" if deal_id else ""),
                     (tenant_id, deal_id) if deal_id else (tenant_id,)).fetchall()
    for r in rows:
        x = out.setdefault(r["deal_id"], {"new": False, "labels": [], "todo": False, "active": 0})
        if r["vmax"] and r["vmax"] > r["staff_seen"]:
            x["new"] = True
        lab = None
        if r["kind"] == "recall" and r["status"] == "open":
            lab = "再テレアポ依頼中"
        elif r["kind"] == "docs" and r["status"] == "requested":
            lab = "資料送付待ち"; x["todo"] = True
        elif r["kind"] == "docs" and r["status"] == "sent":
            lab = "到着確認待ち"
        if lab:
            x["active"] += 1
            if lab not in x["labels"]:
                x["labels"].append(lab)
    return out


def vendor_deal_ids(c, vendor: dict) -> dict:
    """業者が依頼を受けている案件（deal_id → [依頼]）"""
    out = {}
    for r in c.execute("""SELECT r.* FROM vendor_requests r JOIN deals d ON d.id=r.deal_id AND d.tenant_id=r.tenant_id
            WHERE r.tenant_id=? AND r.vendor_id=? AND r.status<>'cancelled' ORDER BY r.id""", (vendor["tenant_id"], vendor["id"])):
        out.setdefault(r["deal_id"], []).append(r)
    return out


def vendor_list_info(c, vendor: dict, deal_id: int, reqs: list) -> dict:
    """業者の一覧の1行に付ける情報"""
    news, todos, labels = False, [], []
    for r in reqs:
        vmax = c.execute("SELECT MAX(id) m FROM vendor_request_events WHERE request_id=? AND actor='staff'", (r["id"],)).fetchone()["m"]
        if vmax and vmax > r["vendor_seen"]:
            news = True
        td = todo_of(r, "vendor")
        if td:
            todos.append({"todo": td, "purpose": r["purpose"], "due_on": r["due_on"] if r["kind"] == "recall" else r["follow_on"]})
        lab = (r["purpose"] if r["kind"] == "recall" else "資料送付") + "・" + STATUS_LABEL.get((r["kind"], r["status"]), "")
        labels.append(lab)
    return {"req_new": news, "req_todo": todos, "req_labels": labels[-2:]}


def mark_seen(c, tenant_id: str, deal_id: int, side: str, vendor_id: Optional[int] = None):
    col = "staff_seen" if side == "staff" else "vendor_seen"
    other = "vendor" if side == "staff" else "staff"
    q = "SELECT id FROM vendor_requests WHERE tenant_id=? AND deal_id=?"
    args = [tenant_id, deal_id]
    if vendor_id is not None:
        q += " AND vendor_id=?"; args.append(vendor_id)
    for r in c.execute(q, args).fetchall():
        m = c.execute("SELECT MAX(id) m FROM vendor_request_events WHERE request_id=? AND actor=?", (r["id"], other)).fetchone()["m"]
        if m:
            c.execute(f"UPDATE vendor_requests SET {col}=MAX({col}, ?) WHERE id=?", (m, r["id"]))


# ── 入力 ──
class ShareIn(BaseModel):
    company: Optional[str] = None
    contact: Optional[str] = None
    department: Optional[str] = None
    phone: Optional[str] = None


class DocsIn(BaseModel):
    to: Optional[str] = None
    method: Optional[str] = None
    address: Optional[str] = None
    what: Optional[str] = None


class RequestIn(BaseModel):
    vendor_id: int
    kind: str = "recall"               # recall / docs（こちらで送った → 到着確認を頼む）
    purpose: Optional[str] = None
    note: Optional[str] = None
    due_on: Optional[str] = None
    share: Optional[ShareIn] = None
    docs: Optional[DocsIn] = None


class StaffEventIn(BaseModel):
    action: str                        # comment / sent / cancel / reopen
    body: Optional[str] = None
    method: Optional[str] = None


class VendorEventIn(BaseModel):
    action: str                        # report / arrival / comment
    result: Optional[str] = None
    body: Optional[str] = None
    at: Optional[str] = None           # 電話した日時
    appo_at: Optional[str] = None
    next_call_on: Optional[str] = None
    docs: Optional[DocsIn] = None      # 再テレアポで「資料送付」になったとき


def create_request(c, t: str, row, body: "RequestIn", note: Optional[str], due: Optional[str], share: dict) -> int:
    """こちらから業者に頼む（再テレアポ・資料の到着確認）。トランザクションの中で呼ぶ（commit しない）"""
    deal_id = row["id"]
    v = c.execute("SELECT * FROM vendors WHERE id=? AND tenant_id=? AND active=1", (body.vendor_id, t)).fetchone()
    if not v:
        raise HTTPException(400, "業者を選んでください")
    share["company"] = share["company"] or row["company_snapshot"]
    if not share["company"]:
        raise HTTPException(400, "会社名を入れてください")
    if body.kind == "recall":
        purpose = sales._clean_text(body.purpose, 40, "用件")
        if purpose not in PURPOSES:
            raise HTTPException(400, "用件を選んでください")
        if purpose == "その他" and not note:
            raise HTTPException(400, "「その他」のときはメモに用件を書いてください")
        if not share["phone"]:
            raise HTTPException(400, "電話番号を入れてください（業者がかける番号です）")
        if c.execute("""SELECT 1 FROM vendor_requests WHERE tenant_id=? AND deal_id=? AND vendor_id=? AND kind='recall'
                AND status='open'""", (t, deal_id, v["id"])).fetchone():
            raise HTTPException(409, f"{v['name']}にはこの会社の再テレアポをもう頼んでいます")
        status, docs, follow = "open", {}, None
    else:
        purpose = "資料送付"
        docs = _clean_docs(body.docs)
        status = "sent"
        follow = (date.today() + timedelta(days=FOLLOW_DAYS)).isoformat()
        due = None
    n = c.execute("SELECT COUNT(*) n FROM vendor_requests WHERE tenant_id=? AND deal_id=?", (t, deal_id)).fetchone()["n"]
    if n >= 100:
        raise HTTPException(400, "この案件の依頼が多すぎます")
    now = sales._now()
    cur = c.execute("""INSERT INTO vendor_requests (tenant_id, deal_id, vendor_id, kind, purpose, status, share, docs, due_on, follow_on,
        created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (t, deal_id, v["id"], body.kind, purpose, status, json.dumps(share, ensure_ascii=False),
                     json.dumps(docs, ensure_ascii=False), due, follow, "staff", now, now))
    req = {"id": cur.lastrowid, "tenant_id": t}
    _event(c, req, "staff", "create" if body.kind == "recall" else "sent", None, note)
    if body.kind == "recall":
        sales.log_activity(c, t, deal_id, "memo", f"再テレアポを依頼（{v['name']}・{purpose}）", note)
    else:
        sales.log_activity(c, t, deal_id, "memo", f"資料を送付（{docs.get('method') or '方法未記入'}）・到着確認を{v['name']}に依頼",
                           "\n".join(x for x in [docs.get("what"), note] if x) or None)
        if row["stage"] not in sales.CLOSED_STAGES and (not row["next_action"] or row["next_action"] == SEND_DOCS):
            sales.apply_sets(c, t, deal_id, {"next_action": f"資料の到着確認の結果を見る（{v['name']}）", "next_action_due": follow})
    return req["id"]


def build_router(check_session: Callable, current_vendor: Callable, fire: Callable) -> tuple:
    admin = APIRouter(prefix="/api/sales")
    partner = APIRouter(prefix="/api/partner")

    def _deal(c, t, deal_id):
        row = c.execute("SELECT * FROM deals WHERE id=? AND tenant_id=?", (deal_id, t)).fetchone()
        if not row:
            raise HTTPException(404, "案件が見つかりません")
        return row

    def _req(c, t, rid):
        r = c.execute("SELECT * FROM vendor_requests WHERE id=? AND tenant_id=?", (rid, t)).fetchone()
        if not r or not c.execute("SELECT 1 FROM deals WHERE id=? AND tenant_id=?", (r["deal_id"], t)).fetchone():
            raise HTTPException(404, "依頼が見つかりません")
        return r

    # ── こちら側 ──
    @admin.get("/vendor-requests/meta")
    def meta(session: dict = Depends(check_session)):
        return {"purposes": PURPOSES, "recall_results": RECALL_RESULTS, "arrival_results": ARRIVAL_RESULTS, "methods": DOC_METHODS}

    @admin.get("/deals/{deal_id}/vendor-requests")
    def list_for_deal(deal_id: int, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with sales._conn() as c:
            _deal(c, t, deal_id)
            vn = sales.vendor_names(c, t)
            reqs = [view(c, r, "staff", vn) for r in c.execute(
                "SELECT * FROM vendor_requests WHERE tenant_id=? AND deal_id=? ORDER BY id DESC", (t, deal_id)).fetchall()]
            vendors = [{"id": v["id"], "name": v["name"]} for v in c.execute(
                "SELECT id, name FROM vendors WHERE tenant_id=? AND active=1 ORDER BY id", (t,))]
        return {"requests": reqs, "new": any(r["new"] for r in reqs), "vendors": vendors}

    @admin.post("/deals/{deal_id}/vendor-requests")
    def create(deal_id: int, body: RequestIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        if body.kind not in ("recall", "docs"):
            raise HTTPException(400, "種類が不正です")
        share = _clean_share(body.share)
        note = sales._clean_text(body.note, 2000, "メモ")
        due = sales._clean_date(body.due_on)
        with sales._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = _deal(c, t, deal_id)
            rid = create_request(c, t, row, body, note, due, share)
            c.commit()
        return {"ok": True, "id": rid}

    @admin.post("/vendor-requests/{rid}/events")
    def staff_event(rid: int, body: StaffEventIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        text = sales._clean_text(body.body, 2000, "コメント")
        with sales._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            r = _req(c, t, rid)
            vname = sales.vendor_names(c, t).get(r["vendor_id"], "業者")
            if body.action == "comment":
                if not text:
                    raise HTTPException(400, "コメントを入れてください")
                _event(c, r, "staff", "comment", None, text)
            elif body.action == "sent":
                if r["kind"] != "docs" or r["status"] not in ("requested", "sent"):
                    raise HTTPException(400, "この依頼は送付済みにできません")
                m = sales._clean_text(body.method, 10, "送り方")
                if m and m not in DOC_METHODS:
                    raise HTTPException(400, "送り方が不正です")
                follow = (date.today() + timedelta(days=FOLLOW_DAYS)).isoformat()
                c.execute("UPDATE vendor_requests SET status='sent', follow_on=? WHERE id=?", (follow, rid))
                _event(c, r, "staff", "sent", m, text)
                sales.log_activity(c, t, r["deal_id"], "memo", f"資料を送付（{m or '方法未記入'}）", text)
                # 「資料を送る」は済んだので、次は業者の到着確認の結果を見る
                d = c.execute("SELECT stage, next_action FROM deals WHERE id=?", (r["deal_id"],)).fetchone()
                if d and d["stage"] not in sales.CLOSED_STAGES and (not d["next_action"] or d["next_action"] == SEND_DOCS):
                    sales.apply_sets(c, t, r["deal_id"], {"next_action": f"資料の到着確認の結果を見る（{vname}）", "next_action_due": follow})
            elif body.action == "cancel":
                if r["status"] in ("done", "cancelled"):
                    raise HTTPException(400, "この依頼はもう終わっています")
                c.execute("UPDATE vendor_requests SET status='cancelled' WHERE id=?", (rid,))
                _event(c, r, "staff", "cancel", None, text)
                sales.log_activity(c, t, r["deal_id"], "memo", f"{vname}への依頼を取り消し（{r['purpose']}）", text)
            elif body.action == "reopen":
                if r["kind"] != "recall" or r["status"] != "done":
                    raise HTTPException(400, "電話済みの再テレアポだけ、もう一度頼めます")
                if c.execute("""SELECT 1 FROM vendor_requests WHERE tenant_id=? AND deal_id=? AND vendor_id=? AND kind='recall'
                        AND status='open' AND id<>?""", (t, r["deal_id"], r["vendor_id"], rid)).fetchone():
                    raise HTTPException(409, f"{vname}にはこの会社の再テレアポをもう頼んでいます")
                c.execute("UPDATE vendor_requests SET status='open' WHERE id=?", (rid,))
                _event(c, r, "staff", "reopen", None, text)
            else:
                raise HTTPException(400, "操作が不正です")
            c.commit()
        return {"ok": True}

    @admin.post("/deals/{deal_id}/vendor-requests/seen")
    def staff_seen(deal_id: int, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with sales._conn() as c:
            _deal(c, t, deal_id)
            mark_seen(c, t, deal_id, "staff")
            c.commit()
        return {"ok": True}

    # ── 業者側 ──
    @partner.get("/request-meta")
    def p_meta(vendor: dict = Depends(current_vendor)):
        return {"recall_results": RECALL_RESULTS, "arrival_results": ARRIVAL_RESULTS, "methods": DOC_METHODS}

    @partner.post("/requests/{rid}/events")
    def vendor_event(rid: int, body: VendorEventIn, vendor: dict = Depends(current_vendor)):
        t = vendor["tenant_id"]
        text = sales._clean_text(body.body, 5000, "コメント")
        tel_at = sales._clean_dt(body.at, "電話した日時") or datetime.now().isoformat(timespec="minutes")
        result = sales._clean_text(body.result, 30, "結果")
        appo = sales._clean_dt(body.appo_at, "アポ日時")
        nxt = sales._clean_date(body.next_call_on)
        info = None
        with sales._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            r = c.execute("SELECT * FROM vendor_requests WHERE id=? AND tenant_id=? AND vendor_id=?", (rid, t, vendor["id"])).fetchone()
            if not r or r["status"] == "cancelled" or not c.execute("SELECT 1 FROM deals WHERE id=? AND tenant_id=?", (r["deal_id"], t)).fetchone():
                raise HTTPException(404, "依頼が見つかりません")
            n = c.execute("""SELECT COUNT(*) n FROM vendor_request_events e JOIN vendor_requests r ON r.id=e.request_id
                WHERE r.vendor_id=? AND e.actor='vendor' AND e.created_at >= ?""", (vendor["id"], date.today().isoformat())).fetchone()["n"]
            if n >= DAILY_EVENT_LIMIT:
                raise HTTPException(429, "1日に記録できる件数を超えました")
            company = _j(r["share"]).get("company") or ""
            if body.action == "comment":
                if not text:
                    raise HTTPException(400, "コメントを入れてください")
                _event(c, r, "vendor", "comment", None, text)
                info = {"what": "コメント", "detail": text[:60]}
            elif body.action == "report":
                if r["kind"] != "recall" or r["status"] != "open":
                    raise HTTPException(400, "この依頼は電話待ちではありません")
                if result not in RECALL_RESULTS:
                    raise HTTPException(400, "結果を選んでください")
                if result != "アポ獲得":
                    appo = None
                docs = _clean_docs(body.docs) if result == "資料送付" else None
                _apply_result(c, r, vendor, result, text, appo, nxt if result in KEEP_OPEN else None, tel_at,
                              f"再テレアポ・{r['purpose']}")
                body_txt = text
                if appo:
                    body_txt = "\n".join(x for x in [f"アポ日時: {appo.replace('T', ' ')}", text] if x)
                if nxt and result in KEEP_OPEN:
                    body_txt = "\n".join(x for x in [f"次にかける日: {nxt}", body_txt] if x)
                _event(c, r, "vendor", "report", result, body_txt, tel_at)
                if result not in KEEP_OPEN:
                    c.execute("UPDATE vendor_requests SET status='done' WHERE id=?", (rid,))
                if docs is not None:
                    create_docs(c, t, r["deal_id"], vendor, docs, _j(r["share"]), text, tel_at)
                info = {"what": f"再テレアポの結果（{r['purpose']}）", "detail": result}
            elif body.action == "arrival":
                if r["kind"] != "docs" or r["status"] != "sent":
                    raise HTTPException(400, "この資料はまだ送付済みになっていません")
                if result not in ARRIVAL_RESULTS:
                    raise HTTPException(400, "結果を選んでください")
                if result != "アポ獲得":
                    appo = None
                _apply_result(c, r, vendor, result, text, appo, None, tel_at, "資料の到着確認")
                body_txt = "\n".join(x for x in [f"アポ日時: {appo.replace('T', ' ')}" if appo else None, text] if x) or None
                _event(c, r, "vendor", "arrival", result, body_txt, tel_at)
                if result == "届いていない":
                    c.execute("UPDATE vendor_requests SET status='requested' WHERE id=?", (rid,))
                elif result != "担当者不在":
                    c.execute("UPDATE vendor_requests SET status='done' WHERE id=?", (rid,))
                info = {"what": "資料の到着確認", "detail": result}
            else:
                raise HTTPException(400, "操作が不正です")
            c.commit()
        if info:
            fire({"kind": "request", "tenant_id": t, "vendor": vendor["name"], "deal_id": r["deal_id"], "company": company, **info})
        return {"ok": True}

    @partner.post("/deals/{deal_id}/requests/seen")
    def vendor_seen(deal_id: int, vendor: dict = Depends(current_vendor)):
        t = vendor["tenant_id"]
        with sales._conn() as c:
            mark_seen(c, t, deal_id, "vendor", vendor["id"])
            c.commit()
        return {"ok": True}

    return admin, partner
