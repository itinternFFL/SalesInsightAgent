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
part (embedding a filtered chunk index, and extracting entity values from
it), keyed by the resulting accessible-filenames set.
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


# Columns worth checking a question against before running retrieval/
# generation at all. Deliberately excludes "month" - a user can have
# partial access to a month (some of its files, not all), so naming a
# month doesn't mean "definitely no access" the way naming a specific
# brand/customer/etc. that's entirely outside their scope does.
ENTITY_COLUMNS = ["brand", "customer_name", "mat_name", "channel", "sale_type"]
MIN_ENTITY_LENGTH = 4  # skip short/generic values, too likely to false-match


def collect_entity_values(df) -> dict[str, set]:
    """The distinct values (per ENTITY_COLUMNS) actually present in `df`.
    This is the one part of the check that scans a whole DataFrame - O(rows),
    not just O(distinct values) - so callers should compute and cache it
    once per DataFrame version rather than call this per request. See
    backend/main.py's _state["entity_values_cache"] (whole-dataset side,
    recomputed only in _refresh_state) and _scoped_entity_values() (per-
    accessible-scope side, cached the same way as _scoped_index_cache) -
    both invalidate exactly when the underlying data they summarize
    changes, never on a schedule."""
    values: dict[str, set] = {}
    for col in ENTITY_COLUMNS:
        values[col] = set(df[col].dropna().unique()) if col in df.columns else set()
    return values


def find_out_of_scope_entity(
    query: str, full_entity_values: dict[str, set], scoped_entity_values: dict[str, set]
) -> str | None:
    """Cheap, LLM-free check: does the question name a specific entity that
    exists in the FULL dataset but not anywhere in what this user can see?
    If so, return that entity's name so the caller can refuse immediately -
    skipping index building and generation entirely, the expensive part of
    every request (tens of seconds), for a query that was always going to
    end in "no data" anyway.

    Takes precomputed entity-value sets (see collect_entity_values), not
    raw DataFrames - this function itself is just string matching, cheap
    regardless of how large the underlying dataset grows; the only part
    that scales with dataset size is the extraction step, which callers
    cache and reuse instead of redoing on every question.

    This can only ever cause a SKIP, never a false grant: a miss here (no
    entity matched, or the matched entity IS in scope) just means the
    normal RAG pipeline runs as before, which still correctly declines on
    its own - just slower. It must never be trusted as the sole access
    check for anything the model is allowed to actually answer from.
    Checking only the asker's own values, with no whole-dataset reference
    point, isn't an option: an ordinary question ("what's the grand
    total?") would then just as trivially fail to match the asker's own
    small value set as a genuinely out-of-scope one would, with no way to
    tell "belongs to someone else" apart from "names nothing in
    particular" - that would refuse most normal questions, not just the
    ones that should be refused.
    """
    query_lower = query.lower()
    for col in ENTITY_COLUMNS:
        scoped_values = scoped_entity_values.get(col, set())
        for value in full_entity_values.get(col, set()):
            value_str = str(value)
            if len(value_str) < MIN_ENTITY_LENGTH:
                continue
            if value_str.lower() in query_lower and value not in scoped_values:
                return value_str
    return None


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
