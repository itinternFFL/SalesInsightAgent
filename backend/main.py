"""FastAPI backend for the Sales Insight Agent React UI.

Run with:  python -m uvicorn backend.main:app --port 8001
"""

from dotenv import load_dotenv

load_dotenv()  # must run before any of the auth env vars below are read

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from backend.access_control import (
    employee_folder_name,
    find_owning_employee_folder,
    get_accessible_filenames,
    get_accessible_user_ids,
    require_role_assigned,
)
from backend.auth import get_current_user
from backend.auth import router as auth_router
from backend.db import get_user_by_id, init_db, record_file_upload
from backend.local_auth import router as local_auth_router
from backend.uploads import (
    build_upload_summary,
    discard_pending,
    read_pending,
    stash_pending,
)
from src.agent import MODEL, answer
from src.index import build_index_from_df
from src.ingest import DATA_DIR, canonical_filename, load_all, parse_upload

# _state["master_df"] is the whole company dataset - every user's queries
# are scoped down from it per-request via _scoped_data(), never served
# directly, and nothing in this app ever answers a query from anything
# outside that scoped view - see ACCESS-CONTROL.md's "Queries never
# reference data outside the asker's scope" for why there is deliberately
# no fast-path check that compares a question against the full dataset,
# even just to decide whether to refuse faster.
# _state["scoped_index_cache"] holds one built (embedded) chunk index per
# distinct accessible-filenames set, since embedding is the expensive step
# - see ACCESS-CONTROL.md's "Performance & caching" section for why this
# is safe to cache (and when it's invalidated).
_state: dict = {}


def _refresh_state():
    """Re-run after any file is added to data/ so the running process picks
    up the change without needing a restart. load_all(use_cache=False) forces
    a fresh parse and rewrites master_sales.parquet with a newer mtime."""
    _state["master_df"] = load_all(use_cache=False)
    _state["scoped_index_cache"] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _state["master_df"] = load_all()
    _state["scoped_index_cache"] = {}
    yield
    _state.clear()


app = FastAPI(title="Sales Insight Agent API", lifespan=lifespan)

# "production" enables the cross-origin cookie settings needed once the
# frontend (Vercel) and backend (a separate server) are on different
# domains - see DEPLOYMENT.md and SETUP.md. Locally, the Vite dev proxy
# makes everything same-origin, so the simpler same-site settings apply.
APP_ENV = os.environ.get("APP_ENV", "development")
IS_PRODUCTION = APP_ENV == "production"

SESSION_SECRET_KEY = os.environ.get("SESSION_SECRET_KEY")
if not SESSION_SECRET_KEY:
    if IS_PRODUCTION:
        raise RuntimeError("SESSION_SECRET_KEY must be set in production - see SETUP.md")
    SESSION_SECRET_KEY = "dev-insecure-secret-change-me"
    print("WARNING: SESSION_SECRET_KEY not set - using an insecure local-dev default.")

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET_KEY,
    max_age=8 * 60 * 60,  # 8-hour session expiry
    same_site="none" if IS_PRODUCTION else "lax",
    https_only=IS_PRODUCTION,
)

# In production this is set via the systemd EnvironmentFile (see
# deploy/sales-agent-backend.service) to the real Vercel frontend URL.
ALLOWED_ORIGINS = os.environ.get(
    "ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,  # required for the session cookie to cross origins
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(local_auth_router)


class ChatRequest(BaseModel):
    question: str


class ChatResponse(BaseModel):
    answer: str


class ResolveRequest(BaseModel):
    upload_id: str
    action: str  # "replace" | "keep_both" | "merge_anyway" | "skip"


def _scoped_data(user: dict):
    """The (dataframe, cache-key) pair for exactly the sales data `user` is
    permitted to see, per ACCESS-CONTROL.md's hierarchy rules. Recomputed
    on every call (no hierarchy caching, no folder-listing caching), so a
    role/reports-to change or a new upload is reflected on the very next
    request. accessible_files is read straight from data/employees/ - see
    backend/access_control.py's module docstring for why the filesystem,
    not a database table, is the source of truth for file ownership."""
    master_df = _state["master_df"]
    accessible_files = get_accessible_filenames(user, DATA_DIR)
    scoped_df = master_df[master_df["source_file"].isin(accessible_files)]
    return scoped_df, frozenset(accessible_files)


def _scoped_index(scoped_df, cache_key):
    cache = _state["scoped_index_cache"]
    if cache_key not in cache:
        cache[cache_key] = build_index_from_df(scoped_df, verbose=False)
    return cache[cache_key]


def _find_existing_path(filename: str) -> Path | None:
    """Search the whole data/ tree - legacy top-level files and every
    employee subfolder - for a file with this exact canonical filename.
    Canonical filenames are unique per month across the WHOLE tree, not
    just within one employee's folder, since source_file (and, before
    folder location became the source of truth, the old file_uploads
    attribution) is keyed by filename alone - see src/ingest.py's
    load_all() docstring for why two different people's files for the
    same month can't coexist under different names."""
    matches = list(DATA_DIR.rglob(filename))
    return matches[0] if matches else None


def _find_free_path_globally(target: Path) -> Path:
    """Like backend/uploads.py's find_free_path, but checks uniqueness
    across the WHOLE data/ tree, not just target's own folder - two
    employees' folders can't each hold a file with the identical name, or
    source_file-keyed RBAC scoping would silently conflate them. The
    second uploader's own-folder path never collided locally on its own,
    so a folder-local check alone would let it through wrongly."""
    if _find_existing_path(target.name) is None:
        return target
    stem, suffix = target.stem, target.suffix
    n = 2
    while True:
        candidate = target.with_name(f"{stem}_{n}{suffix}")
        if _find_existing_path(candidate.name) is None:
            return candidate
        n += 1


def _ensure_can_replace_file(user: dict, filename: str):
    """Blocks overwriting a file that belongs to someone outside the
    current user's accessible branch - without this, any authenticated
    user could destroy another branch's data just by uploading a file that
    happens to canonicalize to the same name (same month). Ownership is
    read from which employee folder the file is currently sitting in -
    see backend/access_control.py's find_owning_employee_folder."""
    owning_folder = find_owning_employee_folder(filename, DATA_DIR)
    if owning_folder is None:
        if user["role"] != "manager":
            raise HTTPException(status_code=403, detail="You don't have permission to replace this file.")
        return
    accessible_rows = (get_user_by_id(uid) for uid in get_accessible_user_ids(user))
    accessible_folders = {employee_folder_name(row) for row in accessible_rows if row is not None}
    if owning_folder not in accessible_folders:
        raise HTTPException(status_code=403, detail="You don't have permission to replace this file.")


@app.get("/api/me")
def me(user: dict = Depends(get_current_user)):
    return user


@app.get("/api/stats")
def stats(user: dict = Depends(require_role_assigned)):
    scoped_df, _ = _scoped_data(user)
    if scoped_df.empty:
        return {"rows": 0, "months": [], "categories": [], "model": MODEL}
    return {
        "rows": int(len(scoped_df)),
        "months": sorted(scoped_df["month"].unique().tolist()),
        "categories": sorted(scoped_df["category"].unique().tolist()),
        "model": MODEL,
    }


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest, user: dict = Depends(require_role_assigned)):
    # scoped_df is exactly - and only - the data this user is permitted to
    # see. Every step below builds from it alone: no step ever compares
    # against, reads, or reasons about anything outside it, including to
    # decide how to respond to an out-of-scope question - see
    # ACCESS-CONTROL.md's "Queries never reference data outside the
    # asker's scope."
    scoped_df, cache_key = _scoped_data(user)
    if scoped_df.empty:
        return ChatResponse(
            answer="There's no sales data available to you yet - ask your manager, "
            "or upload a report to get started."
        )

    index_df = _scoped_index(scoped_df, cache_key)
    response_text = answer(req.question, index_df)
    return ChatResponse(answer=response_text)


