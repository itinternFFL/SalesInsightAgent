"""Role-based access control: who can see whose uploaded sales data.

Hierarchy (top to bottom): manager -> senior_executive -> executive. A
user's visibility is scoped to their own reporting branch, walked downward
from themselves - see ACCESS-CONTROL.md for the full model, the
"unattributed legacy data" policy, and the deleted/unassigned-manager edge
case.

File OWNERSHIP is determined by the filesystem, not the database: each
employee's uploads live in data/employees/<name>/ (see
ACCESS-CONTROL.md's "On-disk layout"), and that folder location is what
get_accessible_filenames() and find_owning_employee_folder() read - not
backend/db.py's file_uploads table, which exists only as an upload-time
audit log now (who/when), not an access-control source of truth. Only the
REPORTING HIERARCHY (who reports to whom) still comes from the database,
since that relationship has no filesystem representation.

Nothing here is cached across requests: every call re-reads the users
table and re-lists the relevant folders, so a hierarchy change
(reassigning someone to a different manager) or a new upload is reflected
on the very next request, for every affected user, with no invalidation
step needed. The only cache backend/main.py keeps is on the expensive
part (embedding a filtered chunk index), keyed by the resulting
accessible-filenames set.

Queries are answered ONLY from the asker's own scoped data - there is
deliberately no separate check anywhere that compares a question against
data outside that scope, even just to decide whether to refuse faster.
An earlier version did (find_out_of_scope_entity, since removed): it
matched question text against known values from the WHOLE dataset to
short-circuit an out-of-scope question before running retrieval. Removed
because that still means comparing against data outside the asker's
authorization, in principle, even though no row content was ever exposed
- see ACCESS-CONTROL.md's "Queries never reference data outside the
asker's scope" for the reasoning and what was given up (a ~400ms fast
refusal) to keep that property absolute.
"""

import re
from pathlib import Path

from fastapi import Depends, HTTPException

from backend.auth import get_current_user
from backend.db import get_user_by_id, list_direct_reports


def get_accessible_user_ids(user_row) -> set[int]:
    """Self, plus every user reachable by walking down the reports_to_id
    chain (not just direct reports) - i.e. the user's own branch only.

    role is None for an account that hasn't completed profile setup yet -
    treated the same as "executive" (self only) as a safe default, though
    require_role_assigned (below) blocks data access entirely until it's
    set, so this path is mostly defensive.
    """
    uid = user_row["id"]
    role = user_row["role"]

    if role != "manager" and role != "senior_executive":
        return {uid}

    accessible = {uid}
    frontier = [uid]
    while frontier:
        children = list_direct_reports(frontier)
        new_children = [c for c in children if c not in accessible]
        if not new_children:
            break
        accessible.update(new_children)
        frontier = new_children
    return accessible


def employee_folder_name(user_row) -> str:
    """Filesystem-safe subfolder name for this user's own uploads, under
    data/employees/<name>/ - falls back to a user-id-based name if the
    display name sanitizes to nothing (e.g. all-invalid characters). The
    single definition of "this user's folder," used both when writing a
    new upload (backend/main.py) and when reading who owns what (below) -
    keeping upload and read paths in agreement is what makes the folder a
    reliable source of truth instead of two things that could drift."""
    safe = re.sub(r'[<>:"/\\|?*]', "", user_row["name"]).strip()
    return safe or f"user{user_row['id']}"


def get_accessible_filenames(user_row, data_dir: Path) -> set[str]:
    """Which source_file values (see src/ingest.py's COMMON_COLUMNS) this
    user's sales-data queries should be scoped to - read directly from the
    filesystem, not a database table (see module docstring).

    data_dir: the data/ directory (backend/main.py's DATA_DIR). Legacy
    files sit directly in it; employee-owned files sit under
    data_dir/employees/<name>/.
    """
    accessible_user_ids = get_accessible_user_ids(user_row)
    accessible: set[str] = set()

    # Legacy/unattributed data (predates per-employee folders): visible
    # only to managers (the broadest legitimate role) rather than everyone
    # or no one - see ACCESS-CONTROL.md's "Unattributed data" section.
    if user_row["role"] == "manager":
        for path in data_dir.glob("*.xlsx"):
            accessible.add(path.name)

    # Employee-owned files: whatever's actually sitting in each accessible
    # person's own folder, read fresh every call - no separate ownership
    # record to fall out of sync with reality.
    employees_root = data_dir / "employees"
    for uid in accessible_user_ids:
        member_row = get_user_by_id(uid)
        if member_row is None:
            continue  # deleted since the hierarchy walk read them
        member_folder = employees_root / employee_folder_name(member_row)
        if member_folder.is_dir():
            for path in member_folder.glob("*.xlsx"):
                accessible.add(path.name)

    return accessible


def find_owning_employee_folder(filename: str, data_dir: Path) -> str | None:
    """Which employee subfolder (if any) currently contains a file with
    this exact name - the folder location IS the ownership record, so
    this is the single check both get_accessible_filenames (read access)
    and backend/main.py's replace-permission check are built on. None
    means the file is a legacy/unattributed top-level file, not owned by
    any specific employee."""
    employees_root = data_dir / "employees"
    if not employees_root.is_dir():
        return None
    matches = list(employees_root.rglob(filename))
    return matches[0].parent.name if matches else None


def require_role_assigned(user: dict = Depends(get_current_user)) -> dict:
    """FastAPI dependency for every data-bearing endpoint - blocks access
    until the user has picked a role/manager via POST /auth/complete-profile.
    Distinct from a plain 401: the user IS authenticated, just not yet
    positioned in the hierarchy, so nothing has an owner to check against."""
    if user["role"] is None:
        raise HTTPException(
            status_code=403,
            detail="Complete your profile (role and reporting line) before continuing.",
        )
    return user
