"""
SQLite DB 操作モジュール (v5)
==============================
- notes, tags, overrides: v4と同じ
- merged_ids: マージ済みペア
- dismissed_dupes: 「別人」と判定済みペア
- email_log: メール送信履歴
"""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

DB_PATH = os.getenv("DB_PATH", "/var/meishi/backend/meishi.db")

EDITABLE_FIELDS = {
    "company", "name", "department_position",
    "email", "address", "mobile", "phone", "website",
    "assigned_to",  # 担当者 (v6.8)
}


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS notes (
            tenant_id TEXT NOT NULL, card_id TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
            PRIMARY KEY (tenant_id, card_id))""")

        c.execute("""CREATE TABLE IF NOT EXISTS tags (
            tenant_id TEXT NOT NULL, card_id TEXT NOT NULL,
            tag TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY (tenant_id, card_id, tag))""")

        c.execute("""CREATE TABLE IF NOT EXISTS overrides (
            tenant_id TEXT NOT NULL, card_id TEXT NOT NULL,
            field TEXT NOT NULL, value TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (tenant_id, card_id, field))""")

        c.execute("""CREATE TABLE IF NOT EXISTS merged_ids (
            tenant_id TEXT NOT NULL,
            removed_card_id TEXT NOT NULL,
            kept_card_id TEXT NOT NULL,
            merged_at TEXT NOT NULL,
            PRIMARY KEY (tenant_id, removed_card_id))""")

        c.execute("""CREATE TABLE IF NOT EXISTS dismissed_dupes (
            tenant_id TEXT NOT NULL,
            card_id_a TEXT NOT NULL,
            card_id_b TEXT NOT NULL,
            dismissed_at TEXT NOT NULL,
            PRIMARY KEY (tenant_id, card_id_a, card_id_b))""")

        c.execute("""CREATE TABLE IF NOT EXISTS email_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            card_id TEXT NOT NULL,
            to_email TEXT NOT NULL,
            subject TEXT NOT NULL,
            status TEXT NOT NULL,
            provider_id TEXT,
            error TEXT,
            sent_at TEXT NOT NULL)""")

        c.execute("""CREATE TABLE IF NOT EXISTS email_presets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            label TEXT NOT NULL,
            local_part TEXT NOT NULL,
            from_name TEXT NOT NULL,
            signature TEXT NOT NULL DEFAULT '',
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL)""")

        # マイグレーション: 既存テーブルに signature 列が無ければ追加
        cols = [r["name"] for r in c.execute("PRAGMA table_info(email_presets)").fetchall()]
        if "signature" not in cols:
            c.execute("ALTER TABLE email_presets ADD COLUMN signature TEXT NOT NULL DEFAULT ''")

        c.execute("CREATE INDEX IF NOT EXISTS idx_tags_tenant_tag ON tags(tenant_id, tag)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_notes_tenant ON notes(tenant_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_overrides_tenant ON overrides(tenant_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_merged_tenant ON merged_ids(tenant_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_dismissed_tenant ON dismissed_dupes(tenant_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_email_log_tenant ON email_log(tenant_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_email_presets_tenant ON email_presets(tenant_id, sort_order)")
        c.commit()


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


# ── メモ ──
def get_note(tenant_id, card_id):
    with _conn() as c:
        row = c.execute("SELECT note FROM notes WHERE tenant_id=? AND card_id=?",
                        (tenant_id, card_id)).fetchone()
        return row["note"] if row else ""


def set_note(tenant_id, card_id, note):
    now = datetime.now().isoformat(timespec="seconds")
    with _conn() as c:
        if note.strip():
            c.execute("""INSERT INTO notes (tenant_id, card_id, note, updated_at)
                VALUES (?,?,?,?)
                ON CONFLICT (tenant_id, card_id) DO UPDATE SET
                note=excluded.note, updated_at=excluded.updated_at""",
                (tenant_id, card_id, note, now))
        else:
            c.execute("DELETE FROM notes WHERE tenant_id=? AND card_id=?",
                      (tenant_id, card_id))
        c.commit()


# ── タグ ──
def get_tags(tenant_id, card_id):
    with _conn() as c:
        rows = c.execute("SELECT tag FROM tags WHERE tenant_id=? AND card_id=? ORDER BY created_at",
                         (tenant_id, card_id)).fetchall()
        return [r["tag"] for r in rows]


def add_tag(tenant_id, card_id, tag):
    tag = tag.strip().lstrip("#")
    if not tag or len(tag) > 40:
        return False
    now = datetime.now().isoformat(timespec="seconds")
    with _conn() as c:
        try:
            c.execute("INSERT INTO tags VALUES (?,?,?,?)",
                      (tenant_id, card_id, tag, now))
            c.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def remove_tag(tenant_id, card_id, tag):
    with _conn() as c:
        c.execute("DELETE FROM tags WHERE tenant_id=? AND card_id=? AND tag=?",
                  (tenant_id, card_id, tag))
        c.commit()


# ── オーバーライド ──
def get_overrides(tenant_id, card_id):
    with _conn() as c:
        rows = c.execute("SELECT field, value FROM overrides WHERE tenant_id=? AND card_id=?",
                         (tenant_id, card_id)).fetchall()
        return {r["field"]: r["value"] for r in rows}


def set_override(tenant_id, card_id, field, value):
    if field not in EDITABLE_FIELDS:
        raise ValueError(f"編集できないフィールド: {field}")
    now = datetime.now().isoformat(timespec="seconds")
    with _conn() as c:
        c.execute("""INSERT INTO overrides (tenant_id, card_id, field, value, updated_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT (tenant_id, card_id, field) DO UPDATE SET
            value=excluded.value, updated_at=excluded.updated_at""",
            (tenant_id, card_id, field, value, now))
        c.commit()


def clear_override(tenant_id, card_id, field):
    with _conn() as c:
        c.execute("DELETE FROM overrides WHERE tenant_id=? AND card_id=? AND field=?",
                  (tenant_id, card_id, field))
        c.commit()


def clear_all_overrides(tenant_id, card_id):
    with _conn() as c:
        c.execute("DELETE FROM overrides WHERE tenant_id=? AND card_id=?",
                  (tenant_id, card_id))
        c.commit()


# ── 重複マージ ──
def get_merged_removed(tenant_id):
    with _conn() as c:
        rows = c.execute("SELECT removed_card_id FROM merged_ids WHERE tenant_id=?",
                         (tenant_id,)).fetchall()
        return {r["removed_card_id"] for r in rows}


def record_merge(tenant_id, removed_id, kept_id):
    now = datetime.now().isoformat(timespec="seconds")
    with _conn() as c:
        c.execute("""INSERT OR REPLACE INTO merged_ids
            (tenant_id, removed_card_id, kept_card_id, merged_at)
            VALUES (?,?,?,?)""", (tenant_id, removed_id, kept_id, now))
        c.commit()


def unmerge(tenant_id, removed_id):
    with _conn() as c:
        c.execute("DELETE FROM merged_ids WHERE tenant_id=? AND removed_card_id=?",
                  (tenant_id, removed_id))
        c.commit()


# ── 「別人」判定 ──
def _sort_pair(a, b):
    return (a, b) if a < b else (b, a)


def is_dismissed(tenant_id, a, b):
    pa, pb = _sort_pair(a, b)
    with _conn() as c:
        row = c.execute("SELECT 1 FROM dismissed_dupes WHERE tenant_id=? AND card_id_a=? AND card_id_b=?",
                        (tenant_id, pa, pb)).fetchone()
        return row is not None


def dismiss_pair(tenant_id, a, b):
    pa, pb = _sort_pair(a, b)
    now = datetime.now().isoformat(timespec="seconds")
    with _conn() as c:
        c.execute("""INSERT OR REPLACE INTO dismissed_dupes
            (tenant_id, card_id_a, card_id_b, dismissed_at)
            VALUES (?,?,?,?)""", (tenant_id, pa, pb, now))
        c.commit()


def get_dismissed_pairs(tenant_id):
    with _conn() as c:
        rows = c.execute("SELECT card_id_a, card_id_b FROM dismissed_dupes WHERE tenant_id=?",
                         (tenant_id,)).fetchall()
        return {(r["card_id_a"], r["card_id_b"]) for r in rows}


# ── 一括取得 ──
def get_all_annotations(tenant_id):
    result = {}
    with _conn() as c:
        for row in c.execute("SELECT card_id, note FROM notes WHERE tenant_id=?", (tenant_id,)):
            result.setdefault(row["card_id"], {"note": "", "tags": [], "overrides": {}})
            result[row["card_id"]]["note"] = row["note"]
        for row in c.execute("SELECT card_id, tag FROM tags WHERE tenant_id=? ORDER BY created_at", (tenant_id,)):
            result.setdefault(row["card_id"], {"note": "", "tags": [], "overrides": {}})
            result[row["card_id"]]["tags"].append(row["tag"])
        for row in c.execute("SELECT card_id, field, value FROM overrides WHERE tenant_id=?", (tenant_id,)):
            result.setdefault(row["card_id"], {"note": "", "tags": [], "overrides": {}})
            result[row["card_id"]]["overrides"][row["field"]] = row["value"]
    return result


def list_all_tags(tenant_id):
    with _conn() as c:
        rows = c.execute("""SELECT tag, COUNT(*) AS count FROM tags
            WHERE tenant_id=? GROUP BY tag ORDER BY count DESC, tag ASC""", (tenant_id,)).fetchall()
        return [{"tag": r["tag"], "count": r["count"]} for r in rows]


# ── メール送信履歴 ──
def log_email(tenant_id, card_id, to_email, subject, status, provider_id=None, error=None):
    now = datetime.now().isoformat(timespec="seconds")
    with _conn() as c:
        c.execute("""INSERT INTO email_log
            (tenant_id, card_id, to_email, subject, status, provider_id, error, sent_at)
            VALUES (?,?,?,?,?,?,?,?)""",
            (tenant_id, card_id, to_email, subject, status, provider_id, error, now))
        c.commit()


def get_email_logs(tenant_id, card_id=None, limit=100):
    with _conn() as c:
        if card_id:
            rows = c.execute("""SELECT * FROM email_log WHERE tenant_id=? AND card_id=?
                ORDER BY sent_at DESC LIMIT ?""", (tenant_id, card_id, limit)).fetchall()
        else:
            rows = c.execute("""SELECT * FROM email_log WHERE tenant_id=?
                ORDER BY sent_at DESC LIMIT ?""", (tenant_id, limit)).fetchall()
        return [dict(r) for r in rows]


# ── メール送信元プリセット ──
def list_email_presets(tenant_id):
    with _conn() as c:
        rows = c.execute("""SELECT id, label, local_part, from_name, signature, sort_order
            FROM email_presets WHERE tenant_id=?
            ORDER BY sort_order ASC, id ASC""", (tenant_id,)).fetchall()
        return [dict(r) for r in rows]


def create_email_preset(tenant_id, label, local_part, from_name, signature=""):
    now = datetime.now().isoformat(timespec="seconds")
    with _conn() as c:
        max_order = c.execute("SELECT COALESCE(MAX(sort_order),0) AS m FROM email_presets WHERE tenant_id=?",
                              (tenant_id,)).fetchone()["m"]
        cur = c.execute("""INSERT INTO email_presets
            (tenant_id, label, local_part, from_name, signature, sort_order, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?)""",
            (tenant_id, label, local_part, from_name, signature, max_order + 1, now, now))
        c.commit()
        return cur.lastrowid


def update_email_preset(tenant_id, preset_id, label, local_part, from_name, signature=""):
    now = datetime.now().isoformat(timespec="seconds")
    with _conn() as c:
        cur = c.execute("""UPDATE email_presets
            SET label=?, local_part=?, from_name=?, signature=?, updated_at=?
            WHERE tenant_id=? AND id=?""",
            (label, local_part, from_name, signature, now, tenant_id, preset_id))
        c.commit()
        return cur.rowcount > 0


def delete_email_preset(tenant_id, preset_id):
    with _conn() as c:
        cur = c.execute("DELETE FROM email_presets WHERE tenant_id=? AND id=?",
                        (tenant_id, preset_id))
        c.commit()
        return cur.rowcount > 0


def purge_card_data(tenant_id, card_ids: list[str]):
    """名刺に関連する全てのSQLiteデータを削除する。
    - notes, tags, overrides: 直接削除
    - merged_ids: removed か kept のどちらかに含まれるエントリを削除
    - dismissed_dupes: a か b のどちらかに含まれるエントリを削除
    - email_log: 紐付く送信履歴も削除
    """
    if not card_ids:
        return
    with _conn() as c:
        placeholders = ",".join("?" for _ in card_ids)
        params = [tenant_id] + list(card_ids)

        c.execute(f"DELETE FROM notes WHERE tenant_id=? AND card_id IN ({placeholders})", params)
        c.execute(f"DELETE FROM tags WHERE tenant_id=? AND card_id IN ({placeholders})", params)
        c.execute(f"DELETE FROM overrides WHERE tenant_id=? AND card_id IN ({placeholders})", params)
        c.execute(f"DELETE FROM email_log WHERE tenant_id=? AND card_id IN ({placeholders})", params)

        # merged_ids: removed_card_id か kept_card_id のどちらかに一致
        c.execute(f"DELETE FROM merged_ids WHERE tenant_id=? AND (removed_card_id IN ({placeholders}) OR kept_card_id IN ({placeholders}))",
                  [tenant_id] + list(card_ids) + list(card_ids))

        # dismissed_dupes: a か b のどちらかに一致
        c.execute(f"DELETE FROM dismissed_dupes WHERE tenant_id=? AND (card_id_a IN ({placeholders}) OR card_id_b IN ({placeholders}))",
                  [tenant_id] + list(card_ids) + list(card_ids))

        c.commit()
