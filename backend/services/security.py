"""Encryption, private object storage, and staff session access controls."""

from functools import wraps
from pathlib import Path
from secrets import token_urlsafe
import hmac
import time
import base64
import hashlib
from uuid import uuid4

from cryptography.fernet import Fernet
from flask import current_app, g, jsonify, request, session
from sqlalchemy.types import TypeDecorator, Text
from werkzeug.security import check_password_hash
from email_validator import EmailNotValidError, validate_email

from backend.extensions import db
from backend.models.accounts import User


class CryptoService:
    PREFIX = "enc:v1:"

    def __init__(self, key):
        encoded = key.encode() if isinstance(key, str) else key
        self.cipher = Fernet(encoded)
        self.lookup_key = base64.urlsafe_b64decode(encoded)

    def lookup_digest(self, value):
        return hmac.new(self.lookup_key, value.encode(), hashlib.sha256).hexdigest()

    def encrypt_bytes(self, data):
        return self.cipher.encrypt(data)

    def decrypt_bytes(self, data):
        return self.cipher.decrypt(data)

    def encrypt_text(self, value):
        if value is None or value.startswith(self.PREFIX):
            return value
        return self.PREFIX + self.encrypt_bytes(value.encode()).decode()

    def decrypt_text(self, value):
        if value is None or not value.startswith(self.PREFIX):
            return value
        return self.decrypt_bytes(value[len(self.PREFIX):].encode()).decode()


