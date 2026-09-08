"""
Microsoft Entra ID SSO authentication for Celito Onboarding Platform.

Uses MSAL (Microsoft Authentication Library) for OAuth 2.0 Authorization Code flow.
Provides login_required and role_required decorators for route protection.
"""

import functools
import logging
from datetime import datetime, timedelta

import msal
import requests
from flask import (
    Blueprint, abort, redirect, render_template, request, session, url_for, jsonify,
)

from .config import config
from .db import get_db, log_audit

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__)

GRAPH_ME_URL = "https://graph.microsoft.com/v1.0/me"
AUTHORITY = None  # Set in init_auth()
_msal_app = None


# ──────────────────────────────────────────────────────────────────────
# MSAL initialization
# ──────────────────────────────────────────────────────────────────────

def _get_msal_app():
    """Lazily create and cache the MSAL ConfidentialClientApplication."""
    global _msal_app, AUTHORITY
    if _msal_app is None:
        tenant_id = config.get("entra.tenant_id", "")
        AUTHORITY = f"https://login.microsoftonline.com/{tenant_id}"
        _msal_app = msal.ConfidentialClientApplication(
            client_id=config.get("entra.client_id", ""),
            client_credential=config.get("entra.client_secret", ""),
            authority=AUTHORITY,
        )
    return _msal_app


# ──────────────────────────────────────────────────────────────────────
# Decorators
# ──────────────────────────────────────────────────────────────────────

