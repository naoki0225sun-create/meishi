"""名刺の一括重複整理

既存の 1組ずつのマージ（/api/duplicates）と同じ仕組み（merged_ids で消す側を隠す・
残す側にオーバーライドで値を補う）を使い、確実なものをまとめて処理する。
スプレッドシートの行は消さないので、元に戻すことができる。

  GET  /api/dedupe/preview        まとめ候補（確実／要確認）
  POST /api/dedupe/apply          選んだグループをまとめる（バッチとして記録）
  GET  /api/dedupe/batches        実行履歴
  POST /api/dedupe/batches/{id}/undo  元に戻す
"""
import json
import os
import re
import shutil
import unicodedata
from datetime import datetime
from difflib import SequenceMatcher
from typing import Callable, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import db
import sales

FIELDS = ["company", "name", "department_position", "email", "mobile", "phone", "address", "website"]
FIELD_LABELS = {
    "company": "会社", "name": "氏名", "department_position": "部署・役職", "email": "メール",
    "mobile": "携帯", "phone": "電話", "address": "住所", "website": "URL", "assigned_to": "所有者",
}
MAX_GROUP = 8  # これより大きいグループは誤判定の疑いが強いので要確認に回す


def init_db():
    with sales._conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS dedupe_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            group_count INTEGER NOT NULL,
            removed_count INTEGER NOT NULL,
            data TEXT NOT NULL,
            undone_at TEXT
        )""")
        c.commit()


# ── 正規化 ──
def _nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "")


def name_key(s: str) -> str:
    return re.sub(r"[\s・･.,]", "", _nfkc(s)).lower()


def company_key(card: dict) -> str:
    k = _nfkc(card.get("company_normalized") or "").lower()
    k = re.sub(r"[\s()（）]", "", k)
    return "" if k in ("", "不明") else k


def email_key(s: str) -> str:
    return _nfkc(s).strip().lower()


def phone_key(s: str) -> str:
    d = re.sub(r"\D", "", _nfkc(s))
    if d.startswith("81") and len(d) >= 11:
        d = "0" + d[2:]
    return d if len(d) >= 10 else ""


def _date_key(s: str) -> str:
    """登録日の並べ替え用（形式がまちまちでも数字だけで比較する）"""
    nums = re.findall(r"\d+", s or "")
    if len(nums) >= 3:
        y, m, d = nums[0], nums[1], nums[2]
        rest = "".join(n.zfill(2) for n in nums[3:5])
        return f"{y.zfill(4)}{m.zfill(2)}{d.zfill(2)}{rest}"
    return ""


def _filled(card: dict) -> int:
    return sum(1 for f in FIELDS if (card.get(f) or "").strip())


def pick_kept(cards: list) -> dict:
    """残す名刺：登録日が新しい → 画像あり → 項目が多い"""
    return max(cards, key=lambda c: (_date_key(c.get("registered_date")), bool(c.get("has_image")), _filled(c)))


def merged_values(kept: dict, others: list) -> dict:
    """残す名刺の空欄を他の名刺の値で埋める。所有者は全員分をまとめる"""
    order = sorted(others, key=lambda c: _date_key(c.get("registered_date")), reverse=True)
    out = {}
    for f in FIELDS:
        v = (kept.get(f) or "").strip()
        if not v:
            for o in order:
                ov = (o.get(f) or "").strip()
                if ov:
                    v = ov
                    break
        out[f] = v
    # 会社名と氏名は正式な書き方（株式会社つき・姓名の間にスペース）を優先する
    for f, keyf in (("company", lambda c: company_key(c)), ("name", lambda c: name_key(c.get("name")))):
        base = keyf(kept)
        same = [c for c in [kept] + order if (c.get(f) or "").strip() and (not base or keyf(c) == base)]
        if same:
            out[f] = max(same, key=lambda c: len((c.get(f) or "").strip()))[f].strip()
    owners = []
    for c in [kept] + order:
        for x in (c.get("assigned_to") or "").split(","):
            x = x.strip()
            if x and x not in owners:
                owners.append(x)
    out["assigned_to"] = ", ".join(owners)
    return out


# ── 判定 ──
class _UF:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


def find_groups(cards: list, dismissed: set) -> dict:
    """確実（自動でまとめてよい）と要確認のグループを返す"""
    by_id = {c["id"]: c for c in cards}

    def ok(a, b):
        pa, pb = (a, b) if a < b else (b, a)
        return a != b and (pa, pb) not in dismissed

    strict_reason = {}
    review_reason = {}

    def link(store, a, b, reason):
        if not ok(a, b):
            return
        key = (a, b) if a < b else (b, a)
        store.setdefault(key, reason)

    # 同じ氏名のなかで「会社・メール・携帯・電話」のどれかが一致 → 確実
    by_name = {}
    for c in cards:
        nk = name_key(c.get("name"))
        if len(nk) >= 2:
            by_name.setdefault(nk, []).append(c)
    for group in by_name.values():
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                reason = None
                if company_key(a) and company_key(a) == company_key(b):
                    reason = "氏名と会社が一致"
                elif email_key(a.get("email")) and email_key(a.get("email")) == email_key(b.get("email")):
                    reason = "氏名とメールが一致"
                elif phone_key(a.get("mobile")) and phone_key(a.get("mobile")) == phone_key(b.get("mobile")):
                    reason = "氏名と携帯が一致"
                elif phone_key(a.get("phone")) and phone_key(a.get("phone")) == phone_key(b.get("phone")):
                    reason = "氏名と電話が一致"
                if not reason:
                    continue  # 同姓同名で連絡先も違うものは対象外
                ca, cb = company_key(a), company_key(b)
                if ca and cb and ca != cb:
                    # 連絡先は同じだが会社名が違う（表記ゆれ・社名変更・転職）→ 人が確認
                    link(review_reason, a["id"], b["id"], reason.replace("一致", "一致（会社名が違う）"))
                else:
                    link(strict_reason, a["id"], b["id"], reason)

    # 要確認：メールか携帯が同じなのに氏名が違う／同じ会社で氏名がよく似ている
    by_email, by_mobile, by_company = {}, {}, {}
    for c in cards:
        e = email_key(c.get("email"))
        if e:
            by_email.setdefault(e, []).append(c)
        m = phone_key(c.get("mobile"))
        if m:
            by_mobile.setdefault(m, []).append(c)
        ck = company_key(c)
        if ck:
            by_company.setdefault(ck, []).append(c)
    for idx, reason in ((by_email, "メールが一致（氏名が違う）"), (by_mobile, "携帯が一致（氏名が違う）")):
        for group in idx.values():
            if len(group) > MAX_GROUP:
                continue  # 代表アドレスなど
            for i, a in enumerate(group):
                for b in group[i + 1:]:
                    if name_key(a.get("name")) != name_key(b.get("name")):
                        link(review_reason, a["id"], b["id"], reason)
    for group in by_company.values():
        if len(group) > 300:
            continue
        for i, a in enumerate(group):
            na = name_key(a.get("name"))
            if len(na) < 3:
                continue
            for b in group[i + 1:]:
                nb = name_key(b.get("name"))
                if len(nb) < 3 or na == nb:
                    continue
                if SequenceMatcher(None, na, nb).ratio() >= 0.8:
                    link(review_reason, a["id"], b["id"], "同じ会社で氏名がよく似ている")

    def build(pairs: dict):
        uf = _UF()
        for (a, b) in pairs:
            uf.union(a, b)
        members = {}
        for (a, b) in pairs:
            members.setdefault(uf.find(a), set()).update([a, b])
        out = []
        for root, ids in members.items():
            reasons = sorted({r for (a, b), r in pairs.items() if a in ids})
            group_cards = [by_id[i] for i in ids]
            out.append((group_cards, reasons))
        return out

    strict, review = [], []
    strict_root = {}
    for group_cards, reasons in build(strict_reason):
        target = strict if len(group_cards) <= MAX_GROUP else review
        target.append(_group_dict(group_cards, reasons, "strict" if target is strict else "review"))
        if target is strict:
            for c in group_cards:
                strict_root[c["id"]] = group_cards[0]["id"]

    # 要確認：確実グループ内で完結するペアは除く
    review_pairs = {k: v for k, v in review_reason.items()
                    if not (k[0] in strict_root and strict_root.get(k[0]) == strict_root.get(k[1]))}
    for group_cards, reasons in build(review_pairs):
        if len(group_cards) <= MAX_GROUP:
            review.append(_group_dict(group_cards, reasons, "review"))

    key = lambda g: (g["cards"][0].get("company") or "", g["cards"][0].get("name") or "")
    strict.sort(key=key)
    review.sort(key=key)
    return {"strict": strict, "review": review}


def _card_view(c: dict) -> dict:
    keys = ["id", "company", "name", "department_position", "email", "mobile", "phone", "address",
            "website", "assigned_to", "registered_date", "has_image"]
    return {k: c.get(k) for k in keys}


def _group_dict(cards: list, reasons: list, level: str) -> dict:
    kept = pick_kept(cards)
    others = [c for c in cards if c["id"] != kept["id"]]
    values = merged_values(kept, others)
    filled = [f for f in FIELDS + ["assigned_to"] if values.get(f) and values[f] != (kept.get(f) or "").strip()]
    ordered = [kept] + sorted(others, key=lambda c: _date_key(c.get("registered_date")), reverse=True)
    return {
        "level": level,
        "reasons": reasons,
        "kept_id": kept["id"],
        "cards": [_card_view(c) for c in ordered],
        "fill": {f: values[f] for f in filled},
    }


# ── 実行 ──
class ApplyGroup(BaseModel):
    kept_id: str
    member_ids: list[str]


class ApplyRequest(BaseModel):
    groups: list[ApplyGroup]


def build_router(check_session: Callable, fetch_cards: Callable,
                 image_path_for: Optional[Callable] = None) -> APIRouter:
    r = APIRouter(prefix="/api/dedupe")

    @r.get("/preview")
    def preview(session: dict = Depends(check_session)):
        t = session["tenant_id"]
        res = find_groups(fetch_cards(t), db.get_dismissed_pairs(t))
        res["strict_removed"] = sum(len(g["cards"]) - 1 for g in res["strict"])
        res["field_labels"] = FIELD_LABELS
        return res

    def _copy_image(t, src_id, dst_id, log):
        if not image_path_for:
            return
        for side in ("front", "back"):
            dst = image_path_for(t, dst_id, side)
            if os.path.exists(dst):
                continue
            if side == "front" and os.path.exists(image_path_for(t, dst_id, None)):
                continue  # 旧形式の表面がある
            src = image_path_for(t, src_id, side)
            if side == "front" and not os.path.exists(src):
                src = image_path_for(t, src_id, None)
            if os.path.exists(src):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                log.append(dst)

    @r.post("/apply")
    def apply(body: ApplyRequest, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        if not body.groups:
            raise HTTPException(400, "まとめるグループを選んでください")
        if len(body.groups) > 1000:
            raise HTTPException(400, "一度に処理できるのは1000グループまでです")
        cards = {c["id"]: c for c in fetch_cards(t)}
        seen = set()
        for g in body.groups:
            ids = set(g.member_ids) | {g.kept_id}
            if len(ids) < 2:
                raise HTTPException(400, "2枚以上の名刺を選んでください")
            missing = [i for i in ids if i not in cards]
            if missing:
                raise HTTPException(409, "名刺の一覧が変わっています。画面を開き直してください")
            if ids & seen:
                raise HTTPException(400, "同じ名刺が複数のグループに入っています")
            seen |= ids

        log = []
        removed_total = 0
        for g in body.groups:
            kept = cards[g.kept_id]
            others = [cards[i] for i in dict.fromkeys(g.member_ids) if i != g.kept_id]
            entry = {
                "kept_id": kept["id"],
                "removed_ids": [o["id"] for o in others],
                "prev_overrides": db.get_overrides(t, kept["id"]),
                "prev_note": db.get_note(t, kept["id"]),
                "prev_tags": db.get_tags(t, kept["id"]),
                "deals": [],
                "images": [],
            }
            # 項目を補う
            values = merged_values(kept, others)
            for f, v in values.items():
                if v and v != (kept.get(f) or "").strip() and f in db.EDITABLE_FIELDS:
                    db.set_override(t, kept["id"], f, v)
            # メモとタグ
            note = entry["prev_note"]
            for o in others:
                on = (o.get("note") or "").strip()
                if on and on not in note:
                    note = (note + "\n\n---\n" + on) if note.strip() else on
                for tag in o.get("tags") or []:
                    db.add_tag(t, kept["id"], tag)
                _copy_image(t, o["id"], kept["id"], entry["images"])
            if note != entry["prev_note"]:
                db.set_note(t, kept["id"], note)
            # 案件のつなぎ替え
            with sales._conn() as c:
                for o in others:
                    for row in c.execute("SELECT id FROM deals WHERE tenant_id=? AND card_id=?", (t, o["id"])):
                        entry["deals"].append([row["id"], o["id"]])
                    c.execute("UPDATE deals SET card_id=? WHERE tenant_id=? AND card_id=?", (kept["id"], t, o["id"]))
                c.commit()
            for o in others:
                db.record_merge(t, o["id"], kept["id"])
            removed_total += len(others)
            log.append(entry)

        with sales._conn() as c:
            cur = c.execute("""INSERT INTO dedupe_batches (tenant_id, created_at, group_count, removed_count, data)
                VALUES (?,?,?,?,?)""", (t, sales._now(), len(log), removed_total, json.dumps(log, ensure_ascii=False)))
            batch_id = cur.lastrowid
            c.commit()
        return {"ok": True, "batch_id": batch_id, "groups": len(log), "removed": removed_total}

    @r.get("/batches")
    def batches(session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with sales._conn() as c:
            rows = c.execute("""SELECT id, created_at, group_count, removed_count, undone_at
                FROM dedupe_batches WHERE tenant_id=? ORDER BY id DESC LIMIT 20""", (t,)).fetchall()
        return {"batches": [dict(x) for x in rows]}

    @r.post("/batches/{batch_id}/undo")
    def undo(batch_id: int, session: dict = Depends(check_session)):
        t = session["tenant_id"]
        with sales._conn() as c:
            row = c.execute("SELECT * FROM dedupe_batches WHERE id=? AND tenant_id=?", (batch_id, t)).fetchone()
        if not row:
            raise HTTPException(404, "履歴が見つかりません")
        if row["undone_at"]:
            return {"ok": True, "already": True}
        for e in reversed(json.loads(row["data"])):
            kept = e["kept_id"]
            for rid in e["removed_ids"]:
                db.unmerge(t, rid)
            now_ov = db.get_overrides(t, kept)
            for f in now_ov:
                if f not in e["prev_overrides"]:
                    db.clear_override(t, kept, f)
            for f, v in e["prev_overrides"].items():
                db.set_override(t, kept, f, v)
            db.set_note(t, kept, e["prev_note"])
            for tag in db.get_tags(t, kept):
                if tag not in e["prev_tags"]:
                    db.remove_tag(t, kept, tag)
            with sales._conn() as c:
                for deal_id, old_card in e["deals"]:
                    c.execute("UPDATE deals SET card_id=? WHERE id=? AND tenant_id=?", (old_card, deal_id, t))
                c.commit()
            for p in e["images"]:
                try:
                    os.remove(p)
                except OSError:
                    pass
        with sales._conn() as c:
            c.execute("UPDATE dedupe_batches SET undone_at=? WHERE id=?", (sales._now(), batch_id))
            c.commit()
        return {"ok": True}

    return r
