"""Authentication module for the Arena-Odoo Sync Dashboard.

Simple single-user login using flask-login. Credentials come from
environment variables ADMIN_USER and ADMIN_PASSWORD.
"""

import hmac
import os
import secrets
import time
from collections import defaultdict

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user

login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = ""


@login_manager.unauthorized_handler
def unauthorized():
    """Return 401 JSON for API calls, redirect to login for browser requests."""
    if request.path.startswith("/api/"):
        return jsonify({"error": "Authentication required"}), 401
    return redirect(url_for("auth.login", next=request.path))

auth_bp = Blueprint("auth", __name__)

# Simple rate limiting: max 5 failed login attempts per IP per 60 seconds
_login_attempts: dict[str, list[float]] = defaultdict(list)
_RATE_LIMIT_MAX = 5
_RATE_LIMIT_WINDOW = 60  # seconds


def _is_rate_limited(ip: str) -> bool:
    """Check if an IP has exceeded the login attempt limit."""
    now = time.time()
    attempts = _login_attempts[ip]
    # Remove old attempts outside the window
    _login_attempts[ip] = [t for t in attempts if now - t < _RATE_LIMIT_WINDOW]
    return len(_login_attempts[ip]) >= _RATE_LIMIT_MAX


def _record_attempt(ip: str) -> None:
    _login_attempts[ip].append(time.time())


class AdminUser(UserMixin):
    def __init__(self):
        self.id = "admin"


def _get_credentials():
    """Return (username, password) from environment variables."""
    return (
        os.getenv("ADMIN_USER", "admin"),
        os.getenv("ADMIN_PASSWORD", ""),
    )


@login_manager.user_loader
def load_user(user_id):
    if user_id == "admin":
        return AdminUser()
    return None


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("page_dashboard"))

    error = None
    if request.method == "POST":
        # Rate limiting
        client_ip = request.remote_addr or "unknown"
        if _is_rate_limited(client_ip):
            error = "Too many login attempts. Please wait a minute."
            session["csrf_token"] = secrets.token_hex(32)
            return render_template("login.html", error=error, csrf_token=session["csrf_token"])

        # Verify CSRF token
        token = request.form.get("_csrf_token", "")
        expected_token = session.get("csrf_token", "")
        if not token or not hmac.compare_digest(token, expected_token):
            error = "Invalid request. Please try again."
        else:
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            expected_user, expected_pass = _get_credentials()

            if not expected_pass:
                error = "ADMIN_PASSWORD not set. Configure it in .env or environment variables."
            elif hmac.compare_digest(username, expected_user) and hmac.compare_digest(password, expected_pass):
                login_user(AdminUser(), remember=True)
                next_page = request.args.get("next", "/")
                if not next_page.startswith("/") or next_page.startswith("//"):
                    next_page = "/"
                return redirect(next_page)
            else:
                _record_attempt(client_ip)
                error = "Invalid credentials"

    # Generate CSRF token for the login form
    session["csrf_token"] = secrets.token_hex(32)
    return render_template("login.html", error=error, csrf_token=session["csrf_token"])


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))
