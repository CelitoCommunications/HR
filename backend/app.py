"""
Flask application factory for Celito Onboarding Platform.

Creates the app, registers blueprints, sets up middleware for
security headers and CSRF protection, and serves the frontend.
"""

import logging
import os
from datetime import timedelta

from flask import Flask, request, send_from_directory, render_template, jsonify
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import config, PROJECT_DIR
from .db import init_db, migrate_db, get_or_create_session_secret

logger = logging.getLogger(__name__)

FRONTEND_DIR = os.path.join(PROJECT_DIR, "frontend")
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")

# Routes exempt from CSRF header check
CSRF_EXEMPT_PATHS = {"/auth/callback", "/login", "/logout"}


def create_app():
    """Application factory — call once at startup."""
    app = Flask(
        __name__,
        template_folder=TEMPLATES_DIR,
        static_folder=None,  # We handle static serving manually
    )

    # ── Database & session secret ─────────────────────────────────────
    init_db()
    migrate_db()
    app.secret_key = get_or_create_session_secret()
    app.permanent_session_lifetime = timedelta(hours=24)

    # ── Register blueprints ───────────────────────────────────────────
    from .auth import auth_bp
    from .routes_admin import admin_bp
    from .routes_mgr import mgr_bp
    from .routes_emp import emp_bp
    from .routes_api import api_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)   # url_prefix="/api/admin" set on Blueprint
    app.register_blueprint(mgr_bp)     # url_prefix="/api" set on Blueprint
    app.register_blueprint(emp_bp)     # url_prefix="/api/me" set on Blueprint
    app.register_blueprint(api_bp)     # url_prefix="/api/v1" — external REST API

    # ── CSRF protection middleware ────────────────────────────────────
    @app.before_request
    def csrf_check():
        if request.method in ("POST", "PUT", "DELETE"):
            if request.path not in CSRF_EXEMPT_PATHS:
                # External API uses key auth, not CSRF
                if request.path.startswith("/api/v1/"):
                    return
                if request.headers.get("X-Requested-With") != "XMLHttpRequest":
                    return jsonify({"error": "CSRF validation failed"}), 403

    # ── Security headers middleware ───────────────────────────────────
    @app.after_request
    def security_headers(response):
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data:; "
            "connect-src 'self' https://login.microsoftonline.com; "
            "frame-ancestors 'none'"
        )
        return response

    # ── Frontend serving ──────────────────────────────────────────────
    @app.route("/")
    def index():
        """Serve the single-file frontend dashboard."""
        from .auth import login_required as _lr
        # We do the auth check inline so we can redirect properly
        from flask import session, redirect, url_for
        if "user" not in session:
            return redirect(url_for("auth.login"))
        return send_from_directory(
            FRONTEND_DIR, "celito_onboarding_dashboard.html"
        )

    @app.route("/manifest.json")
    def manifest():
        """Serve the PWA web app manifest."""
        return send_from_directory(FRONTEND_DIR, "manifest.json")

    @app.route("/assets/<path:filename>")
    def serve_assets(filename):
        """Serve any additional frontend assets."""
        assets_dir = os.path.join(FRONTEND_DIR, "assets")
        return send_from_directory(assets_dir, filename)

    # ── Error handlers ────────────────────────────────────────────────
    @app.errorhandler(403)
    def forbidden(e):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Forbidden"}), 403
        return render_template("403.html", message="Access denied"), 403

    @app.errorhandler(404)
    def not_found(e):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Not found"}), 404
        return jsonify({"error": "Page not found"}), 404

    @app.errorhandler(500)
    def server_error(e):
        logger.exception("Internal server error")
        if request.path.startswith("/api/"):
            return jsonify({"error": "Internal server error"}), 500
        return jsonify({"error": "Something went wrong"}), 500

    # ── Reverse proxy support ─────────────────────────────────────
    # IIS sends X-Forwarded-Proto, X-Forwarded-Host, X-Forwarded-Prefix
    # so Flask generates correct URLs (redirects, url_for, request.url_root)
    app.wsgi_app = ProxyFix(
        app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1
    )

    logger.info("Celito Onboarding app created successfully")
    return app
