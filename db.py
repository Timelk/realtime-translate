"""SQLite + 简易认证 (scrypt + session cookie)

无第三方依赖, 全部用 Python 标准库.
"""
import sqlite3
import time
import secrets
import hashlib
import json
import os
from contextlib import contextmanager
from typing import Optional

DB_PATH = os.getenv("DB_PATH", "app.db")
SESSION_TTL = 30 * 24 * 3600   # 30 天


@contextmanager
def conn():
    """每次调用开一个新 connection — WAL 模式支持并发读+单 writer, 无需 Python 层锁"""
    c = sqlite3.connect(DB_PATH, timeout=5)
    c.execute("PRAGMA foreign_keys=ON")
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    with conn() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            nickname TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS recordings (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            created_at REAL NOT NULL,
            entries_json TEXT NOT NULL DEFAULT '[]',
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
        CREATE INDEX IF NOT EXISTS idx_recordings_user ON recordings(user_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS rooms (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            host_user_id INTEGER NOT NULL,
            created_at REAL NOT NULL,
            closed_at REAL,
            FOREIGN KEY (host_user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS room_members (
            room_code TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            nickname TEXT NOT NULL,
            target_lang TEXT NOT NULL,
            color INTEGER NOT NULL DEFAULT 0,
            joined_at REAL NOT NULL,
            left_at REAL,
            PRIMARY KEY (room_code, user_id),
            FOREIGN KEY (room_code) REFERENCES rooms(code) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS room_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_code TEXT NOT NULL,
            speaker_user_id INTEGER NOT NULL,
            speaker_name TEXT NOT NULL,
            src_lang TEXT,
            src TEXT,
            translations_json TEXT NOT NULL DEFAULT '{}',
            ts REAL NOT NULL,
            FOREIGN KEY (room_code) REFERENCES rooms(code) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_room_messages ON room_messages(room_code, ts);
        CREATE INDEX IF NOT EXISTS idx_room_members_user ON room_members(user_id);
        """)


# ============================================================
# 密码哈希
# ============================================================
def hash_password(pwd: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pwd.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return salt.hex() + ":" + h.hex()


def verify_password(pwd: str, hashed: str) -> bool:
    try:
        salt_hex, h_hex = hashed.split(":")
        salt = bytes.fromhex(salt_hex)
        h = hashlib.scrypt(pwd.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
        return secrets.compare_digest(h.hex(), h_hex)
    except Exception:
        return False


# ============================================================
# 用户
# ============================================================
def create_user(email: str, password: str, nickname: str) -> Optional[dict]:
    email = email.strip().lower()
    nickname = nickname.strip()[:30]
    if not email or not password or not nickname:
        return None
    try:
        with conn() as c:
            cur = c.execute(
                "INSERT INTO users (email, nickname, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (email, nickname, hash_password(password), time.time()),
            )
            uid = cur.lastrowid
        return get_user(uid)
    except sqlite3.IntegrityError:
        return None


def get_user(user_id: int) -> Optional[dict]:
    with conn() as c:
        row = c.execute("SELECT id, email, nickname, created_at FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def find_user_by_email(email: str) -> Optional[dict]:
    with conn() as c:
        row = c.execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()
        return dict(row) if row else None


def authenticate(email: str, password: str) -> Optional[dict]:
    u = find_user_by_email(email)
    if not u: return None
    if not verify_password(password, u["password_hash"]): return None
    return {"id": u["id"], "email": u["email"], "nickname": u["nickname"]}


# ============================================================
# Session
# ============================================================
def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with conn() as c:
        c.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, user_id, now, now + SESSION_TTL),
        )
    return token


def get_session_user(token: str) -> Optional[dict]:
    if not token: return None
    with conn() as c:
        row = c.execute("""
            SELECT u.id, u.email, u.nickname FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token = ? AND s.expires_at > ?
        """, (token, time.time())).fetchone()
        return dict(row) if row else None


def revoke_session(token: str):
    with conn() as c:
        c.execute("DELETE FROM sessions WHERE token = ?", (token,))


def insert_recording(rec_id: str, user_id: int, kind: str, name: str, created_at: float):
    with conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO recordings (id, user_id, kind, name, created_at, entries_json) VALUES (?, ?, ?, ?, ?, '[]')",
            (rec_id, user_id, kind, name, created_at),
        )


def append_recording_entry(rec_id: str, entry: dict):
    """读 entries JSON, append, 写回."""
    with conn() as c:
        row = c.execute("SELECT entries_json FROM recordings WHERE id = ?", (rec_id,)).fetchone()
        if not row: return
        try:
            entries = json.loads(row["entries_json"])
        except Exception:
            entries = []
        entries.append(entry)
        c.execute("UPDATE recordings SET entries_json = ? WHERE id = ?",
                  (json.dumps(entries, ensure_ascii=False), rec_id))


def list_recordings_for_user(user_id: int) -> list:
    with conn() as c:
        rows = c.execute(
            "SELECT id, kind, name, created_at, entries_json FROM recordings WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    out = []
    for r in rows:
        try:
            count = len(json.loads(r["entries_json"]))
        except Exception:
            count = 0
        out.append({
            "id": r["id"], "kind": r["kind"], "name": r["name"],
            "created_at": r["created_at"], "count": count,
        })
    return out


def get_recording(rec_id: str, user_id: int) -> Optional[dict]:
    with conn() as c:
        row = c.execute(
            "SELECT id, user_id, kind, name, created_at, entries_json FROM recordings WHERE id = ? AND user_id = ?",
            (rec_id, user_id),
        ).fetchone()
    if not row: return None
    d = dict(row)
    try:
        d["entries"] = json.loads(d.pop("entries_json"))
    except Exception:
        d["entries"] = []
    return d


def delete_recording(rec_id: str, user_id: int) -> bool:
    with conn() as c:
        cur = c.execute("DELETE FROM recordings WHERE id = ? AND user_id = ?", (rec_id, user_id))
        return cur.rowcount > 0


# ============================================================
# Rooms (群组持久化)
# ============================================================
def room_create(code: str, name: str, host_user_id: int) -> dict:
    now = time.time()
    with conn() as c:
        c.execute("INSERT INTO rooms (code, name, host_user_id, created_at) VALUES (?, ?, ?, ?)",
                  (code, name, host_user_id, now))
    return {"code": code, "name": name, "host_user_id": host_user_id, "created_at": now, "closed_at": None}


def room_get(code: str) -> Optional[dict]:
    with conn() as c:
        row = c.execute("SELECT code, name, host_user_id, created_at, closed_at FROM rooms WHERE code = ?",
                        (code,)).fetchone()
        return dict(row) if row else None


def room_close(code: str):
    with conn() as c:
        c.execute("UPDATE rooms SET closed_at = ? WHERE code = ? AND closed_at IS NULL",
                  (time.time(), code))


def room_add_member(code: str, user_id: int, nickname: str, target_lang: str, color: int):
    with conn() as c:
        c.execute("""
            INSERT INTO room_members (room_code, user_id, nickname, target_lang, color, joined_at, left_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(room_code, user_id) DO UPDATE SET
                nickname = excluded.nickname,
                target_lang = excluded.target_lang,
                joined_at = excluded.joined_at,
                left_at = NULL
        """, (code, user_id, nickname, target_lang, color, time.time()))


def room_mark_member_left(code: str, user_id: int):
    with conn() as c:
        c.execute("UPDATE room_members SET left_at = ? WHERE room_code = ? AND user_id = ?",
                  (time.time(), code, user_id))


def room_list_members(code: str, active_only: bool = True) -> list:
    sql = "SELECT user_id, nickname, target_lang, color, joined_at, left_at FROM room_members WHERE room_code = ?"
    if active_only:
        sql += " AND left_at IS NULL"
    sql += " ORDER BY joined_at"
    with conn() as c:
        return [dict(r) for r in c.execute(sql, (code,)).fetchall()]


def room_add_message(code: str, speaker_user_id: int, speaker_name: str,
                     src_lang: str, src: str, translations: dict):
    with conn() as c:
        c.execute("""
            INSERT INTO room_messages (room_code, speaker_user_id, speaker_name, src_lang, src, translations_json, ts)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (code, speaker_user_id, speaker_name, src_lang, src,
              json.dumps(translations, ensure_ascii=False), time.time()))


def room_list_messages(code: str) -> list:
    with conn() as c:
        rows = c.execute("""
            SELECT speaker_user_id, speaker_name, src_lang, src, translations_json, ts
            FROM room_messages WHERE room_code = ? ORDER BY ts
        """, (code,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["translations"] = json.loads(d.pop("translations_json"))
        except Exception:
            d["translations"] = {}
        out.append(d)
    return out


def rooms_for_user(user_id: int) -> list:
    """用户参与过 (含 host) 的所有房间, 按时间倒序"""
    with conn() as c:
        rows = c.execute("""
            SELECT DISTINCT r.code, r.name, r.host_user_id, r.created_at, r.closed_at,
                   (SELECT COUNT(*) FROM room_messages m WHERE m.room_code = r.code) AS msg_count,
                   (SELECT COUNT(*) FROM room_members m2 WHERE m2.room_code = r.code) AS member_count
            FROM rooms r
            LEFT JOIN room_members rm ON rm.room_code = r.code
            WHERE r.host_user_id = ? OR rm.user_id = ?
            ORDER BY r.created_at DESC
        """, (user_id, user_id)).fetchall()
        return [dict(r) for r in rows]


init()
