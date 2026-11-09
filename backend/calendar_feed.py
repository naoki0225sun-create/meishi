"""カレンダー連携（iCalendar 形式の購読URL）

Googleカレンダーの「URLで追加」や iPhone の「照会するカレンダー」に登録すると、
次のアクション・受注予定日・打合せ予定・再アプローチ・リピートの時期が自動で出る。
URLに含む秘密のトークンで本人を判定する（ログイン不要。漏れたら設定で作り直す）。

  GET /api/sales/calendar/{token}.ics
"""
from datetime import date, datetime, timedelta

from fastapi import APIRouter, HTTPException, Response

import sales

TZ = "Asia/Tokyo"
VTIMEZONE = ["BEGIN:VTIMEZONE", f"TZID:{TZ}", "BEGIN:STANDARD", "DTSTART:19700101T000000",
             "TZOFFSETFROM:+0900", "TZOFFSETTO:+0900", "TZNAME:JST", "END:STANDARD", "END:VTIMEZONE"]


def esc(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n")


def fold(line: str) -> list:
    """75オクテットごとに折り返す（RFC 5545）"""
    out, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        limit = 75 if not out else 74
        if len(cur) + len(b) > limit:
            out.append(cur.decode("utf-8"))
            cur = b""
        cur += b
    out.append(cur.decode("utf-8"))
    return [out[0]] + [" " + x for x in out[1:]]


def build_ics(name: str, events: list, stamp: str) -> str:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//wako-apps//sales//JA", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", f"X-WR-CALNAME:{esc(name)}", f"X-WR-TIMEZONE:{TZ}",
             "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H"] + VTIMEZONE
    for ev in events:
        try:
            block = _event_lines(ev, stamp)
        except (ValueError, TypeError, KeyError):
            continue   # 日付がおかしい1件はとばす（全体を止めない）
        lines += block
    lines.append("END:VCALENDAR")
    out = []
    for ln in lines:
        out += fold(ln)
    return "\r\n".join(out) + "\r\n"


def _event_lines(ev: dict, stamp: str) -> list:
    lines = []
    if True:
        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{ev['uid']}")
        lines.append(f"DTSTAMP:{stamp}")
        if ev.get("all_day"):
            d = date.fromisoformat(ev["date"])
            lines.append(f"DTSTART;VALUE=DATE:{d.strftime('%Y%m%d')}")
            lines.append(f"DTEND;VALUE=DATE:{(d + timedelta(days=1)).strftime('%Y%m%d')}")
        else:
            st = datetime.fromisoformat(ev["start"][:19])
            lines.append(f"DTSTART;TZID={TZ}:{st.strftime('%Y%m%dT%H%M%S')}")
            lines.append(f"DTEND;TZID={TZ}:{(st + timedelta(hours=1)).strftime('%Y%m%dT%H%M%S')}")
        lines.append(f"SUMMARY:{esc(ev['summary'])}")
        if ev.get("description"):
            lines.append(f"DESCRIPTION:{esc(ev['description'])}")
        if ev.get("url"):
            lines.append(f"URL:{ev['url']}")
        lines.append("END:VEVENT")
    return lines


