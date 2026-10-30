"""Environment-backed configuration for the Q&M chatbot API."""

import os
import json
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv


BACKEND_DIR = Path(__file__).resolve().parent
load_dotenv(BACKEND_DIR / ".env")


def _database_url():
    configured = os.getenv("DATABASE_URL", "").strip()
    if configured.startswith("postgres://"):
        return configured.replace("postgres://", "postgresql://", 1)
    if configured:
        return configured

    sqlite_path = (BACKEND_DIR / "data" / "qm_chatbot.db").as_posix()
    return f"sqlite:///{sqlite_path}"


def _json_setting(name, default):
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{name} must contain valid JSON.") from error


def _boolean_setting(name, default=False):
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


class Config:
    """Settings shared by local development and production deployments."""

    SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "development-only-change-me")
    PORT = int(os.getenv("FLASK_PORT", "5000"))
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "").strip()
    SQLALCHEMY_DATABASE_URI = _database_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    CONVERSATION_CONTEXT_MESSAGE_LIMIT = int(
        os.getenv("CONVERSATION_CONTEXT_MESSAGE_LIMIT", "20")
    )
    CORS_ORIGINS = tuple(
        value.strip()
        for value in os.getenv(
            "CORS_ORIGINS",
            "http://127.0.0.1:5173,http://localhost:5173",
        ).split(",")
        if value.strip()
    )
    PRODUCTION = os.getenv("APP_ENV", "development") == "production"
    # Fictional dates are convenient in local development but must never be
    # presented as approved production intakes.
    ENABLE_DEMO_INTAKES = (
        False if PRODUCTION else _boolean_setting("ENABLE_DEMO_INTAKES", True)
    )
    MINIMUM_ENROLLMENT_AGE = int(os.getenv("MINIMUM_ENROLLMENT_AGE", "16"))
    MAXIMUM_ENROLLMENT_AGE = int(os.getenv("MAXIMUM_ENROLLMENT_AGE", "100"))
    ENROLLMENT_TIMEZONE = os.getenv("ENROLLMENT_TIMEZONE", "Asia/Singapore")
    MOBILE_COUNTRY_CODE = os.getenv("MOBILE_COUNTRY_CODE", "65")
    MAX_CONTENT_LENGTH = 10 * 1024 * 1024
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = PRODUCTION
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)

    DATA_ENCRYPTION_KEY = os.getenv("DATA_ENCRYPTION_KEY", "").strip()
    STORAGE_DIR = os.getenv("STORAGE_DIR", "").strip() or str(
        BACKEND_DIR / "data" / "private"
    )
    MASTER_INVOICE_PATH = os.getenv("MASTER_INVOICE_PATH", "").strip() or str(
        Path(STORAGE_DIR) / "Master_Invoice_List.xlsx"
    )
    MASTER_INVOICE_TEMPLATE_PATH = os.getenv(
        "MASTER_INVOICE_TEMPLATE_PATH", ""
    ).strip()
    MASTER_INVOICE_LOCK_TIMEOUT_SECONDS = float(
        os.getenv("MASTER_INVOICE_LOCK_TIMEOUT_SECONDS", "10")
    )
    MASTER_INVOICE_NUMBER_PREFIX = (
        os.getenv("MASTER_INVOICE_NUMBER_PREFIX", "DEMO-INV").strip()
        or "DEMO-INV"
    )
    INVOICE_GENERATION_ENABLED = _boolean_setting(
        "INVOICE_GENERATION_ENABLED", True
    )
    INVOICE_TEMPLATE_SOURCE_PATH = os.getenv(
        "INVOICE_TEMPLATE_SOURCE_PATH", ""
    ).strip() or str(
        BACKEND_DIR.parent
        / "docs"
        / "source-materials"
        / "company-workflow"
        / "FW_ Agentive Workflow for Dental Assisting Course Administration.eml"
    )
    INVOICE_TEMPLATE_ATTACHMENT_NAME = os.getenv(
        "INVOICE_TEMPLATE_ATTACHMENT_NAME", "College & CN Template.xlsx"
    ).strip()
    # This must stay false until Q&M approves the template's embedded payment,
    # deadline and refund wording for customer-facing use.
    INVOICE_TEMPLATE_APPROVED = _boolean_setting(
        "INVOICE_TEMPLATE_APPROVED", False
    )
    INVOICE_LOCK_TIMEOUT_SECONDS = float(
        os.getenv("INVOICE_LOCK_TIMEOUT_SECONDS", "10")
    )
    STAFF_ACCOUNTS = _json_setting("STAFF_ACCOUNTS_JSON", {})
    AUTOMATION_API_TOKEN = os.getenv("AUTOMATION_API_TOKEN", "").strip()
    CONSENT_VERSION = os.getenv("CONSENT_VERSION", "2026-09")
    RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "365"))
    FOLLOWUP_HOURS = tuple(
        int(value)
        for value in os.getenv("FOLLOWUP_HOURS", "48,96,144").split(",")
    )
    # False: leads who message first may receive reminders until they reply STOP.
    FOLLOWUP_REQUIRE_EXPLICIT_CONSENT = _boolean_setting(
        "FOLLOWUP_REQUIRE_EXPLICIT_CONSENT", False
    )

    EMAIL_DELIVERY_ENABLED = _boolean_setting("EMAIL_DELIVERY_ENABLED", False)
    EMAIL_PROVIDER = os.getenv("EMAIL_PROVIDER", "smtp").strip().casefold()
    EMAIL_FROM_NAME = os.getenv("EMAIL_FROM_NAME", "Q&M Training").strip()
    EMAIL_FROM_ADDRESS = os.getenv("EMAIL_FROM_ADDRESS", "").strip()
    EMAIL_REPLY_TO = os.getenv("EMAIL_REPLY_TO", "").strip()
    EMAIL_LOGO_PATH = os.getenv("EMAIL_LOGO_PATH", "").strip() or str(
        BACKEND_DIR / "assets" / "email" / "qm-training-logo.png"
    )
    EMAIL_ATTACHMENT_MAX_BYTES = int(
        os.getenv("EMAIL_ATTACHMENT_MAX_BYTES", str(10 * 1024 * 1024))
    )
    SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
    SMTP_FROM = os.getenv("SMTP_FROM", EMAIL_FROM_ADDRESS).strip()
    SMTP_USERNAME = os.getenv("SMTP_USERNAME", "").strip()
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
    SMTP_USE_TLS = _boolean_setting("SMTP_USE_TLS", True)
    SMTP_STARTTLS = _boolean_setting("SMTP_STARTTLS", SMTP_USE_TLS)
    SMTP_TIMEOUT_SECONDS = int(os.getenv("SMTP_TIMEOUT_SECONDS", "30"))
    INVOICE_EMAIL_MAX_RETRIES = int(os.getenv("INVOICE_EMAIL_MAX_RETRIES", "3"))
    INVOICE_EMAIL_RESEND_COOLDOWN_SECONDS = int(
        os.getenv("INVOICE_EMAIL_RESEND_COOLDOWN_SECONDS", "60")
    )
    EMAIL_LIVE_TEST_ENABLED = _boolean_setting("EMAIL_LIVE_TEST_ENABLED", False)
    EMAIL_TEST_RECIPIENT = os.getenv("EMAIL_TEST_RECIPIENT", "").strip()

    WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip()
    WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET", "").strip()
    WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "").strip()
    WHATSAPP_API_VERSION = os.getenv("WHATSAPP_API_VERSION", "v23.0")
    WHATSAPP_FOLLOWUP_TEMPLATE = os.getenv("WHATSAPP_FOLLOWUP_TEMPLATE", "")
    WHATSAPP_TEMPLATE_LANGUAGE = os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "en")

    PAYMENT_AUTO_CONFIRM = False
    PAYMENT_VISION_MODEL = os.getenv("PAYMENT_VISION_MODEL", "gpt-4o-mini").strip()
    PAYMENT_CONFIDENCE_THRESHOLD = float(
        os.getenv("PAYMENT_CONFIDENCE_THRESHOLD", "0.98")
    )
    PAYMENT_DUE_DAYS = int(os.getenv("PAYMENT_DUE_DAYS", "14"))
    PAYNOW_RECIPIENT_ALIASES = _json_setting("PAYNOW_RECIPIENT_ALIASES_JSON", [])
    ACCOUNTANT_REPORT_EMAIL = os.getenv("ACCOUNTANT_REPORT_EMAIL", "").strip()
    STAFF_ALERT_EMAIL = os.getenv("STAFF_ALERT_EMAIL", "").strip()

    INVOICE_SIGNATURE_PATH = os.getenv("INVOICE_SIGNATURE_PATH", "").strip()
    FINANCE_SIGNATURE_PATH = os.getenv(
        "FINANCE_SIGNATURE_PATH", INVOICE_SIGNATURE_PATH
    ).strip()
    PAYNOW_QR_PATH = os.getenv("PAYNOW_QR_PATH", "").strip()
    SKILLSFUTURE_INSTRUCTIONS_PATH = os.getenv(
        "SKILLSFUTURE_INSTRUCTIONS_PATH", ""
    ).strip()
    COMPANY_STAMP_PATH = os.getenv("COMPANY_STAMP_PATH", "").strip()
    FINANCE_COMPANY_NAME = os.getenv(
        "FINANCE_COMPANY_NAME", "Q&M Dental Group (Singapore) Limited"
    )
    RETENTION_ENABLED = os.getenv("RETENTION_ENABLED", "false").lower() == "true"
