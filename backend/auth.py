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
    """Redirect to /login if no active session; block pending/disabled users.
    Also syncs the session role from the DB so role changes take effect
    without requiring re-login."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            # API and AJAX requests get a 401 so the frontend can redirect
            # cleanly; a full-page redirect to Microsoft login fails CORS
            # on fetch() calls.  Check the path first (reliable for all
            # fetch calls), then fall back to the X-Requested-With header.
            if (request.path.startswith("/api/")
                    or request.headers.get("X-Requested-With") == "XMLHttpRequest"):
                return jsonify({"error": "Not authenticated"}), 401
            return redirect(url_for("auth.login", next=request.url))

        # Sync role from DB so admin changes take effect immediately
        email = session["user"].get("email")
        if email:
            conn = get_db()
            try:
                row = conn.execute(
                    "SELECT role FROM users WHERE email = ?", (email,)
                ).fetchone()
            finally:
                conn.close()
            if row and row["role"] != session["user"].get("role"):
                session["user"]["role"] = row["role"]
                session.modified = True

        role = session["user"].get("role", "")
        if role in ("pending", "disabled"):
            return redirect(url_for("auth.access_pending"))
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
                if (request.path.startswith("/api/")
                        or request.headers.get("X-Requested-With") == "XMLHttpRequest"):
                    return jsonify({"error": "Not authenticated"}), 401
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
            # First user ever gets admin; everyone after gets pending (awaiting admin approval)
            user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            role = "admin" if user_count == 0 else "pending"
            conn.execute(
                "INSERT INTO users (email, display_name, role, last_login) VALUES (?, ?, ?, ?)",
                (email, display_name, role, now),
            )
            department = ""
        conn.commit()
    finally:
        conn.close()

    log_audit(email, "login", "user", email)
    logger.info("User logged in: %s (%s) — role: %s", display_name, email, role)

    # Pending and disabled users see the access-request page, not the app
    if role in ("pending", "disabled"):
        session.permanent = True
        session["user"] = {
            "email": email,
            "name": display_name,
            "role": role,
            "department": department or "",
        }
        return redirect(url_for("auth.access_pending"))

    # Set session
    session.permanent = True
    session["user"] = {
        "email": email,
        "name": display_name,
        "role": role,
        "department": department or "",
    }

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


@auth_bp.route("/access-pending")
def access_pending():
    """Show the 'waiting for admin approval' page for pending/disabled users."""
    user = session.get("user")
    if not user:
        return redirect(url_for("auth.login"))

    # If the user's role has been updated since login, refresh from DB
    conn = get_db()
    try:
        row = conn.execute("SELECT role FROM users WHERE email = ?", (user["email"],)).fetchone()
    finally:
        conn.close()

    if row and row["role"] not in ("pending", "disabled"):
        # Admin has approved — update session and redirect to app
        session["user"]["role"] = row["role"]
        session.modified = True
        return redirect("/")

    status = "disabled" if (row and row["role"] == "disabled") else "pending"
    name = user.get("name", user.get("email", ""))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Access Pending — Celito Onboarding</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
               background: #f0f2f5; display: flex; align-items: center; justify-content: center;
               min-height: 100vh; padding: 20px; color: #2c3e50; }}
        .card {{ background: #fff; border-radius: 12px; box-shadow: 0 4px 24px rgba(0,0,0,.08);
                max-width: 480px; width: 100%; padding: 48px 40px; text-align: center; }}
        .icon {{ font-size: 56px; margin-bottom: 16px; }}
        h1 {{ font-size: 22px; font-weight: 700; margin-bottom: 8px; }}
        .subtitle {{ font-size: 14px; color: #7f8c8d; margin-bottom: 24px; }}
        .info-box {{ background: #f8f9fa; border-radius: 8px; padding: 16px; margin-bottom: 24px;
                    font-size: 13px; color: #555; line-height: 1.6; text-align: left; }}
        .info-box strong {{ color: #2c3e50; }}
        .user-email {{ font-size: 13px; color: #95a5a6; margin-bottom: 24px; }}
        .btn {{ display: inline-block; padding: 10px 24px; border-radius: 6px; font-size: 14px;
               font-weight: 600; text-decoration: none; cursor: pointer; border: none; }}
        .btn-logout {{ background: #e9ecef; color: #495057; }}
        .btn-logout:hover {{ background: #dee2e6; }}
        .btn-retry {{ background: #3498db; color: #fff; margin-right: 8px; }}
        .btn-retry:hover {{ background: #2980b9; }}
    </style>
</head>
<body>
    <div class="card">
        <div class="icon">{"🚫" if status == "disabled" else "⏳"}</div>
        <h1>{"Account Disabled" if status == "disabled" else "Access Pending"}</h1>
        <p class="subtitle">{"Your account has been disabled by an administrator." if status == "disabled"
            else f"Welcome, {name}! Your login was successful."}</p>
        {"" if status == "disabled" else '''<div class="info-box">
            <strong>What happens next?</strong><br>
            An administrator has been notified of your access request.
            Once they assign you a role, you\\'ll be able to use the
            Onboarding Portal on your next login.<br><br>
            <strong>Need access sooner?</strong><br>
            Contact your HR department or system administrator.
        </div>'''}
        <div class="user-email">Signed in as {user.get("email", "")}</div>
        <div>
            <a href="/" class="btn btn-retry">Check Again</a>
            <a href="/logout" class="btn btn-logout">Sign Out</a>
        </div>
    </div>
</body>
</html>""", 200


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