@app.post("/api/upload")
async def upload(file: UploadFile = File(...), user: dict = Depends(require_role_assigned)):
    file_bytes = await file.read()
    result = parse_upload(file_bytes, file.filename)

    if result.month is None:
        return {
            "status": "error",
            "message": (
                "Could not determine which month/year this file covers. "
                "Please rename it to match Sale_Report_FMO-<Month>-<Year>.xlsx "
                "and re-upload, or tell me which period it covers."
            ),
        }

    canonical_name = canonical_filename(result.month)
    existing_path = _find_existing_path(canonical_name)
    naming_conflict = existing_path is not None
    target_path = existing_path or (
        DATA_DIR / "employees" / employee_folder_name(user) / canonical_name
    )

    if not naming_conflict and not result.has_schema_issues:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(file_bytes)
        record_file_upload(target_path.name, user["id"])
        _refresh_state()
        return {
            "status": "saved",
            "summary": build_upload_summary(result, target_path),
        }

    upload_id = stash_pending(file_bytes)
    issue_lines = []
    if naming_conflict:
        issue_lines.append(
            f"A file for {target_path.name} already exists in the dataset."
        )
        actions = [
            {"label": "Replace", "value": "replace"},
            {"label": "Keep Both", "value": "keep_both"},
            {"label": "Skip", "value": "skip"},
        ]
    else:
        actions = [
            {"label": "Merge Anyway", "value": "merge_anyway"},
            {"label": "Skip", "value": "skip"},
        ]
    if result.has_schema_issues:
        for s in result.sheets:
            if s.schema_issues:
                issue_lines.append(f"{s.sheet_name}: {'; '.join(s.schema_issues)}")

    return {
        "status": "needs_confirmation",
        "upload_id": upload_id,
        "message": " ".join(issue_lines),
        "actions": actions,
    }


@app.post("/api/upload/resolve")
def resolve_upload(req: ResolveRequest, user: dict = Depends(require_role_assigned)):
    file_bytes = read_pending(req.upload_id)
    if file_bytes is None:
        return {"status": "error", "message": "This upload has expired or was already resolved."}

    if req.action == "skip":
        discard_pending(req.upload_id)
        return {"status": "skipped", "summary": "Upload discarded - no changes made."}

    result = parse_upload(file_bytes, "upload.xlsx")
    canonical_name = canonical_filename(result.month)
    own_folder_path = DATA_DIR / "employees" / employee_folder_name(user) / canonical_name

    if req.action == "replace":
        _ensure_can_replace_file(user, canonical_name)
        # Overwrite wherever the existing file actually lives, not
        # necessarily this uploader's own folder - falls back to their own
        # folder only if it's somehow already gone (shouldn't normally
        # happen, since "replace" is only offered after a conflict was
        # just detected).
        target_path = _find_existing_path(canonical_name) or own_folder_path
    elif req.action == "keep_both":
        target_path = _find_free_path_globally(own_folder_path)
    else:
        # "merge_anyway" writes straight to the uploader's own folder too -
        # it's only ever offered when there was no naming conflict, so
        # this is a brand new file, not an overwrite.
        target_path = own_folder_path

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(file_bytes)
    record_file_upload(target_path.name, user["id"])
    discard_pending(req.upload_id)
    _refresh_state()

    return {
        "status": "saved",
        "summary": build_upload_summary(result, target_path),
    }
