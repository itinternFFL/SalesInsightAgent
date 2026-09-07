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


# Columns worth checking a question against before running retrieval/
# generation at all. Deliberately excludes "month" - a user can have
# partial access to a month (some of its files, not all), so naming a
# month doesn't mean "definitely no access" the way naming a specific
# brand/customer/etc. that's entirely outside their scope does.
ENTITY_COLUMNS = ["brand", "customer_name", "mat_name", "channel", "sale_type"]
MIN_ENTITY_LENGTH = 4  # skip short/generic values, too likely to false-match


def find_out_of_scope_entity(query: str, full_df, scoped_df) -> str | None:
    """Cheap, LLM-free check: does the question name a specific entity that
    exists in the FULL dataset but not anywhere in what this user can see?
    If so, return that entity's name so the caller can refuse immediately -
    skipping index building and generation entirely, the expensive part of
    every request (tens of seconds), for a query that was always going to
    end in "no data" anyway.

    This can only ever cause a SKIP, never a false grant: a miss here (no
    entity matched, or the matched entity IS in scope) just means the
    normal RAG pipeline runs as before, which still correctly declines on
    its own - just slower. It must never be trusted as the sole access
    check for anything the model is allowed to actually answer from.
    """
    query_lower = query.lower()
    for col in ENTITY_COLUMNS:
        if col not in full_df.columns:
            continue
        scoped_values = set(scoped_df[col].dropna().unique()) if col in scoped_df.columns else set()
        for value in full_df[col].dropna().unique():
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
