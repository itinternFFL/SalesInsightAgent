"""Helpers for the file-upload endpoints: pending-file staging, filename
disambiguation, and the auto-generated "file received" summary text."""

import uuid
from pathlib import Path

import pandas as pd

from src.ingest import DATA_DIR, UploadParseResult

PENDING_DIR = Path(__file__).resolve().parent.parent / "cache" / "pending_uploads"


def stash_pending(file_bytes: bytes) -> str:
    """Save uploaded bytes under a fresh id so a later /resolve call can
    finish the job without the client re-sending the file."""
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    upload_id = uuid.uuid4().hex
    (PENDING_DIR / f"{upload_id}.xlsx").write_bytes(file_bytes)
    return upload_id


def read_pending(upload_id: str) -> bytes | None:
    path = PENDING_DIR / f"{upload_id}.xlsx"
    return path.read_bytes() if path.exists() else None


def discard_pending(upload_id: str) -> None:
    path = PENDING_DIR / f"{upload_id}.xlsx"
    path.unlink(missing_ok=True)


def find_free_path(target: Path) -> Path:
    """For 'keep both': target.xlsx -> target_2.xlsx -> target_3.xlsx ...
    until a name that doesn't already exist is found."""
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    n = 2
    while True:
        candidate = target.with_name(f"{stem}_{n}{suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def build_upload_summary(result: UploadParseResult, saved_path: Path) -> str:
    """The automatic 'file received' summary: save confirmation, sheets +
    row counts, period covered, per-sheet Net Sale totals, and anomalies -
    posted to chat without the user having to ask."""
    month_label = (
        pd.Period(result.month, freq="M").strftime("%B %Y")
        if result.month else "unknown"
    )

    lines = [f"**Saved** to `{saved_path.relative_to(DATA_DIR.parent)}`.", ""]

    lines.append("**Sheets found:**")
    for s in result.sheets:
        lines.append(f"- {s.sheet_name}: **{s.row_count:,}** rows")
    lines.append("")

    lines.append(f"**Period covered:** {month_label}")
    lines.append("")

    lines.append("**Total Net Sale:**")
    for s in result.sheets:
        total = s.df["net_sale"].sum() if len(s.df) else 0
        lines.append(f"- {s.sheet_name}: **Rs {total:,.0f}**")
    lines.append("")

    anomalies = []
    for s in result.sheets:
        if s.schema_issues:
            anomalies.append(f"{s.sheet_name} - {'; '.join(s.schema_issues)}")
        if s.dropped_blank_rows:
            anomalies.append(f"{s.sheet_name} - {s.dropped_blank_rows} blank row(s) dropped")
        if s.duplicate_rows:
            anomalies.append(f"{s.sheet_name} - {s.duplicate_rows} duplicate row(s)")
        if s.positive_sign_rows:
            anomalies.append(
                f"{s.sheet_name} - {s.positive_sign_rows} row(s) with a "
                f"positive Net Sale (unexpected sign for this dataset)"
            )
    lines.append("**Anomalies:** " + ("; ".join(anomalies) if anomalies else "None detected."))

    return "\n".join(lines)