class EncryptedText(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return current_app.extensions["crypto"].encrypt_text(value)

    def process_result_value(self, value, dialect):
        return current_app.extensions["crypto"].decrypt_text(value)


class EncryptedStorage:
    """Only opaque generated identifiers can address private encrypted files."""

    def _path(self, key):
        if not isinstance(key, str) or not key or any(c not in "0123456789abcdef-" for c in key):
            raise ValueError("Invalid private object identifier.")
        directory = Path(current_app.config["STORAGE_DIR"]).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        return directory / key

    def save(self, data):
        key = str(uuid4())
        self._path(key).write_bytes(current_app.extensions["crypto"].encrypt_bytes(data))
        return key

    def read(self, key):
        return current_app.extensions["crypto"].decrypt_bytes(self._path(key).read_bytes())

    def delete(self, key):
        self._path(key).unlink(missing_ok=True)


def initialize_crypto(app):
    key = app.config.get("DATA_ENCRYPTION_KEY", "")
    if not key:
        if app.config.get("PRODUCTION"):
            raise RuntimeError("DATA_ENCRYPTION_KEY is required in production.")
        # Development and test data are encrypted with a key derived from the
        # local Flask secret. Production must always provide an independent key.
        # This avoids creating a portable key file beside the application data.
        key = base64.urlsafe_b64encode(
            hashlib.sha256(app.config["SECRET_KEY"].encode()).digest()
        )
    app.extensions["crypto"] = CryptoService(key)


def staff_required(view=None, *, roles=None):
    def decorate(function):
        @wraps(function)
        def protected(*args, **kwargs):
            if not session.get("staff_username"):
                return jsonify(error={"code": "AUTH_REQUIRED", "message": "Please sign in."}), 401
            if roles and session.get("staff_role") not in roles:
                return jsonify(error={"code": "FORBIDDEN", "message": "Your role cannot perform this action."}), 403
            return function(*args, **kwargs)
        return protected
    return decorate(view) if view is not None else decorate


def current_participant_id():
    """Return only the participant identity verified from the server session."""
    return session.get("participant_user_id")


def participant_required(view=None):
    """Protect participant APIs and enforce CSRF on state-changing requests."""

    def decorate(function):
        @wraps(function)
        def protected(*args, **kwargs):
            user_id = current_participant_id()
            user = db.session.get(User, user_id) if user_id else None
            if user is None or not user.is_active:
                session.pop("participant_user_id", None)
                session.pop("participant_csrf_token", None)
                return (
                    jsonify(
                        error={
                            "code": "AUTH_REQUIRED",
                            "message": "Please sign in to continue.",
                        }
                    ),
                    401,
                )
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                supplied = request.headers.get("X-CSRF-Token", "")
                expected = session.get("participant_csrf_token", "")
                if not expected or not hmac.compare_digest(supplied, expected):
                    return (
                        jsonify(
                            error={
                                "code": "CSRF_REQUIRED",
                                "message": "Please refresh and sign in again.",
                            }
                        ),
                        403,
                    )
            g.participant_user = user
            return function(*args, **kwargs)

        return protected

    return decorate(view) if view is not None else decorate


def register_participant_auth(app):
    """Register participant account and session endpoints."""
    failures = {}

    def state(user=None):
        user = user or (
            db.session.get(User, current_participant_id())
            if current_participant_id()
            else None
        )
        if user is None or not user.is_active:
            return {"authenticated": False, "user": None, "csrf_token": None}
        return {
            "authenticated": True,
            "user": {
                "id": user.id,
                "email": user.email,
                "full_name": user.full_name,
            },
            "csrf_token": session.get("participant_csrf_token"),
        }

    def start_session(user):
        session.clear()
        session.update(
            participant_user_id=user.id,
            participant_csrf_token=token_urlsafe(32),
        )
        session.permanent = True
        return state(user)

    def normalized_credentials():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return None, None, "Request body must be a JSON object."
        try:
            email = validate_email(
                str(body.get("email", "")).strip(), check_deliverability=False
            ).normalized.casefold()
        except EmailNotValidError:
            return None, None, "Please enter a valid email address."
        password = body.get("password")
        if not isinstance(password, str) or not 8 <= len(password) <= 128:
            return None, None, "Password must contain 8 to 128 characters."
        return email, password, None

    @app.post("/api/auth/register")
    def participant_register():
        email, password, error = normalized_credentials()
        body = request.get_json(silent=True) or {}
        full_name = " ".join(str(body.get("full_name", "")).split())
        if error:
            return jsonify(error={"code": "INVALID_REGISTRATION", "message": error}), 400
        if len(full_name) < 2 or len(full_name) > 255 or not any(
            character.isalpha() for character in full_name
        ):
            return (
                jsonify(
                    error={
                        "code": "INVALID_REGISTRATION",
                        "message": "Please enter your full name.",
                    }
                ),
                400,
            )
        if User.query.filter_by(email=email).first() is not None:
            return (
                jsonify(
                    error={
                        "code": "ACCOUNT_EXISTS",
                        "message": "An account already exists for this email address.",
                    }
                ),
                409,
            )
        user = User(email=email, full_name=full_name)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        return jsonify(start_session(user)), 201

    @app.post("/api/auth/login")
    def participant_login():
        email, password, error = normalized_credentials()
        address = request.remote_addr or "unknown"
        now = time.monotonic()
        attempts = [timestamp for timestamp in failures.get(address, []) if now - timestamp < 900]
        failures[address] = attempts
        if len(attempts) >= 10:
            return (
                jsonify(
                    error={
                        "code": "RATE_LIMITED",
                        "message": "Too many sign-in attempts. Try again later.",
                    }
                ),
                429,
            )
        user = User.query.filter_by(email=email).one_or_none() if not error else None
        if error or user is None or not user.is_active or not user.check_password(password):
            failures[address].append(now)
            return (
                jsonify(
                    error={
                        "code": "INVALID_LOGIN",
                        "message": "Invalid email or password.",
                    }
                ),
                401,
            )
        failures.pop(address, None)
        return jsonify(start_session(user))

    @app.get("/api/auth/session")
    def participant_session():
        return jsonify(state())

    @app.post("/api/auth/logout")
    @participant_required
    def participant_logout():
        session.pop("participant_user_id", None)
        session.pop("participant_csrf_token", None)
        return jsonify(authenticated=False)


def register_staff_auth(app):
    failures = {}

    def state():
        return {"authenticated": bool(session.get("staff_username")),
                "username": session.get("staff_username"), "role": session.get("staff_role"),
                "csrf_token": session.get("csrf_token")}

    @app.before_request
    def csrf_check():
        if request.path.startswith("/api/staff/") and request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("Origin")
            allowed = set(app.config["CORS_ORIGINS"]) | {request.host_url.rstrip("/")}
            if origin and origin not in allowed:
                return jsonify(error={"code": "INVALID_ORIGIN", "message": "Request origin is not allowed."}), 403
            if request.path != "/api/staff/login" and session.get("staff_username"):
                token = request.headers.get("X-CSRF-Token", "")
                if not hmac.compare_digest(token, session.get("csrf_token", "")):
                    return jsonify(error={"code": "CSRF_REQUIRED", "message": "Please refresh and sign in again."}), 403

    @app.post("/api/staff/login")
    def staff_login():
        body = request.get_json(silent=True) or {}
        address = request.remote_addr or "unknown"
        now = time.monotonic()
        attempts = [t for t in failures.get(address, []) if now - t < 900]
        failures[address] = attempts
        if len(attempts) >= 10:
            return jsonify(error={"code": "RATE_LIMITED", "message": "Too many sign-in attempts. Try again later."}), 429
        username, password = body.get("username", ""), body.get("password", "")
        accounts = app.config.get("STAFF_ACCOUNTS", {})
        account = accounts.get(username) if isinstance(username, str) else None
        if not account or not isinstance(password, str) or not check_password_hash(account["password_hash"], password):
            failures[address].append(now)
            return jsonify(error={"code": "INVALID_LOGIN", "message": "Invalid username or password."}), 401
        failures.pop(address, None)
        session.clear()
        session.update(staff_username=username, staff_role=account["role"], csrf_token=token_urlsafe(32))
        session.permanent = True
        return jsonify(state())

    @app.get("/api/staff/session")
    def staff_session():
        return jsonify(state())

    @app.post("/api/staff/logout")
    def staff_logout():
        session.clear()
        return jsonify(authenticated=False)

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response
