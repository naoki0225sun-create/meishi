"""
みんなの予定（TimeTree のような共有カレンダー）
  ・アカウントは分けず、名前を選ぶだけ（初めての人は名前を入れる・見るだけも可）
  ・日を選んで「午前／午後前半／午後後半／終日／時間を入力」で予定を入れる
  ・案件のアポ（アポ日時）も自動で載る
  ・テレアポ業者は「予定がある時間帯」と名前だけ見られる（件名・メモ・お客様名は見えない）
"""
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import sales

SLOTS = {
    "am": {"label": "午前", "start": 9 * 60, "end": 12 * 60},
    "pm1": {"label": "午後前半", "start": 13 * 60, "end": 15 * 60},
    "pm2": {"label": "午後後半", "start": 15 * 60 + 30, "end": 17 * 60 + 30},
    "day": {"label": "終日", "start": 0, "end": 24 * 60},
}
COLORS = ["#2556E8", "#0E9F6E", "#D97706", "#9333EA", "#DB2777", "#0891B2", "#65A30D", "#DC2626", "#4F46E5", "#B45309"]
APPO_MINUTES = 60


def init_db():
    with sales._conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS cal_people (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            name TEXT NOT NULL,
            color TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (tenant_id, name)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS cal_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            person_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            start_min INTEGER NOT NULL,
            end_min INTEGER NOT NULL,
            slot TEXT,
            title TEXT,
            memo TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_cal_day ON cal_events(tenant_id, day)")
        c.commit()


def _hm(m: int) -> str:
    return f"{m // 60}:{m % 60:02d}"


def _range(frm: Optional[str], to: Optional[str]) -> tuple:
    try:
        f = date.fromisoformat(frm) if frm else date.today().replace(day=1) - timedelta(days=7)
        t = date.fromisoformat(to) if to else f + timedelta(days=50)
    except ValueError:
        raise HTTPException(400, "日付の形式が不正です")
    if t < f or (t - f).days > 120:
        raise HTTPException(400, "期間は120日までです")
    return f.isoformat(), t.isoformat()


def people(c, tenant_id: str) -> list:
    return [dict(r) for r in c.execute("SELECT id, name, color FROM cal_people WHERE tenant_id=? ORDER BY id", (tenant_id,))]


def busy(c, tenant_id: str, frm: str, to: str, detail: bool) -> list:
    """予定とアポ。detail=False（業者向け）は件名・メモ・お客様を出さない"""
    ppl = {p["id"]: p for p in people(c, tenant_id)}
    members_color = {p["name"]: p["color"] for p in ppl.values()}
    out = []
    for e in c.execute("""SELECT * FROM cal_events WHERE tenant_id=? AND day BETWEEN ? AND ?
            ORDER BY day, start_min""", (tenant_id, frm, to)):
        p = ppl.get(e["person_id"], {"name": "?", "color": "#64748B"})
        item = {"kind": "event", "day": e["day"], "start": e["start_min"], "end": e["end_min"],
                "label": SLOTS.get(e["slot"] or "", {}).get("label") or f"{_hm(e['start_min'])}〜{_hm(e['end_min'])}",
                "all_day": e["start_min"] == 0 and e["end_min"] >= 24 * 60,
                "person": p["name"], "color": p["color"]}
        if detail:
            item.update(id=e["id"], person_id=e["person_id"], slot=e["slot"], title=e["title"], memo=e["memo"])
        out.append(item)
    closed = sorted(sales.CLOSED_STAGES)
    for d in c.execute(f"""SELECT id, title, company_snapshot, owner, appo_at FROM deals WHERE tenant_id=? AND appo_at IS NOT NULL
            AND substr(appo_at, 1, 10) BETWEEN ? AND ? AND stage NOT IN ({','.join('?' * len(closed))})""",
                       [tenant_id, frm, to] + closed):
        ap = d["appo_at"]
        if len(ap) >= 16:
            s = int(ap[11:13]) * 60 + int(ap[14:16])
            e_ = min(s + APPO_MINUTES, 24 * 60)
            label = f"{_hm(s)}〜 アポ"
        else:
            s, e_, label = 0, 24 * 60, "アポ（時間未定）"
        who = d["owner"] or ""
        item = {"kind": "appo", "day": ap[:10], "start": s, "end": e_, "label": label, "all_day": len(ap) < 16,
                "person": who or "担当未定", "color": members_color.get(who, "#475569")}
        if detail:
            item.update(deal_id=d["id"], title=f"{d['company_snapshot'] or ''} {d['title'] or ''}".strip())
        out.append(item)
    out.sort(key=lambda x: (x["day"], x["start"], x["end"]))
    return out


class PersonIn(BaseModel):
    name: str


class EventIn(BaseModel):
    person_id: int
    day: str
    slot: Optional[str] = None          # am / pm1 / pm2 / day / None（時間を入力）
    start: Optional[str] = None         # "HH:MM"
    end: Optional[str] = None
    title: Optional[str] = None
    memo: Optional[str] = None


def _minutes(v: Optional[str], label: str) -> int:
    try:
        h, m = (v or "").split(":")[:2]
        n = int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        raise HTTPException(400, f"{label}を入れてください（例: 10:00）")
    if not 0 <= n <= 24 * 60:
        raise HTTPException(400, f"{label}が不正です")
    return n


def _event_values(c, t: str, body: EventIn) -> tuple:
    if not c.execute("SELECT 1 FROM cal_people WHERE id=? AND tenant_id=?", (body.person_id, t)).fetchone():
        raise HTTPException(400, "名前を選んでください")
    try:
        day = date.fromisoformat(body.day).isoformat()
    except (ValueError, TypeError):
        raise HTTPException(400, "日付が不正です")
    if body.slot:
        if body.slot not in SLOTS:
            raise HTTPException(400, "時間帯が不正です")
        s, e = SLOTS[body.slot]["start"], SLOTS[body.slot]["end"]
    else:
        s, e = _minutes(body.start, "開始時刻"), _minutes(body.end, "終了時刻")
        if e <= s:
            raise HTTPException(400, "終了は開始より後にしてください")
    return (body.person_id, day, s, e, body.slot or None, sales._clean_text(body.title, 80, "件名"),
            sales._clean_text(body.memo, 500, "メモ"))


def build_router(check_session: Callable) -> APIRouter:
    r = APIRouter(prefix="/api/sales/cal")

    @r.get("/people")
    def list_people(session: dict = Depends(check_session)):
        with sales._conn() as c:
            return {"people": people(c, session["tenant_id"]), "slots": SLOTS}

    @r.post("/people")
    def add_person(body: PersonIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        name = sales._clean_text(body.name, 30, "名前")
        if not name:
            raise HTTPException(400, "名前を入れてください")
        with sales._conn() as c:
            row = c.execute("SELECT id, name, color FROM cal_people WHERE tenant_id=? AND name=?", (t, name)).fetchone()
            if row:
                return dict(row)   # 同じ名前ならその人として使う
            n = c.execute("SELECT COUNT(*) n FROM cal_people WHERE tenant_id=?", (t,)).fetchone()["n"]
            if n >= 60:
                raise HTTPException(400, "名前は60人までです")
            cur = c.execute("INSERT INTO cal_people (tenant_id, name, color, created_at) VALUES (?,?,?,?)",
                            (t, name, COLORS[n % len(COLORS)], sales._now()))
            c.commit()
            return {"id": cur.lastrowid, "name": name, "color": COLORS[n % len(COLORS)]}

    @r.get("/events")
    def list_events(frm: Optional[str] = None, to: Optional[str] = None, session: dict = Depends(check_session)):
        f, t_ = _range(frm, to)
        with sales._conn() as c:
            return {"events": busy(c, session["tenant_id"], f, t_, detail=True)}

    @r.post("/events")
    def add_event(body: EventIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with sales._conn() as c:
            vals = _event_values(c, t, body)
            cur = c.execute("""INSERT INTO cal_events (tenant_id, person_id, day, start_min, end_min, slot, title, memo, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""", (t, *vals, sales._now(), sales._now()))
            c.commit()
        return {"ok": True, "id": cur.lastrowid}

    @r.put("/events/{event_id}")
    def update_event(event_id: int, body: EventIn, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with sales._conn() as c:
            vals = _event_values(c, t, body)
            cur = c.execute("""UPDATE cal_events SET person_id=?, day=?, start_min=?, end_min=?, slot=?, title=?, memo=?, updated_at=?
                WHERE id=? AND tenant_id=?""", (*vals, sales._now(), event_id, t))
            c.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "予定が見つかりません")
        return {"ok": True}

    @r.delete("/events/{event_id}")
    def delete_event(event_id: int, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with sales._conn() as c:
            cur = c.execute("DELETE FROM cal_events WHERE id=? AND tenant_id=?", (event_id, t))
            c.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "予定が見つかりません")
        return {"ok": True}

    return r
