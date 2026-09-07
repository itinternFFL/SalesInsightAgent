"""Role-based access control: who can see whose uploaded sales data.

Hierarchy (top to bottom): manager -> senior_executive -> executive. A
user's visibility is scoped to their own reporting branch, walked downward
from themselves - see ACCESS-CONTROL.md for the full model, the
"unattributed legacy data" policy, and the deleted/unassigned-manager edge
case.

Nothing here is cached across requests: every call re-reads the users table,
so a hierarchy change (reassigning someone to a different manager) is
reflected on the very next request, for every affected user, with no
invalidation step needed. The only cache backend/main.py keeps is on the
expensive part (embedding a filtered chunk index), keyed by the resulting
accessible-filenames set - see build_scoped_index() there.
"""

from fastapi import Depends, HTTPException

from backend.auth import get_current_user
from backend.db import list_direct_reports


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


def get_accessible_filenames(user_row, file_uploads: dict[str, int | None], all_source_files) -> set[str]:
    """Which source_file values (see src/ingest.py's COMMON_COLUMNS) this
    user's sales-data queries should be scoped to.

    file_uploads: filename -> uploaded_by_id, from db.list_file_uploads() -
    only covers files uploaded since this feature shipped.
    all_source_files: every source_file value actually present in the
    dataset right now (from the master DataFrame) - a filename in here but
    NOT in file_uploads is legacy/unattributed (predates upload tracking).
    """
    accessible_user_ids = get_accessible_user_ids(user_row)
    accessible = set()
    for filename in all_source_files:
        uploader_id = file_uploads.get(filename)
        if uploader_id is None:
            # Unattributed/legacy data: visible only to managers (the
            # broadest legitimate role) rather than everyone or no one -
            # see ACCESS-CONTROL.md's "Unattributed data" section.
            if user_row["role"] == "manager":
                accessible.add(filename)
        elif uploader_id in accessible_user_ids:
            accessible.add(filename)
    return accessible


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