def collect(tenant_id: str, owner, app_url: str, today: date = None) -> list:
    """owner=None なら全員分。'' なら担当なしの案件"""
    today = today or date.today()
    since = (today - timedelta(days=30)).isoformat()
    closed = sorted(sales.CLOSED_STAGES)
    with sales._conn() as c:
        q = "SELECT * FROM deals WHERE tenant_id=?"
        params = [tenant_id]
        if owner is not None:
            q += " AND COALESCE(owner,'')=?"
            params.append(owner)
        deals = [dict(r) for r in c.execute(q, params)]
        ids = [d["id"] for d in deals]
        meetings = []
        if ids:
            meetings = [dict(r) for r in c.execute(f"""SELECT id, deal_id, occurred_at, title FROM activities
                WHERE tenant_id=? AND kind='meeting' AND occurred_at>=? AND deal_id IN ({','.join('?' * len(ids))})""",
                [tenant_id, since] + ids)]
    by_id = {d["id"]: d for d in deals}
    evs = []

    def who(d):
        return " ".join(x for x in (d.get("company_snapshot"), d.get("contact_snapshot")) if x)

    for d in deals:
        link = f"{app_url}/sales.html?deal={d['id']}"
        label = f"#{d['id']} {who(d)}｜{d['title']}"
        is_open = d["stage"] not in closed
        if is_open and d.get("next_action_due") and d["next_action_due"] >= since:
            evs.append({"uid": f"deal-{d['id']}-next@wako-apps", "all_day": True, "date": d["next_action_due"],
                        "summary": f"【営業】{d.get('next_action') or '次のアクション'}（#{d['id']} {who(d)}）",
                        "description": f"{label}\n段階: {d['stage']}\n{link}", "url": link})
        if is_open and d.get("expected_close") and d["expected_close"] >= since:
            evs.append({"uid": f"deal-{d['id']}-close@wako-apps", "all_day": True, "date": d["expected_close"],
                        "summary": f"【受注予定】#{d['id']} {who(d)}", "description": f"{label}\n{link}", "url": link})
        if is_open and d.get("appo_at") and d["appo_at"][:10] >= since:
            ap = d["appo_at"]
            ev = {"uid": f"deal-{d['id']}-appo@wako-apps", "summary": f"【アポ】{who(d)}｜{d['title']}",
                  "description": f"{label}\n段階: {d['stage']}\n{link}", "url": link}
            if len(ap) >= 16:
                ev["start"] = ap[:16] + ":00"
            else:
                ev.update(all_day=True, date=ap[:10])
            evs.append(ev)
        if is_open and d.get("delivery_on") and d["delivery_on"] >= since:
            site = f"（{d['site_name']}）" if d.get("site_name") else ""
            evs.append({"uid": f"deal-{d['id']}-delivery@wako-apps", "all_day": True, "date": d["delivery_on"],
                        "summary": f"【納期】#{d['id']} {who(d)}{site}", "description": f"{label}\n段階: {d['stage']}\n{link}",
                        "url": link})
        for key, title in (("reapproach_on", "【再アプローチ】"), ("repeat_on", "【リピートの声かけ】")):
            if not is_open and d.get(key) and d[key] >= since:
                evs.append({"uid": f"deal-{d['id']}-{key}@wako-apps", "all_day": True, "date": d[key],
                            "summary": f"{title}#{d['id']} {who(d)}", "description": f"{label}\n{link}", "url": link})
    for m in meetings:
        d = by_id.get(m["deal_id"])
        if not d or len(m["occurred_at"] or "") < 16:
            continue
        link = f"{app_url}/sales.html?deal={d['id']}"
        evs.append({"uid": f"act-{m['id']}@wako-apps", "start": m["occurred_at"],
                    "summary": f"【打合せ】{who(d)}｜{m.get('title') or d['title']}",
                    "description": f"#{d['id']} {d['title']}\n{link}", "url": link})
    return evs


def build_router(app_url_fn, tenant_name_fn=None) -> APIRouter:
    r = APIRouter()

    @r.get("/api/sales/calendar/{token}.ics")
    def feed(token: str):
        if not token or len(token) < 20:
            raise HTTPException(404, "not found")
        with sales._conn() as c:
            m = c.execute("SELECT tenant_id, name FROM members WHERE ics_token=?", (token,)).fetchone()
            t = None
            if not m:
                t = c.execute("SELECT tenant_id FROM sales_settings WHERE ics_token=?", (token,)).fetchone()
        if not m and not t:
            raise HTTPException(404, "not found")
        tenant = m["tenant_id"] if m else t["tenant_id"]
        owner = m["name"] if m else None
        tname = tenant_name_fn(tenant) if tenant_name_fn else tenant
        cal_name = f"営業（{owner}）" if owner else f"営業（{tname} 全員）"
        stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        body = build_ics(cal_name, collect(tenant, owner, app_url_fn()), stamp)
        return Response(content=body.encode("utf-8"), media_type="text/calendar; charset=utf-8",
                        headers={"Cache-Control": "private, max-age=600"})

    return r
