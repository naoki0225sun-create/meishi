"""工程と担当者（phase37）

受注した案件（注文書を確定して案件に紐づいたもの）ごとに、
- 区分（盤の筐体／レーザー加工／その他）
- CAD担当
- 工程ごとの担当者・工数（0.5日単位）・終わる日
を現場のリーダーが決める。決めていない案件は「設定」の逆算工程から仮の予定を出す。

画面は納期タブの3つの表示（2か月・2週間の案件ガント・担当者別）と、営業の案件画面の工程欄。
工程の「完了」は従来どおり dz_schedule（工程名ごと）に残すので、現場の画面・製作指示書とそのままつながる。
"""
import re
from datetime import date, timedelta
from typing import Callable, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

import sales
import zumen
from sales import _conn

CATEGORIES = {"panel": "盤の筐体", "laser": "レーザー加工", "other": "その他"}
PALETTE = ["#2556E8", "#E8590C", "#0CA678", "#AE3EC9", "#1098AD", "#66A80F", "#F08C00", "#5C7CFA", "#495057"]  # 赤は「遅れ」と紛れるので使わない
MAX_STAFF = 40
MAX_TASKS = 20


def init_db():
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS dz_staff (
            id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, name TEXT NOT NULL, color TEXT,
            cad INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1, sort INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_dz_staff ON dz_staff(tenant_id, active, sort);
        CREATE TABLE IF NOT EXISTS dz_job (
            tenant_id TEXT NOT NULL, deal_id INTEGER NOT NULL, category TEXT, cad_staff_id INTEGER, note TEXT,
            planned INTEGER NOT NULL DEFAULT 0, updated_by TEXT, updated_at TEXT NOT NULL, PRIMARY KEY (tenant_id, deal_id));
        CREATE TABLE IF NOT EXISTS dz_task (
            id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, deal_id INTEGER NOT NULL, label TEXT NOT NULL,
            staff_id INTEGER, days REAL NOT NULL DEFAULT 1, start_on TEXT NOT NULL, end_on TEXT NOT NULL, sort INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS idx_dz_task_deal ON dz_task(tenant_id, deal_id);
        CREATE INDEX IF NOT EXISTS idx_dz_task_staff ON dz_task(tenant_id, staff_id, end_on);
        """)
        c.commit()


# ── 営業日の計算 ──
def is_workday(d: date) -> bool:
    return d.weekday() < 5


def back_days(d: date, n: int) -> date:
    while n > 0:
        d -= timedelta(days=1)
        if is_workday(d):
            n -= 1
    return d


def start_from(end: date, days: float) -> date:
    """終わる日と工数（日）から始める日を出す。0.5日・1日はその日のうちに終わる"""
    need = max(1, int(-(-days // 1)))   # 切り上げ
    d = end
    while not is_workday(d):
        d -= timedelta(days=1)
    for _ in range(need - 1):
        d = back_days(d, 1)
    return d


def workdays(a: date, b: date) -> list:
    out, d = [], a
    while d <= b:
        if is_workday(d):
            out.append(d)
        d += timedelta(days=1)
    return out or [a]


def guess_category(texts: list, is_group: bool) -> str:
    s = " ".join(t or "" for t in texts)
    if re.search(r"レーザー|レーザ|LASER|切断のみ|抜きのみ", s, re.I):
        return "laser"
    if re.search(r"盤|筐体|キュービクル|ボックス|BOX|箱|分電|制御|配電|受電", s, re.I) or is_group:
        return "panel"
    return ""


def own_staff(c, t: str, active_only: bool = False) -> list:
    q = "SELECT * FROM dz_staff WHERE tenant_id=?" + (" AND active=1" if active_only else "") + " ORDER BY active DESC, sort, id"
    return [dict(x) for x in c.execute(q, (t,))]


def plan_for(c, t: str, row: dict, steps: list, staff_names: dict) -> dict:
    """1件の案件の工程（保存済みならそれ、なければ逆算工程から仮に作る）"""
    deal_id = row["deal_id"]
    job = c.execute("SELECT * FROM dz_job WHERE tenant_id=? AND deal_id=?", (t, deal_id)).fetchone()
    done = {x["stage"]: x["done_on"] for x in c.execute("SELECT stage, done_on FROM dz_schedule WHERE tenant_id=? AND deal_id=?", (t, deal_id))}
    tasks = [dict(x) for x in c.execute("SELECT * FROM dz_task WHERE tenant_id=? AND deal_id=? ORDER BY sort, end_on, id", (t, deal_id))]
    planned = bool(job and job["planned"] and tasks)
    if not planned:
        tasks = []
        if row.get("due"):
            due = date.fromisoformat(row["due"])
            for i, st in enumerate(steps):
                end = back_days(due, int(st.get("days") or 0))
                tasks.append({"id": None, "label": st["label"], "staff_id": None, "days": 1.0, "start_on": end.isoformat(), "end_on": end.isoformat(), "sort": i})
    today = date.today().isoformat()
    for x in tasks:
        x["done"] = done.get(x["label"])
        x["staff"] = staff_names.get(x["staff_id"])
        x["late"] = not x["done"] and x["end_on"] < today and row.get("state") != "shipped"
    category = (job["category"] if job and job["category"] else None)
    if category is None:
        category = row.get("_guess") or ""
    return {"category": category, "category_label": CATEGORIES.get(category, "未分類"), "cad_staff_id": job["cad_staff_id"] if job else None,
            "cad_staff": staff_names.get(job["cad_staff_id"]) if job else None, "planned": planned, "tasks": tasks,
            "note": job["note"] if job else "", "updated_by": job["updated_by"] if job else None, "updated_at": job["updated_at"] if job else None}


def build_router(check_session: Callable, main_router) -> APIRouter:
    r = APIRouter(prefix="/api/zumen/plan")

    def steps_of(c, t: str, company_id) -> list:
        import zumen_extra
        kind = "prod" if (main_router and main_router.has_prod(t)) else "sheet"
        return zumen_extra.steps_for(c, t, company_id, kind)

    def rows_of(t: str, session: dict, frm: str, to: str) -> list:
        rows = main_router.schedule_rows(frm=frm, to=to, session=session)["rows"]
        return [x for x in rows if x.get("deal_id")]

    def enrich(c, t: str, rows: list) -> list:
        staff_names = {s["id"]: s["name"] for s in own_staff(c, t)}
        out = []
        for x in rows:
            texts = [x.get("title"), x.get("company")]
            for oid in x.get("order_ids") or []:
                texts += [ln["name"] for ln in c.execute("SELECT name FROM dz_lines WHERE doc_id=?", (oid,))]
            grp = c.execute("SELECT is_group FROM dz_companies WHERE id=?", (x.get("company_id"),)).fetchone() if x.get("company_id") else None
            x["_guess"] = guess_category(texts, bool(grp and grp["is_group"]))
            p = plan_for(c, t, x, steps_of(c, t, x.get("company_id")), staff_names)
            x.pop("_guess", None)
            x.pop("milestones", None)
            out.append(x | p)
        return out

    # ── 社員 ──
    @r.get("/staff")
    def staff(session: dict = Depends(check_session)):
        with _conn() as c:
            return {"staff": own_staff(c, session["tenant_id"]), "palette": PALETTE}

    class StaffItem(BaseModel):
        id: Optional[int] = None
        name: str
        color: Optional[str] = None
        cad: bool = False
        active: bool = True

    class StaffIn(BaseModel):
        staff: List[StaffItem]

    @r.put("/staff")
    def put_staff(body: StaffIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        if len(body.staff) > MAX_STAFF:
            raise HTTPException(400, f"登録できる社員は{MAX_STAFF}人までです")
        names = [s.name.strip() for s in body.staff]
        if any(not n for n in names):
            raise HTTPException(400, "名前が空欄の行があります")
        if len(set(names)) != len(names):
            raise HTTPException(400, "同じ名前の社員が2人います。名字と名前で区別してください")
        now = zumen._now()
        with _conn() as c:
            mine = {s["id"] for s in own_staff(c, t)}
            keep = set()
            for i, s in enumerate(body.staff):
                color = s.color if s.color and re.fullmatch(r"#[0-9A-Fa-f]{6}", s.color) else PALETTE[i % len(PALETTE)]
                if s.id and s.id in mine:
                    c.execute("UPDATE dz_staff SET name=?, color=?, cad=?, active=?, sort=? WHERE id=? AND tenant_id=?",
                              (s.name.strip()[:30], color, int(s.cad), int(s.active), i, s.id, t))
                    keep.add(s.id)
                elif s.id:
                    raise HTTPException(404, "社員が見つかりません")
                else:
                    keep.add(c.execute("INSERT INTO dz_staff (tenant_id, name, color, cad, active, sort, created_at) VALUES (?,?,?,?,?,?,?)",
                                       (t, s.name.strip()[:30], color, int(s.cad), int(s.active), i, now)).lastrowid)
            for sid in mine - keep:
                # 一度でも工程に入った人は消さずに「使わない」にする（過去の記録を残す）
                used = c.execute("SELECT 1 FROM dz_task WHERE staff_id=? UNION SELECT 1 FROM dz_job WHERE cad_staff_id=? LIMIT 1", (sid, sid)).fetchone()
                if used:
                    c.execute("UPDATE dz_staff SET active=0 WHERE id=?", (sid,))
                else:
                    c.execute("DELETE FROM dz_staff WHERE id=?", (sid,))
            c.commit()
            return {"staff": own_staff(c, t)}

    # ── 一覧（3つの表示で共通） ──
    @r.get("/board")
    def board(frm: str = Query("", alias="from"), to: str = "", session: dict = Depends(check_session)):
        t = session["tenant_id"]
        for v in (frm, to):
            if v and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                raise HTTPException(400, "日付の形式が正しくありません")
        today = date.today()
        f = frm or (today - timedelta(days=7)).isoformat()
        e = to or (today + timedelta(days=62)).isoformat()
        rows = rows_of(t, session, f, e)
        with _conn() as c:
            jobs = enrich(c, t, rows)
            staff_list = own_staff(c, t)
        # 担当者ごとの1日の負荷（工数を、始める日〜終わる日の営業日に均等に割る）
        load = {}
        for j in jobs:
            if j.get("state") == "shipped":
                continue
            for x in j["tasks"]:
                if x["done"] or not x["staff_id"]:
                    continue
                days = workdays(date.fromisoformat(x["start_on"]), date.fromisoformat(x["end_on"]))
                per = (x["days"] or 1) / len(days)
                for d in days:
                    k = d.isoformat()
                    if f <= k <= e:
                        load.setdefault(str(x["staff_id"]), {}).setdefault(k, 0)
                        load[str(x["staff_id"])][k] = round(load[str(x["staff_id"])][k] + per, 2)
        return {"jobs": jobs, "staff": staff_list, "load": load, "categories": CATEGORIES, "from": f, "to": e, "today": today.isoformat()}

    @r.get("/jobs/{deal_id}")
    def job(deal_id: int, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        today = date.today()
        rows = rows_of(t, session, (today - timedelta(days=400)).isoformat(), (today + timedelta(days=400)).isoformat())
        row = next((x for x in rows if x["deal_id"] == deal_id), None)
        with _conn() as c:
            if not row:
                dl = c.execute("SELECT * FROM deals WHERE id=? AND tenant_id=?", (deal_id, t)).fetchone()
                if not dl:
                    raise HTTPException(404, "案件が見つかりません")
                row = {"deal_id": deal_id, "title": dl["title"], "company": dl["company_snapshot"], "stage": dl["stage"],
                       "due": dl["delivery_on"], "state": "nodue" if not dl["delivery_on"] else "ok", "order_ids": [], "company_id": None}
            job_ = enrich(c, t, [row])[0]
            return {"job": job_, "staff": own_staff(c, t, active_only=True), "categories": CATEGORIES}

    class TaskIn(BaseModel):
        label: str
        staff_id: Optional[int] = None
        days: float = 1
        end_on: str

    class JobIn(BaseModel):
        category: str = ""
        cad_staff_id: Optional[int] = None
        note: str = ""
        tasks: List[TaskIn] = []
        actor: Optional[str] = None

    @r.put("/jobs/{deal_id}")
    def put_job(deal_id: int, body: JobIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        if body.category and body.category not in CATEGORIES:
            raise HTTPException(400, "区分の指定が正しくありません")
        if len(body.tasks) > MAX_TASKS:
            raise HTTPException(400, f"工程は{MAX_TASKS}個までです")
        labels = [x.label.strip()[:20] for x in body.tasks]
        if any(not x for x in labels):
            raise HTTPException(400, "工程名が空欄の行があります")
        if len(set(labels)) != len(labels):
            raise HTTPException(400, "同じ名前の工程が2つあります。名前を変えてください")
        rows = []
        for x, label in zip(body.tasks, labels):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", x.end_on or ""):
                raise HTTPException(400, f"「{label}」の終わる日が正しくありません")
            if not (0.5 <= x.days <= 60) or abs(x.days * 2 - round(x.days * 2)) > 1e-6:
                raise HTTPException(400, f"「{label}」の工数は0.5日単位で、0.5〜60日の範囲で入力してください")
            end = date.fromisoformat(x.end_on)
            rows.append((label, x.staff_id, float(x.days), start_from(end, x.days).isoformat(), end.isoformat()))
        now = zumen._now()
        with _conn() as c:
            if not c.execute("SELECT 1 FROM deals WHERE id=? AND tenant_id=?", (deal_id, t)).fetchone():
                raise HTTPException(404, "案件が見つかりません")
            mine = {s["id"] for s in own_staff(c, t)}
            for sid in [x[1] for x in rows] + [body.cad_staff_id]:
                if sid is not None and sid not in mine:
                    raise HTTPException(400, "担当者が見つかりません。社員の登録を確認してください")
            c.execute("BEGIN IMMEDIATE")
            c.execute("""INSERT INTO dz_job (tenant_id, deal_id, category, cad_staff_id, note, planned, updated_by, updated_at) VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(tenant_id, deal_id) DO UPDATE SET category=excluded.category, cad_staff_id=excluded.cad_staff_id, note=excluded.note,
                planned=excluded.planned, updated_by=excluded.updated_by, updated_at=excluded.updated_at""",
                      (t, deal_id, body.category or None, body.cad_staff_id, (body.note or "")[:500], 1 if rows else 0, (body.actor or "")[:30] or None, now))
            c.execute("DELETE FROM dz_task WHERE tenant_id=? AND deal_id=?", (t, deal_id))
            for i, (label, sid, days, s, e) in enumerate(rows):
                c.execute("INSERT INTO dz_task (tenant_id, deal_id, label, staff_id, days, start_on, end_on, sort) VALUES (?,?,?,?,?,?,?,?)",
                          (t, deal_id, label, sid, days, s, e, i))
            c.commit()
        return job(deal_id, session=session)

    class CatIn(BaseModel):
        category: str

    @r.post("/jobs/{deal_id}/category")
    def set_category(deal_id: int, body: CatIn, session: dict = Depends(check_session)):
        """区分だけを変える（2か月の表示から）"""
        t = session["tenant_id"]
        if body.category and body.category not in CATEGORIES:
            raise HTTPException(400, "区分の指定が正しくありません")
        with _conn() as c:
            if not c.execute("SELECT 1 FROM deals WHERE id=? AND tenant_id=?", (deal_id, t)).fetchone():
                raise HTTPException(404, "案件が見つかりません")
            c.execute("""INSERT INTO dz_job (tenant_id, deal_id, category, updated_at) VALUES (?,?,?,?)
                ON CONFLICT(tenant_id, deal_id) DO UPDATE SET category=excluded.category, updated_at=excluded.updated_at""",
                      (t, deal_id, body.category or None, zumen._now()))
            c.commit()
        return {"ok": True}

    @r.delete("/jobs/{deal_id}/tasks")
    def reset_tasks(deal_id: int, session: dict = Depends(check_session)):
        """割り当てを消して、逆算工程の仮の予定に戻す（区分とCAD担当は残す）"""
        t = session["tenant_id"]
        with _conn() as c:
            if not c.execute("SELECT 1 FROM deals WHERE id=? AND tenant_id=?", (deal_id, t)).fetchone():
                raise HTTPException(404, "案件が見つかりません")
            c.execute("DELETE FROM dz_task WHERE tenant_id=? AND deal_id=?", (t, deal_id))
            c.execute("UPDATE dz_job SET planned=0 WHERE tenant_id=? AND deal_id=?", (t, deal_id))
            c.commit()
        return job(deal_id, session=session)

    return r


def milestones_of(c, t: str, deal_id: int) -> Optional[list]:
    """割り当て済みの案件なら、工程（担当者つき）を逆算工程の代わりに返す。未割り当てなら None"""
    try:
        job = c.execute("SELECT planned FROM dz_job WHERE tenant_id=? AND deal_id=?", (t, deal_id)).fetchone()
    except Exception:
        return None
    if not job or not job["planned"]:
        return None
    names = {s["id"]: s["name"] for s in own_staff(c, t)}
    return [{"label": x["label"], "plan": x["end_on"], "start": x["start_on"], "days": x["days"], "staff": names.get(x["staff_id"])}
            for x in c.execute("SELECT * FROM dz_task WHERE tenant_id=? AND deal_id=? ORDER BY sort, end_on", (t, deal_id))]
