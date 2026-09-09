"""Tests for role-based access control: the reporting-hierarchy walk in
backend/access_control.py and the file-attribution scoping it drives.

Run with: python -m pytest tests/test_access_control.py -v

Each test gets its own throwaway SQLite file (via the fresh_db fixture) so
these never touch the real db/users.db.
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backend.db as db
from backend.access_control import (
    collect_entity_values,
    employee_folder_name,
    find_out_of_scope_entity,
    find_owning_employee_folder,
    get_accessible_filenames,
    get_accessible_user_ids,
)


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


def _write(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("dummy")


def test_accessible_filenames_scoped_by_employee_folder(fresh_db, tmp_path):
    manager = _make_user(fresh_db, "m@x.com", "Manager", "manager")
    s1 = _make_user(fresh_db, "s1@x.com", "SE1", "senior_executive", manager["id"])
    s2 = _make_user(fresh_db, "s2@x.com", "SE2", "senior_executive", manager["id"])
    e1 = _make_user(fresh_db, "e1@x.com", "E1", "executive", s1["id"])

    data_dir = tmp_path / "data"
    _write(data_dir / "legacy_file.xlsx")  # predates per-employee folders
    _write(data_dir / "employees" / employee_folder_name(manager) / "manager_file.xlsx")
    _write(data_dir / "employees" / employee_folder_name(s1) / "s1_file.xlsx")
    _write(data_dir / "employees" / employee_folder_name(s2) / "s2_file.xlsx")
    _write(data_dir / "employees" / employee_folder_name(e1) / "e1_file.xlsx")

    all_files = {"legacy_file.xlsx", "manager_file.xlsx", "s1_file.xlsx", "s2_file.xlsx", "e1_file.xlsx"}

    manager_files = get_accessible_filenames(manager, data_dir)
    assert manager_files == all_files  # everything, including legacy data

    s1_files = get_accessible_filenames(s1, data_dir)
    assert s1_files == {"s1_file.xlsx", "e1_file.xlsx"}
    assert "manager_file.xlsx" not in s1_files
    assert "s2_file.xlsx" not in s1_files
    assert "legacy_file.xlsx" not in s1_files  # unattributed data is manager-only

    e1_files = get_accessible_filenames(e1, data_dir)
    assert e1_files == {"e1_file.xlsx"}


def test_accessible_filenames_reflects_folder_changes_live(fresh_db, tmp_path):
    """No caching anywhere in this path - moving a file between employee
    folders (e.g. the equivalent of a re-upload/re-attribution) changes
    who can see it on the very next call, same as a database update would
    have before folders became the source of truth."""
    manager = _make_user(fresh_db, "m@x.com", "Manager", "manager")
    s1 = _make_user(fresh_db, "s1@x.com", "SE1", "senior_executive", manager["id"])
    s2 = _make_user(fresh_db, "s2@x.com", "SE2", "senior_executive", manager["id"])

    data_dir = tmp_path / "data"
    s1_path = data_dir / "employees" / employee_folder_name(s1) / "shared.xlsx"
    _write(s1_path)

    assert "shared.xlsx" in get_accessible_filenames(s1, data_dir)
    assert "shared.xlsx" not in get_accessible_filenames(s2, data_dir)

    s2_path = data_dir / "employees" / employee_folder_name(s2) / "shared.xlsx"
    s2_path.parent.mkdir(parents=True, exist_ok=True)
    s1_path.rename(s2_path)

    assert "shared.xlsx" not in get_accessible_filenames(s1, data_dir)
    assert "shared.xlsx" in get_accessible_filenames(s2, data_dir)


def test_find_owning_employee_folder(fresh_db, tmp_path):
    s1 = _make_user(fresh_db, "s1@x.com", "SE1", "senior_executive", None)
    data_dir = tmp_path / "data"
    _write(data_dir / "employees" / employee_folder_name(s1) / "s1_file.xlsx")
    _write(data_dir / "legacy_file.xlsx")

    assert find_owning_employee_folder("s1_file.xlsx", data_dir) == employee_folder_name(s1)
    assert find_owning_employee_folder("legacy_file.xlsx", data_dir) is None
    assert find_owning_employee_folder("nonexistent.xlsx", data_dir) is None


def _make_full_and_scoped_values():
    full_df = pd.DataFrame({
        "brand": ["BudgetPlan", "PowerBI", "Coated"],
        "customer_name": ["Finance - Budgeting Dept", "Analytics - PowerBI Team", "Big Retailer Ltd"],
        "mat_name": ["Budget Plan Pack", "PowerBI Dashboard Pack", "Coated Flakes Pack"],
        "channel": ["Modern Trade", "Modern Trade", "GT"],
        "sale_type": ["Credit Sale", "Cash Sale", "Credit Sale"],
    })
    # Scoped view only contains the PowerBI row - as if this user can only
    # see the file that row came from.
    scoped_df = full_df.iloc[[1]].reset_index(drop=True)
    return collect_entity_values(full_df), collect_entity_values(scoped_df)


def test_out_of_scope_entity_detected_for_named_brand():
    full_values, scoped_values = _make_full_and_scoped_values()
    result = find_out_of_scope_entity(
        "What are the total net sales for BudgetPlan?", full_values, scoped_values
    )
    assert result == "BudgetPlan"


def test_in_scope_entity_not_flagged():
    full_values, scoped_values = _make_full_and_scoped_values()
    result = find_out_of_scope_entity(
        "What are the total net sales for PowerBI?", full_values, scoped_values
    )
    assert result is None


def test_no_entity_mentioned_not_flagged():
    full_values, scoped_values = _make_full_and_scoped_values()
    result = find_out_of_scope_entity(
        "What is the grand total across all months?", full_values, scoped_values
    )
    assert result is None


def test_out_of_scope_customer_name_detected():
    full_values, scoped_values = _make_full_and_scoped_values()
    result = find_out_of_scope_entity(
        "How much did Big Retailer Ltd buy?", full_values, scoped_values
    )
    assert result == "Big Retailer Ltd"


def test_short_generic_values_not_flagged():
    # "GT" (channel) and short sale-type-ish words are below the minimum
    # length guard - shouldn't false-positive on incidental substrings.
    full_values, scoped_values = _make_full_and_scoped_values()
    result = find_out_of_scope_entity(
        "What's a good strategy going forward?", full_values, scoped_values
    )
    assert result is None


def test_collect_entity_values_matches_original_dataframe():
    full_values, _ = _make_full_and_scoped_values()
    assert full_values["brand"] == {"BudgetPlan", "PowerBI", "Coated"}
    assert full_values["sale_type"] == {"Credit Sale", "Cash Sale"}
