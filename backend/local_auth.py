"""Email/password sign-in - an alternative to Microsoft SSO (backend/auth.py),
not a replacement. Self-registration is restricted to approved email
domain(s) so only employees can create an account; there is no separate
"personal account" tier the way Microsoft's login has one to reject.

Sessions set here use the same shape as backend/auth.py's Microsoft flow
(request.session["user"] = {name, email, oid}), so get_current_user and
every protected route work identically regardless of which path a user
signed in through.
"""

import os
import re

import bcrypt
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from backend.db import create_user, get_user_by_email

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


class LoginRequest(BaseModel):
    email: str
    password: str


def _email_domain_allowed(email: str) -> bool:
    if not ALLOWED_EMAIL_DOMAINS:
        return False
    domain = email.rsplit("@", 1)[-1].lower()
    return domain in ALLOWED_EMAIL_DOMAINS


def _set_session(request: Request, user_row) -> dict:
    session_user = {"name": user_row["name"], "email": user_row["email"], "oid": None}
    request.session["user"] = session_user
    return session_user


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

    password_hash = bcrypt.hashpw(req.password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    user_row = create_user(email, name, password_hash)
    return _set_session(request, user_row)


@router.post("/login-password")
def login_password(req: LoginRequest, request: Request):
    email = req.email.strip().lower()
    user_row = get_user_by_email(email)

    # Same generic error either way - don't reveal whether the email exists.
    invalid = HTTPException(status_code=401, detail="Invalid email or password.")
    if user_row is None:
        raise invalid
    if not bcrypt.checkpw(req.password.encode("utf-8"), user_row["password_hash"].encode("utf-8")):
        raise invalid

    return _set_session(request, user_row)
