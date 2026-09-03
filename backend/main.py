"""FastAPI backend for the Sales Insight Agent React UI.

Run with:  python -m uvicorn backend.main:app --port 8001
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

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

# In production this is set via the systemd EnvironmentFile (see
# deploy/sales-agent-backend.service) to the real Vercel frontend URL.
ALLOWED_ORIGINS = os.environ.get(
    "ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    question: str


class ChatResponse(BaseModel):
    answer: str


class ResolveRequest(BaseModel):
    upload_id: str
    action: str  # "replace" | "keep_both" | "merge_anyway" | "skip"


@app.get("/api/stats")
def stats():
    return _state["stats"]


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    response_text = answer(req.question, _state["index"])
    return ChatResponse(answer=response_text)


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
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
def resolve_upload(req: ResolveRequest):
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
