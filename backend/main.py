"""FastAPI backend for the Sales Insight Agent React UI.

Run with:  python -m uvicorn backend.main:app --port 8001
"""

from dotenv import load_dotenv

load_dotenv()  # must run before any of the auth env vars below are read

import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from backend.access_control import get_accessible_filenames, get_accessible_user_ids, require_role_assigned
from backend.auth import get_current_user
from backend.auth import router as auth_router
from backend.db import get_uploader_id, init_db, list_file_uploads, record_file_upload
from backend.local_auth import router as local_auth_router
from backend.uploads import (
    build_upload_summary,
    discard_pending,
    find_free_path,
    read_pending,
    stash_pending,
)
from src.agent import MODEL, answer
from src.index import build_index_from_df
from src.ingest import DATA_DIR, canonical_filename, load_all, parse_upload

# _state["master_df"] is the whole company dataset - every user's queries
# are scoped down from it per-request via _scoped_data(), never served
# directly. _state["scoped_index_cache"] holds one built (embedded) chunk
# index per distinct accessible-filenames set, since embedding is the
# expensive step - see ACCESS-CONTROL.md's "Performance & caching" section
# for why this is safe to cache (and when it's invalidated).
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
    on every call (no hierarchy caching), so a role/reports-to change or a
    new upload is reflected on the very next request."""
    master_df = _state["master_df"]
    all_source_files = master_df["source_file"].unique().tolist()
    file_uploads = list_file_uploads()
    accessible_files = get_accessible_filenames(user, file_uploads, all_source_files)
    scoped_df = master_df[master_df["source_file"].isin(accessible_files)]
    return scoped_df, frozenset(accessible_files)


def _scoped_index(scoped_df, cache_key):
    cache = _state["scoped_index_cache"]
    if cache_key not in cache:
        cache[cache_key] = build_index_from_df(scoped_df, verbose=False)
    return cache[cache_key]


def _ensure_can_replace_file(user: dict, filename: str):
    """Blocks overwriting a file that belongs to someone outside the
    current user's accessible branch - without this, any authenticated
    user could destroy another branch's data just by uploading a file that
    happens to canonicalize to the same name (same month)."""
    existing_uploader_id = get_uploader_id(filename)
    if existing_uploader_id is None:
        if user["role"] != "manager":
            raise HTTPException(status_code=403, detail="You don't have permission to replace this file.")
        return
    if existing_uploader_id not in get_accessible_user_ids(user):
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

    target_path = DATA_DIR / canonical_filename(result.month)
    naming_conflict = target_path.exists()

    if not naming_conflict and not result.has_schema_issues:
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
    target_path = DATA_DIR / canonical_filename(result.month)

    if req.action == "replace":
        _ensure_can_replace_file(user, target_path.name)
    elif req.action == "keep_both":
        target_path = find_free_path(target_path)
    # "merge_anyway" writes straight to target_path too - it's only ever
    # offered when there was no naming conflict, so target_path is new.

    target_path.write_bytes(file_bytes)
    record_file_upload(target_path.name, user["id"])
    discard_pending(req.upload_id)
    _refresh_state()

    return {
        "status": "saved",
        "summary": build_upload_summary(result, target_path),
    }