def login_required(f):
    """Redirect to /login if no active session."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("auth.login", next=request.url))
        return f(*args, **kwargs)
    return decorated


def role_required(*allowed_roles):
    """
    Check the current user's role against the database.
    Usage: @role_required('admin', 'hr')  OR  @role_required(['admin', 'hr'])
    """
    # Support both @role_required('a','b') and @role_required(['a','b'])
    if len(allowed_roles) == 1 and isinstance(allowed_roles[0], (list, tuple)):
        allowed_roles = tuple(allowed_roles[0])

    def decorator(f):
        @functools.wraps(f)
        def decorated(*args, **kwargs):
            if "user" not in session:
                return redirect(url_for("auth.login", next=request.url))

            user_email = session["user"]["email"]
            conn = get_db()
            try:
                row = conn.execute(
                    "SELECT role FROM users WHERE email = ?", (user_email,)
                ).fetchone()
            finally:
                conn.close()

            if not row or row["role"] not in allowed_roles:
                abort(403)
            return f(*args, **kwargs)
        return decorated
    return decorator


# ──────────────────────────────────────────────────────────────────────
# Current user helper
# ──────────────────────────────────────────────────────────────────────

def get_current_user():
    """
    Return the current user dict from the session.

    If the admin is impersonating (via ?as=Name), return the impersonated
    user's info while keeping `_real_user` for audit purposes.
    """
    if "user" not in session:
        return None

    user = dict(session["user"])

    # Admin impersonation
    impersonate_name = request.args.get("as")
    if impersonate_name and user.get("role") == "admin":
        conn = get_db()
        try:
            # Try users table first
            row = conn.execute(
                "SELECT email, display_name, role, department FROM users WHERE display_name = ?",
                (impersonate_name,),
            ).fetchone()

            # Fall back to employees table
            if not row:
                emp = conn.execute(
                    "SELECT email, first_name || ' ' || last_name AS display_name, "
                    "'employee' AS role, department FROM employees "
                    "WHERE first_name || ' ' || last_name = ?",
                    (impersonate_name,),
                ).fetchone()
                if emp:
                    row = emp
        finally:
            conn.close()

        if row:
            user = {
                "email": row["email"],
                "name": row["display_name"],
                "role": row["role"],
                "department": row["department"] or "",
                "_real_user": session["user"],
                "_impersonating": True,
            }

    return user


# ──────────────────────────────────────────────────────────────────────
# URL safety check
# ──────────────────────────────────────────────────────────────────────

def _is_safe_redirect(url):
    """Only allow relative paths — prevent open redirect attacks."""
    if not url:
        return False
    # Reject absolute URLs, protocol-relative URLs, and javascript:
    if url.startswith(("http://", "https://", "//", "javascript:", "data:")):
        return False
    # Must start with /
    return url.startswith("/")


# ──────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────

@auth_bp.route("/login")
def login():
    """Initiate the Microsoft Entra ID sign-in flow."""
    app = _get_msal_app()
    redirect_uri = config.get("entra.redirect_uri", "")

    auth_url = app.get_authorization_request_url(
        scopes=["User.Read"],
        redirect_uri=redirect_uri,
        state=request.args.get("next", "/"),
    )
    return redirect(auth_url)


@auth_bp.route("/auth/callback")
def auth_callback():
    """Handle the OAuth callback from Microsoft Entra ID."""
    code = request.args.get("code")
    if not code:
        logger.warning("Auth callback received without authorization code")
        return redirect(url_for("auth.login"))

    app = _get_msal_app()
    redirect_uri = config.get("entra.redirect_uri", "")

    result = app.acquire_token_by_authorization_code(
        code,
        scopes=["User.Read"],
        redirect_uri=redirect_uri,
    )

    if "error" in result:
        logger.error("Token acquisition failed: %s - %s",
                      result.get("error"), result.get("error_description"))
        return render_template("403.html", message="Authentication failed"), 403

    # Fetch user profile from Microsoft Graph
    access_token = result.get("access_token")
    headers = {"Authorization": f"Bearer {access_token}"}
    graph_resp = requests.get(GRAPH_ME_URL, headers=headers, timeout=10)

    if graph_resp.status_code != 200:
        logger.error("Microsoft Graph /me failed: %s", graph_resp.text)
        return render_template("403.html", message="Could not retrieve user profile"), 403

    profile = graph_resp.json()
    email = (profile.get("mail") or profile.get("userPrincipalName", "")).lower()
    display_name = profile.get("displayName", email)

    if not email:
        return render_template("403.html", message="No email address found in profile"), 403

    # Upsert user in database
    conn = get_db()
    try:
        existing = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        now = datetime.utcnow().isoformat()

        if existing:
            conn.execute(
                "UPDATE users SET display_name = ?, last_login = ? WHERE email = ?",
                (display_name, now, email),
            )
            role = existing["role"]
            department = existing["department"]
        else:
            # First user ever gets admin; everyone after gets employee
            user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            role = "admin" if user_count == 0 else "employee"
            conn.execute(
                "INSERT INTO users (email, display_name, role, last_login) VALUES (?, ?, ?, ?)",
                (email, display_name, role, now),
            )
            department = ""
        conn.commit()
    finally:
        conn.close()

    # Set session
    session.permanent = True
    session["user"] = {
        "email": email,
        "name": display_name,
        "role": role,
        "department": department or "",
    }

    log_audit(email, "login", "user", email)
    logger.info("User logged in: %s (%s)", display_name, email)

    # Redirect to the originally-requested page or home (validate to prevent open redirect)
    next_url = request.args.get("state", "/")
    if not _is_safe_redirect(next_url):
        next_url = "/"
    return redirect(next_url)


@auth_bp.route("/logout")
def logout():
    """Clear local session and redirect to Microsoft logout."""
    user = session.get("user", {})
    email = user.get("email", "unknown")
    log_audit(email, "logout", "user", email)

    session.clear()

    tenant_id = config.get("entra.tenant_id", "")
    post_logout_url = request.url_root.rstrip("/")
    logout_url = (
        f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/logout"
        f"?post_logout_redirect_uri={post_logout_url}"
    )
    return redirect(logout_url)


@auth_bp.route("/api/me")
def api_me():
    """Return current user info as JSON (for the frontend)."""
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401

    return jsonify({
        "email": user["email"],
        "name": user["name"],
        "role": user["role"],
        "department": user.get("department", ""),
        "impersonating": user.get("_impersonating", False),
        "real_user": user.get("_real_user", {}).get("name", "") if user.get("_impersonating") else "",
    })


@auth_bp.route("/api/session/refresh", methods=["POST"])
def session_refresh():
    """
    Refresh the session expiry timer without re-authenticating.

    The frontend calls this when the user clicks 'Extend Session' or
    periodically while the user is active, so that the 24-hour timeout
    resets from the moment of last activity rather than from login.
    """
    if "user" not in session:
        return jsonify({"error": "Not authenticated"}), 401

    # Re-mark the session as permanent so Flask resets the expiry cookie
    session.permanent = True
    session.modified = True
    return jsonify({"ok": True, "message": "Session extended"})
