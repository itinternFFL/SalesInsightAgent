"""Local SQLite store: user accounts (both email/password and Microsoft SSO
identities, unified into one table) plus which user uploaded which sales
report file - the basis for role-based access control. See
ACCESS-CONTROL.md for the hierarchy model this supports.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "db" / "users.db"

ROLES = {"manager", "senior_executive", "executive"}


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                password_hash TEXT,
                role TEXT,
                reports_to_id INTEGER REFERENCES users(id),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS file_uploads (
                filename TEXT PRIMARY KEY,
                uploaded_by_id INTEGER REFERENCES users(id),
                uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


@contextmanager
def _connect(db_path: Path = None):
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# --- users -------------------------------------------------------------


def get_user_by_email(email: str) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE email = ?", (email.lower(),)
        ).fetchone()


def get_user_by_id(user_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def create_user(
    email: str,
    name: str,
    password_hash: str | None,
    role: str | None = None,
    reports_to_id: int | None = None,
) -> sqlite3.Row:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO users (email, name, password_hash, role, reports_to_id)
               VALUES (?, ?, ?, ?, ?)""",
            (email.lower(), name, password_hash, role, reports_to_id),
        )
        return conn.execute(
            "SELECT * FROM users WHERE email = ?", (email.lower(),)
        ).fetchone()


def get_or_create_sso_user(email: str, name: str) -> sqlite3.Row:
    """Microsoft SSO never sets a password or a role - a first-time SSO user
    lands with role=NULL (see backend/access_control.py's
    require_role_assigned) until they complete their profile via
    POST /auth/complete-profile."""
    existing = get_user_by_email(email)
    if existing is not None:
        return existing
    return create_user(email, name, password_hash=None)


def set_role(user_id: int, role: str, reports_to_id: int | None) -> sqlite3.Row:
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET role = ?, reports_to_id = ? WHERE id = ?",
            (role, reports_to_id, user_id),
        )
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def list_users_by_role(role: str) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            "SELECT id, name, email, reports_to_id FROM users WHERE role = ? ORDER BY name",
            (role,),
        ).fetchall()


def list_direct_reports(user_ids: list[int]) -> list[int]:
    """IDs of every user whose reports_to_id is one of user_ids."""
    if not user_ids:
        return []
    with _connect() as conn:
        placeholders = ",".join("?" for _ in user_ids)
        rows = conn.execute(
            f"SELECT id FROM users WHERE reports_to_id IN ({placeholders})",
            user_ids,
        ).fetchall()
        return [r["id"] for r in rows]


# --- file uploads (RBAC data-ownership basis) ---------------------------


def record_file_upload(filename: str, uploaded_by_id: int):
    """Upsert - re-uploading/replacing a file re-attributes it to whoever
    just uploaded it."""
    with _connect() as conn:
        conn.execute(
            """INSERT INTO file_uploads (filename, uploaded_by_id)
               VALUES (?, ?)
               ON CONFLICT(filename) DO UPDATE SET
                 uploaded_by_id = excluded.uploaded_by_id,
                 uploaded_at = CURRENT_TIMESTAMP""",
            (filename, uploaded_by_id),
        )


def get_uploader_id(filename: str) -> int | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT uploaded_by_id FROM file_uploads WHERE filename = ?", (filename,)
        ).fetchone()
        return row["uploaded_by_id"] if row else None


def list_file_uploads() -> dict[str, int | None]:
    """filename -> uploaded_by_id (None if the file predates upload
    attribution and was never re-uploaded since)."""
    with _connect() as conn:
        rows = conn.execute("SELECT filename, uploaded_by_id FROM file_uploads").fetchall()
        return {r["filename"]: r["uploaded_by_id"] for r in rows}
