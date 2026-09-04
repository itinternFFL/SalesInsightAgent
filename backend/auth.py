"""Microsoft SSO (Entra ID) authentication, restricted to one tenant.

Backend-owned Authorization Code flow via MSAL - the browser is redirected
to Microsoft, Microsoft redirects back here with a code, we exchange it for
tokens server-side, and store only the minimal user info in a signed
HttpOnly session cookie. The frontend never sees the client secret or any
Microsoft token; it only ever talks to /auth/* and reads /api/me.

Tenant restriction is enforced twice: the authority URL below is
tenant-specific (Microsoft's login page itself refuses accounts outside
that tenant), and the returned ID token's `tid` claim is checked again
here as defense in depth - see _validate_tenant().
"""

import os

import msal
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

MS_CLIENT_ID = os.environ.get("MS_CLIENT_ID")
MS_CLIENT_SECRET = os.environ.get("MS_CLIENT_SECRET")
MS_TENANT_ID = os.environ.get("MS_TENANT_ID")
MS_REDIRECT_URI = os.environ.get("MS_REDIRECT_URI", "http://localhost:8001/auth/callback")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:5173")

AUTHORITY = f"https://login.microsoftonline.com/{MS_TENANT_ID}"
SCOPES = ["User.Read"]  # openid/profile/email are requested implicitly by MSAL

router = APIRouter(prefix="/auth", tags=["auth"])


def _msal_app() -> msal.ConfidentialClientApplication:
    if not (MS_CLIENT_ID and MS_CLIENT_SECRET and MS_TENANT_ID):
        raise RuntimeError(
            "MS_CLIENT_ID, MS_CLIENT_SECRET, and MS_TENANT_ID must be set - see SETUP.md"
        )
    return msal.ConfidentialClientApplication(
        MS_CLIENT_ID, authority=AUTHORITY, client_credential=MS_CLIENT_SECRET
    )


def _login_error_redirect(reason: str) -> RedirectResponse:
    return RedirectResponse(f"{FRONTEND_URL}/?login_error={reason}")


@router.get("/login")
def login(request: Request):
    app = _msal_app()
    # state ties the callback back to this browser session, independent of
    # the (currently anonymous, pre-login) session cookie.
    flow = app.initiate_auth_code_flow(SCOPES, redirect_uri=MS_REDIRECT_URI)
    request.session["auth_flow"] = flow
    return RedirectResponse(flow["auth_uri"])


@router.get("/callback")
def callback(request: Request):
    flow = request.session.pop("auth_flow", None)
    if not flow:
        return _login_error_redirect("expired")

    app = _msal_app()
    result = app.acquire_token_by_auth_code_flow(flow, dict(request.query_params))

    if "error" in result:
        # Includes the user cancelling, or Microsoft rejecting the account
        # outright (e.g. a personal account against a work/school-only app).
        return _login_error_redirect("denied")

    claims = result.get("id_token_claims", {})
    if claims.get("tid") != MS_TENANT_ID:
        # Defense in depth - see module docstring. Never establish a
        # session for a token issued by the wrong tenant.
        return _login_error_redirect("wrong_tenant")

    request.session["user"] = {
        "name": claims.get("name"),
        "email": claims.get("preferred_username") or claims.get("email"),
        "oid": claims.get("oid"),
    }
    return RedirectResponse(FRONTEND_URL)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return {"status": "signed_out"}


def get_current_user(request: Request) -> dict:
    """FastAPI dependency - protects a route by requiring a valid session.
    Use as: Depends(get_current_user)."""
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user
