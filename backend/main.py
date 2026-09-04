"""FastAPI backend for the Sales Insight Agent React UI.

Run with:  python -m uvicorn backend.main:app --port 8001
"""

from dotenv import load_dotenv

load_dotenv()  # must run before any of the auth env vars below are read

import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from backend.auth import get_current_user
from backend.auth import router as auth_router
from backend.db import init_db
from backend.local_auth import router as local_auth_router
from backend.uploads import (
    build_upload_summary,
    discard_pending,
    find_free_path,
    read_pending,
    stash_pending,
)
from src.agent import MODEL, answer
from src.index import get_index
from src.ingest import DATA_DIR, canonical_filename, load_all, parse_upload

_state: dict = {}


def _refresh_state():
    """Re-run after any file is added to data/ so the running process picks
    up the change without needing a restart. load_all(use_cache=False) forces
    a fresh parse and rewrites master_sales.parquet with a newer mtime, so
    the get_index() call after it correctly sees its own cache is stale and
    rebuilds - without needing to force a second full xlsx re-parse itself."""
    df = load_all(use_cache=False)
    _state["stats"] = {
        "rows": int(len(df)),
        "months": sorted(df["month"].unique().tolist()),
        "categories": sorted(df["category"].unique().tolist()),
        "model": MODEL,
    }
    _state["index"] = get_index(use_cache=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _state["index"] = get_index()
    df = load_all()
    _state["stats"] = {
        "rows": int(len(df)),
        "months": sorted(df["month"].unique().tolist()),
        "categories": sorted(df["category"].unique().tolist()),
        "model": MODEL,
    }
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


@app.get("/api/me")
def me(user: dict = Depends(get_current_user)):
    return user


@app.get("/api/stats")
def stats(user: dict = Depends(get_current_user)):
    return _state["stats"]


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest, user: dict = Depends(get_current_user)):
    response_text = answer(req.question, _state["index"])
    return ChatResponse(answer=response_text)


@app.post("/api/upload")
async def upload(file: UploadFile = File(...), user: dict = Depends(get_current_user)):
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
def resolve_upload(req: ResolveRequest, user: dict = Depends(get_current_user)):
    file_bytes = read_pending(req.upload_id)
    if file_bytes is None:
        return {"status": "error", "message": "This upload has expired or was already resolved."}

    if req.action == "skip":
        discard_pending(req.upload_id)
        return {"status": "skipped", "summary": "Upload discarded - no changes made."}

    result = parse_upload(file_bytes, "upload.xlsx")
    target_path = DATA_DIR / canonical_filename(result.month)

    if req.action == "keep_both":
        target_path = find_free_path(target_path)
    # "replace" and "merge_anyway" both just write to the canonical path.

    target_path.write_bytes(file_bytes)
    discard_pending(req.upload_id)
    _refresh_state()

    return {
        "status": "saved",
        "summary": build_upload_summary(result, target_path),
    }
