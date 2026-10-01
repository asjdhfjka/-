"""轻量用户、会话与个人记录存储。只依赖 Python 标准库。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path


ROLES = {"student", "reviewer", "admin"}
SUBMISSION_STATUSES = {"submitted", "reviewing", "changes_requested", "approved"}
PBKDF2_ITERATIONS = 260_000


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _hash_password(password: str, salt: bytes | None = None) -> str:
    if not isinstance(password, str) or len(password) < 8:
        raise ValueError("密码至少需要 8 个字符")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_text, expected_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(expected_text.encode("ascii"))
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, binascii.Error):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def public_user(row) -> dict | None:
    if not row:
        return None
    data = dict(row)
    return {
        "id": data["id"],
        "username": data["username"],
        "display_name": data["display_name"],
        "college": data.get("college") or "",
        "role": data["role"],
        "is_active": bool(data["is_active"]),
        "created_at": data["created_at"],
    }


class AuthStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self):
        with self._lock, self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    college TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL CHECK(role IN ('student','reviewer','admin')),
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    expires_at INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
                CREATE TABLE IF NOT EXISTS chat_history (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    question TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    sources_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_chat_user_time ON chat_history(user_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS review_history (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    filename TEXT NOT NULL,
                    material_type TEXT NOT NULL DEFAULT '',
                    outcome TEXT NOT NULL DEFAULT '',
                    total_score INTEGER,
                    result_json TEXT NOT NULL,
                    stored_path TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_review_user_time ON review_history(user_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS material_submissions (
                    id TEXT PRIMARY KEY,
                    review_id TEXT NOT NULL UNIQUE REFERENCES review_history(id) ON DELETE CASCADE,
                    student_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    status TEXT NOT NULL CHECK(status IN ('submitted','reviewing','changes_requested','approved')),
                    reviewer_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                    reviewer_note TEXT NOT NULL DEFAULT '',
                    submitted_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_submission_status_time
                    ON material_submissions(status, updated_at DESC);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(review_history)").fetchall()}
            if "stored_path" not in columns:
                db.execute("ALTER TABLE review_history ADD COLUMN stored_path TEXT NOT NULL DEFAULT ''")

    def create_user(self, username: str, password: str, display_name: str,
                    role: str = "student", college: str = "") -> dict:
        username = str(username or "").strip()
        display_name = str(display_name or "").strip()
        role = str(role or "student").strip().lower()
        if not username or len(username) > 50:
            raise ValueError("用户名不能为空且不能超过 50 个字符")
        if not display_name or len(display_name) > 50:
            raise ValueError("显示姓名不能为空且不能超过 50 个字符")
        if role not in ROLES:
            raise ValueError("用户角色不合法")
        user_id = uuid.uuid4().hex
        with self._lock, self._connect() as db:
            try:
                db.execute(
                    "INSERT INTO users(id,username,password_hash,display_name,college,role,is_active,created_at) "
                    "VALUES(?,?,?,?,?,?,1,?)",
                    (user_id, username, _hash_password(password), display_name,
                     str(college or "").strip()[:100], role, _now_iso()),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("用户名已经存在") from exc
            row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return public_user(row)

    def bootstrap_demo_users(self, password: str) -> list[dict]:
        templates = (
            ("student", "学生演示账号", "student", "计算机学院"),
            ("reviewer", "审核负责人", "reviewer", "学生工作处"),
            ("admin", "系统管理员", "admin", "信息中心"),
        )
        created = []
        for username, name, role, college in templates:
            if self.get_user_by_username(username):
                continue
            created.append(self.create_user(username, password, name, role, college))
        return created

    def get_user_by_username(self, username: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE",
                             (str(username or "").strip(),)).fetchone()
        return dict(row) if row else None

    def authenticate(self, username: str, password: str) -> dict | None:
        row = self.get_user_by_username(username)
        if not row or not row["is_active"] or not _verify_password(str(password or ""), row["password_hash"]):
            return None
        return public_user(row)

    def create_session(self, user_id: str, days: int = 7) -> str:
        token = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + max(1, int(days)) * 86400
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM sessions WHERE expires_at <= ?", (int(time.time()),))
            db.execute(
                "INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                (_token_hash(token), user_id, expires_at, _now_iso()),
            )
        return token

    def user_for_session(self, token: str) -> dict | None:
        if not token:
            return None
        now = int(time.time())
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
                "WHERE s.token_hash=? AND s.expires_at>? AND u.is_active=1",
                (_token_hash(token), now),
            ).fetchone()
            if row is None:
                db.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))
        return public_user(row)

    def delete_session(self, token: str):
        if not token:
            return
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))

    def list_users(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM users ORDER BY created_at").fetchall()
        return [public_user(row) for row in rows]

    def set_user_active(self, user_id: str, active: bool) -> dict | None:
        with self._lock, self._connect() as db:
            db.execute("UPDATE users SET is_active=? WHERE id=?", (1 if active else 0, user_id))
            if not active:
                db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return public_user(row)

    def record_chat(self, user_id: str, question: str, answer: str, sources: list) -> str:
        record_id = uuid.uuid4().hex
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO chat_history(id,user_id,question,answer,sources_json,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (record_id, user_id, str(question)[:2000], str(answer)[:20000],
                 json.dumps(sources or [], ensure_ascii=False), _now_iso()),
            )
        return record_id

    def recent_chats(self, user_id: str, limit: int = 20) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id,question,answer,sources_json,created_at FROM chat_history "
                "WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                (user_id, max(1, min(int(limit), 100))),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["sources"] = json.loads(item.pop("sources_json"))
            items.append(item)
        return items

    def record_review(self, user_id: str, filename: str, result: dict,
                      stored_path: str = "") -> str:
        record_id = uuid.uuid4().hex
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO review_history(id,user_id,filename,material_type,outcome,total_score,result_json,stored_path,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (record_id, user_id, str(filename)[:255], str(result.get("材料类型") or "")[:100],
                 str(result.get("审查结论") or "")[:50], result.get("总评分"),
                 json.dumps(result, ensure_ascii=False), str(stored_path or ""), _now_iso()),
            )
        return record_id

    def recent_reviews(self, user_id: str, limit: int = 20) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT r.id,r.filename,r.material_type,r.outcome,r.total_score,r.result_json,r.created_at,"
                "s.id AS submission_id,s.status AS submission_status,s.reviewer_note,s.updated_at "
                "FROM review_history r LEFT JOIN material_submissions s ON s.review_id=r.id "
                "WHERE r.user_id=? ORDER BY r.created_at DESC LIMIT ?",
                (user_id, max(1, min(int(limit), 100))),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["result"] = json.loads(item.pop("result_json"))
            item["result"]["记录ID"] = item["id"]
            items.append(item)
        return items

    def get_review(self, review_id: str, user_id: str | None = None) -> dict | None:
        sql = (
            "SELECT r.*,s.id AS submission_id,s.status AS submission_status,"
            "s.reviewer_note,s.updated_at FROM review_history r "
            "LEFT JOIN material_submissions s ON s.review_id=r.id WHERE r.id=?"
        )
        params: list = [review_id]
        if user_id is not None:
            sql += " AND r.user_id=?"
            params.append(user_id)
        with self._connect() as db:
            row = db.execute(sql, params).fetchone()
        if not row:
            return None
        item = dict(row)
        item["result"] = json.loads(item.pop("result_json"))
        item["result"]["记录ID"] = item["id"]
        return item

    def submit_review(self, review_id: str, student_id: str) -> dict:
        now = _now_iso()
        with self._lock, self._connect() as db:
            review = db.execute(
                "SELECT id,stored_path FROM review_history WHERE id=? AND user_id=?",
                (review_id, student_id),
            ).fetchone()
            if not review:
                raise ValueError("审查记录不存在或不属于当前用户")
            if not review["stored_path"]:
                raise ValueError("该历史记录没有归档原文件，请重新预审后再提交")
            existing = db.execute(
                "SELECT id FROM material_submissions WHERE review_id=?", (review_id,)
            ).fetchone()
            if existing:
                submission_id = existing["id"]
                db.execute(
                    "UPDATE material_submissions SET status='submitted',reviewer_id=NULL,"
                    "reviewer_note='',updated_at=? WHERE id=?",
                    (now, submission_id),
                )
            else:
                submission_id = uuid.uuid4().hex
                db.execute(
                    "INSERT INTO material_submissions(id,review_id,student_id,status,submitted_at,updated_at) "
                    "VALUES(?,?,?,'submitted',?,?)",
                    (submission_id, review_id, student_id, now, now),
                )
        return self.get_submission(submission_id)

    def get_submission(self, submission_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT s.*,r.filename,r.material_type,r.outcome,r.total_score,r.result_json,"
                "r.stored_path,u.display_name AS student_name,u.college,"
                "rv.display_name AS reviewer_name "
                "FROM material_submissions s JOIN review_history r ON r.id=s.review_id "
                "JOIN users u ON u.id=s.student_id "
                "LEFT JOIN users rv ON rv.id=s.reviewer_id WHERE s.id=?",
                (submission_id,),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["result"] = json.loads(item.pop("result_json"))
        item["result"]["记录ID"] = item["review_id"]
        item["has_file"] = bool(item.pop("stored_path", ""))
        return item

    def list_submissions(self, student_id: str | None = None, limit: int = 100) -> list[dict]:
        sql = (
            "SELECT s.id,s.review_id,s.student_id,s.status,s.reviewer_note,s.submitted_at,s.updated_at,"
            "r.filename,r.material_type,r.outcome,r.total_score,"
            "CASE WHEN r.stored_path<>'' THEN 1 ELSE 0 END AS has_file,"
            "u.display_name AS student_name,u.college,rv.display_name AS reviewer_name "
            "FROM material_submissions s JOIN review_history r ON r.id=s.review_id "
            "JOIN users u ON u.id=s.student_id LEFT JOIN users rv ON rv.id=s.reviewer_id"
        )
        params: list = []
        if student_id is not None:
            sql += " WHERE s.student_id=?"
            params.append(student_id)
        sql += " ORDER BY CASE s.status WHEN 'submitted' THEN 0 WHEN 'reviewing' THEN 1 "
        sql += "WHEN 'changes_requested' THEN 2 ELSE 3 END,s.updated_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 200)))
        with self._connect() as db:
            rows = db.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def update_submission(self, submission_id: str, status: str, reviewer_id: str,
                          note: str = "") -> dict | None:
        status = str(status or "").strip()
        if status not in SUBMISSION_STATUSES:
            raise ValueError("材料状态不合法")
        with self._lock, self._connect() as db:
            existing = db.execute(
                "SELECT id FROM material_submissions WHERE id=?", (submission_id,)
            ).fetchone()
            if not existing:
                return None
            db.execute(
                "UPDATE material_submissions SET status=?,reviewer_id=?,reviewer_note=?,updated_at=? "
                "WHERE id=?",
                (status, reviewer_id, str(note or "").strip()[:2000], _now_iso(), submission_id),
            )
        return self.get_submission(submission_id)
