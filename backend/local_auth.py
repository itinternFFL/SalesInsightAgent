"""Email/password sign-in - an alternative to Microsoft SSO (backend/auth.py),
not a replacement. Self-registration is restricted to approved email
domain(s) so only employees can create an account; there is no separate
"personal account" tier the way Microsoft's login has one to reject.

Also hosts the role/reporting-line self-service endpoints used by BOTH
auth paths: registration collects role + manager directly (local accounts
already fill out a form), while a first-time Microsoft SSO user has no
form at all, so the frontend sends them through POST /auth/complete-profile
after their first successful login instead. See ACCESS-CONTROL.md - roles
are self-declared, not admin-assigned, which is a deliberate simplification
for a small internal tool, documented there along with its tradeoff.

Sessions set here use the same shape as backend/auth.py's Microsoft flow
(request.session["user"] = {"id": ...}), so get_current_user and every
protected route work identically regardless of which path a user signed
in through.
"""

import os
import re

import bcrypt
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from backend.db import (
    ROLES,
    create_user,
    get_user_by_email,
    get_user_by_id,
    list_users_by_role,
    set_role,
)

# Comma-separated list, e.g. "faujifoods.com" or "faujifoods.com,fauji.com.pk".
# No default - if unset, registration is refused rather than silently open.
ALLOWED_EMAIL_DOMAINS = [
    d.strip().lower()
    for d in os.environ.get("ALLOWED_EMAIL_DOMAIN", "").split(",")
    if d.strip()
]

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LENGTH = 8

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str
    role: str
    reports_to_id: int | None = None


class LoginRequest(BaseModel):
    email: str
    password: str


class ProfileRequest(BaseModel):
    role: str
    reports_to_id: int | None = None


def _email_domain_allowed(email: str) -> bool:
    if not ALLOWED_EMAIL_DOMAINS:
        return False
    domain = email.rsplit("@", 1)[-1].lower()
    return domain in ALLOWED_EMAIL_DOMAINS


def _validate_role_assignment(role: str, reports_to_id: int | None):
    """Shared by registration and profile completion - enforces the 3-tier
    shape (manager -> senior_executive -> executive) so the hierarchy stored
    in the database can never itself be invalid: a manager can't report to
    anyone, and everyone else must report to a real user one level up."""
    if role not in ROLES:
        raise HTTPException(status_code=400, detail=f"role must be one of {sorted(ROLES)}.")

    if role == "manager":
        if reports_to_id is not None:
            raise HTTPException(status_code=400, detail="A manager cannot report to anyone.")
        return

    if reports_to_id is None:
        raise HTTPException(status_code=400, detail="reports_to_id is required for this role.")

    parent = get_user_by_id(reports_to_id)
    if parent is None:
        raise HTTPException(status_code=400, detail="reports_to_id does not refer to a real user.")

    expected_parent_role = "manager" if role == "senior_executive" else "senior_executive"
    if parent["role"] != expected_parent_role:
        raise HTTPException(
            status_code=400,
            detail=f"A {role.replace('_', ' ')} must report to a {expected_parent_role.replace('_', ' ')}.",
        )


def _public_user(user_row) -> dict:
    return {
        "id": user_row["id"],
        "name": user_row["name"],
        "email": user_row["email"],
        "role": user_row["role"],
        "reports_to_id": user_row["reports_to_id"],
    }


def _set_session(request: Request, user_row) -> dict:
    request.session["user"] = {"id": user_row["id"]}
    return _public_user(user_row)


@router.get("/managers")
def list_managers():
    """Public (unauthenticated) - candidate list for the 'reports to'
    picker a Senior Executive fills in at registration/profile setup. Only
    name/id are exposed, not email, to keep the leak from an
    unauthenticated endpoint as small as possible."""
    return [{"id": r["id"], "name": r["name"]} for r in list_users_by_role("manager")]


@router.get("/senior-executives")
def list_senior_executives():
    """Public - candidate list for the 'reports to' picker an Executive
    fills in. Includes reports_to_id so the frontend can show which
    manager each senior executive belongs to."""
    return [
        {"id": r["id"], "name": r["name"], "reports_to_id": r["reports_to_id"]}
        for r in list_users_by_role("senior_executive")
    ]


@router.post("/register")
def register(req: RegisterRequest, request: Request):
    name = req.name.strip()
    email = req.email.strip().lower()

    if not name:
        raise HTTPException(status_code=400, detail="Name is required.")
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Enter a valid email address.")
    if not _email_domain_allowed(email):
        raise HTTPException(
            status_code=403,
            detail="Registration is restricted to company email addresses.",
        )
    if len(req.password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters.",
        )
    if get_user_by_email(email) is not None:
        raise HTTPException(status_code=409, detail="An account with this email already exists.")

    _validate_role_assignment(req.role, req.reports_to_id)

    password_hash = bcrypt.hashpw(req.password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    user_row = create_user(email, name, password_hash, req.role, req.reports_to_id)
    return _set_session(request, user_row)


@router.post("/login-password")
def login_password(req: LoginRequest, request: Request):
    email = req.email.strip().lower()
    user_row = get_user_by_email(email)

    # Same generic error either way - don't reveal whether the email exists.
    invalid = HTTPException(status_code=401, detail="Invalid email or password.")
    if user_row is None or user_row["password_hash"] is None:
        raise invalid
    if not bcrypt.checkpw(req.password.encode("utf-8"), user_row["password_hash"].encode("utf-8")):
        raise invalid

    return _set_session(request, user_row)


@router.post("/complete-profile")
def complete_profile(req: ProfileRequest, request: Request):
    """Sets or updates the current user's role and reporting line. Required
    after a first-time Microsoft SSO login (which has no form to collect
    this); also reachable any time after that to handle a reassignment,
    consistent with roles being self-declared rather than admin-assigned -
    see ACCESS-CONTROL.md."""
    session_user = request.session.get("user")
    if not session_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    _validate_role_assignment(req.role, req.reports_to_id)

    user_row = set_role(session_user["id"], req.role, req.reports_to_id)
    return _public_user(user_row)
