"""Tests for role-based access control: the reporting-hierarchy walk in
backend/access_control.py and the file-attribution scoping it drives.

Run with: python -m pytest tests/test_access_control.py -v

Each test gets its own throwaway SQLite file (via the fresh_db fixture) so
these never touch the real db/users.db.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backend.db as db
from backend.access_control import get_accessible_filenames, get_accessible_user_ids


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test_users.db")
    db.init_db()
    return db


def _make_user(fresh_db, email, name, role, reports_to_id=None):
    row = fresh_db.create_user(email, name, password_hash="x", role=role, reports_to_id=reports_to_id)
    return dict(row)


def test_manager_sees_whole_branch(fresh_db):
    manager = _make_user(fresh_db, "m@x.com", "Manager", "manager")
    s1 = _make_user(fresh_db, "s1@x.com", "SE1", "senior_executive", manager["id"])
    s2 = _make_user(fresh_db, "s2@x.com", "SE2", "senior_executive", manager["id"])
    e1 = _make_user(fresh_db, "e1@x.com", "E1", "executive", s1["id"])
    e2 = _make_user(fresh_db, "e2@x.com", "E2", "executive", s2["id"])

    accessible = get_accessible_user_ids(manager)
    assert accessible == {manager["id"], s1["id"], s2["id"], e1["id"], e2["id"]}


def test_senior_executive_sees_own_branch_only(fresh_db):
    manager = _make_user(fresh_db, "m@x.com", "Manager", "manager")
    s1 = _make_user(fresh_db, "s1@x.com", "SE1", "senior_executive", manager["id"])
    s2 = _make_user(fresh_db, "s2@x.com", "SE2", "senior_executive", manager["id"])
    e1 = _make_user(fresh_db, "e1@x.com", "E1", "executive", s1["id"])
    e2 = _make_user(fresh_db, "e2@x.com", "E2", "executive", s2["id"])

    accessible = get_accessible_user_ids(s1)
    assert accessible == {s1["id"], e1["id"]}
    assert manager["id"] not in accessible  # can't see the manager
    assert s2["id"] not in accessible  # can't see a sibling branch's owner
    assert e2["id"] not in accessible  # ...or its executive


def test_executive_sees_only_self(fresh_db):
    manager = _make_user(fresh_db, "m@x.com", "Manager", "manager")
    s1 = _make_user(fresh_db, "s1@x.com", "SE1", "senior_executive", manager["id"])
    e1 = _make_user(fresh_db, "e1@x.com", "E1", "executive", s1["id"])
    e2 = _make_user(fresh_db, "e2@x.com", "E2 (peer)", "executive", s1["id"])

    accessible = get_accessible_user_ids(e1)
    assert accessible == {e1["id"]}
    assert e2["id"] not in accessible  # can't see a peer
    assert s1["id"] not in accessible
    assert manager["id"] not in accessible


def test_user_with_no_role_sees_only_self(fresh_db):
    user = _make_user(fresh_db, "u@x.com", "Unassigned", None)
    assert get_accessible_user_ids(user) == {user["id"]}


def test_reassignment_takes_effect_immediately(fresh_db):
    manager = _make_user(fresh_db, "m@x.com", "Manager", "manager")
    s1 = _make_user(fresh_db, "s1@x.com", "SE1", "senior_executive", manager["id"])
    s2 = _make_user(fresh_db, "s2@x.com", "SE2", "senior_executive", manager["id"])
    e1 = _make_user(fresh_db, "e1@x.com", "E1", "executive", s1["id"])

    assert e1["id"] in get_accessible_user_ids(s1)
    assert e1["id"] not in get_accessible_user_ids(s2)

    fresh_db.set_role(e1["id"], "executive", s2["id"])  # reassign E1 to S2

    assert e1["id"] not in get_accessible_user_ids(s1)
    assert e1["id"] in get_accessible_user_ids(s2)


def test_orphaned_report_is_safe_not_a_leak(fresh_db):
    """If a manager/senior exec is deleted, their old reports still point
    at a now-nonexistent id. That must never grant anyone extra access -
    it just makes that person invisible to everyone above them until
    reassigned. See ACCESS-CONTROL.md's "Deleted or unassigned manager"."""
    manager = _make_user(fresh_db, "m@x.com", "Manager", "manager")
    s1 = _make_user(fresh_db, "s1@x.com", "SE1", "senior_executive", manager["id"])
    e1 = _make_user(fresh_db, "e1@x.com", "E1", "executive", s1["id"])

    with fresh_db._connect() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (manager["id"],))

    s1_row = dict(fresh_db.get_user_by_id(s1["id"]))
    # S1 keeps their own branch even though their manager is gone.
    assert get_accessible_user_ids(s1_row) == {s1["id"], e1["id"]}


def test_accessible_filenames_scoped_by_uploader_branch(fresh_db):
    manager = _make_user(fresh_db, "m@x.com", "Manager", "manager")
    s1 = _make_user(fresh_db, "s1@x.com", "SE1", "senior_executive", manager["id"])
    s2 = _make_user(fresh_db, "s2@x.com", "SE2", "senior_executive", manager["id"])
    e1 = _make_user(fresh_db, "e1@x.com", "E1", "executive", s1["id"])

    file_uploads = {
        "manager_file.xlsx": manager["id"],
        "s1_file.xlsx": s1["id"],
        "s2_file.xlsx": s2["id"],
        "e1_file.xlsx": e1["id"],
        "legacy_file.xlsx": None,  # predates upload attribution
    }
    all_files = list(file_uploads.keys())

    manager_files = get_accessible_filenames(manager, file_uploads, all_files)
    assert manager_files == set(all_files)  # everything, including legacy data

    s1_files = get_accessible_filenames(s1, file_uploads, all_files)
    assert s1_files == {"s1_file.xlsx", "e1_file.xlsx"}
    assert "manager_file.xlsx" not in s1_files
    assert "s2_file.xlsx" not in s1_files
    assert "legacy_file.xlsx" not in s1_files  # unattributed data is manager-only

    e1_files = get_accessible_filenames(e1, file_uploads, all_files)
    assert e1_files == {"e1_file.xlsx"}
