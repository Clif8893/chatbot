"""Idempotent invoice-email outbox built on the existing encrypted delivery system."""

from __future__ import annotations

import hashlib
import html
import json
import smtplib
import socket
import ssl
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from email_validator import EmailNotValidError, validate_email
from flask import current_app
from reportlab.pdfgen import canvas
from sqlalchemy.exc import IntegrityError

from backend.extensions import db
from backend.models import EnrollmentDraft, utc_now
from backend.models.communications import OutboundMessage
from backend.models.finance import FinanceAudit, InvoiceGeneration
from backend.services.delivery_service import (
    EmailConfigurationError,
    EmailRecipientError,
    SMTPDeliveryError,
)


ELIGIBLE_ENROLLMENT_STATUSES = {
    "confirmed",
    "enrolled",
    "invoice_sent",
    "awaiting_payment",
    "paid",
    "receipt_issued",
}
INVOICE_EMAIL_TYPE = "invoice"
COURSE_EMAIL_NAME = "2-Day Basic Certificate in Dental Assisting"


class InvoiceEmailError(RuntimeError):
    """Safe invoice-email workflow failure."""


class ResendCooldownError(InvoiceEmailError):
    """Raised when a participant retries within the configured cooldown."""


class InvoiceEmailService:
    def __init__(self, delivery, invoice_templates):
        self.delivery = delivery
        self.invoice_templates = invoice_templates

    def queue_automatic(self, enrollment, generation):
        """Queue at most one automatic delivery for a generated PDF version."""
        if not current_app.config.get("EMAIL_DELIVERY_ENABLED"):
            return None
        enrollment, generation = self._eligible(enrollment, generation)
        key = self._automatic_key(generation)
        existing = OutboundMessage.query.filter_by(idempotency_key=key).one_or_none()
        if existing:
            return existing
        try:
            row = self._queue(
                enrollment,
                generation,
                actor="system",
                action="invoice_email_automatic_queued",
                idempotency_key=key,
                resend_count=0,
            )
            db.session.commit()
        except IntegrityError:
            # A concurrent confirmer may have inserted the same unique
            # idempotency key between our lookup and flush.
            db.session.rollback()
            return OutboundMessage.query.filter_by(idempotency_key=key).one()
        if row.status == "pending":
            self.process(row.id)
        return row

    def participant_resend(self, enrollment):
        generation = self.invoice_templates.for_enrollment(enrollment.id)
        enrollment, generation = self._eligible(enrollment, generation)
        latest = self.latest(enrollment.id)
        current_recipient = self._recipient(enrollment.email)
        if latest and latest.recipient.casefold() != current_recipient:
            raise InvoiceEmailError(
                "Your enrollment email has changed. Staff must verify it before "
                "resending the invoice."
            )
        cooldown = int(
            current_app.config.get("INVOICE_EMAIL_RESEND_COOLDOWN_SECONDS", 60)
        )
        if latest and _aware(latest.created_at) > _aware(utc_now()) - timedelta(
            seconds=cooldown
        ):
            raise ResendCooldownError(
                f"Please wait {cooldown} seconds before requesting another invoice email."
            )
        row = self._queue_manual(
            enrollment, generation, actor="participant", action="invoice_email_participant_resend"
        )
        db.session.commit()
        if row.status == "pending":
            self.process(row.id)
        return row

    def staff_resend(self, enrollment, actor):
        generation = self.invoice_templates.for_enrollment(enrollment.id)
        enrollment, generation = self._eligible(enrollment, generation)
        row = self._queue_manual(
            enrollment, generation, actor=actor, action="invoice_email_staff_resend"
        )
        db.session.commit()
        if row.status == "pending":
            self.process(row.id)
        return row

    def staff_retry(self, delivery_id, actor, enrollment_id=None):
        row = db.session.get(OutboundMessage, delivery_id)
        if (
            row is None
            or row.email_type != INVOICE_EMAIL_TYPE
            or (enrollment_id is not None and row.enrollment_id != enrollment_id)
        ):
            raise LookupError("Invoice email delivery was not found")
        if row.status not in {"failed", "disabled", "sending"}:
            raise InvoiceEmailError("Only failed or interrupted invoice emails can be retried.")
        generation = db.session.get(InvoiceGeneration, row.invoice_generation_id)
        enrollment = db.session.get(EnrollmentDraft, row.enrollment_id)
        self._eligible(enrollment, generation)
        row.status = "pending" if self._configured() else "disabled"
        row.error = None if row.status == "pending" else self._configuration_message()
        row.failure_category = None if row.status == "pending" else "configuration"
        row.queued_at = utc_now()
        self._audit(enrollment, "invoice_email_staff_retry", actor, delivery_id=row.id)
        db.session.commit()
        if row.status == "pending":
            self.process(row.id)
        return row

    def process(self, delivery_id=None, limit=100):
        """Process pending invoice emails; never scans enrollments to create new jobs."""
        self._recover_stuck()
        query = db.select(OutboundMessage).where(
            OutboundMessage.email_type == INVOICE_EMAIL_TYPE,
            OutboundMessage.status == "pending",
        )
        if delivery_id:
            query = query.where(OutboundMessage.id == delivery_id)
        rows = list(
            db.session.execute(
                query.order_by(OutboundMessage.queued_at, OutboundMessage.created_at).limit(limit)
            ).scalars()
        )
        processed = 0
        for candidate in rows:
            claimed = db.session.execute(
                db.update(OutboundMessage)
                .where(
                    OutboundMessage.id == candidate.id,
                    OutboundMessage.status == "pending",
                )
                .values(
                    status="sending",
                    attempts=OutboundMessage.attempts + 1,
                    last_attempt_at=utc_now(),
                )
            )
            db.session.commit()
            if claimed.rowcount != 1:
                continue
            row = db.session.get(OutboundMessage, candidate.id)
            enrollment = db.session.get(EnrollmentDraft, row.enrollment_id)
            self._audit(
                enrollment,
                "invoice_email_delivery_attempt",
                "system",
                delivery_id=row.id,
                attempt=row.attempts,
            )
            try:
                if not self._configured():
                    raise EmailConfigurationError(self._configuration_message())
                self.delivery._email(row)
                row.status = "sent"
                row.sent_at = utc_now()
                row.error = None
                row.failure_category = None
                self._audit(
                    enrollment,
                    "invoice_email_sent",
                    "system",
                    delivery_id=row.id,
                    attempt=row.attempts,
                )
            except EmailConfigurationError:
                self._fail(row, enrollment, "disabled", "configuration")
            except smtplib.SMTPAuthenticationError:
                self._fail(row, enrollment, "failed", "authentication")
            except EmailRecipientError:
                self._fail(row, enrollment, "failed", "recipient")
            except SMTPDeliveryError as error:
                if error.category == "smtp_timeout":
                    self._transient(row, enrollment, "timeout")
                elif error.category in {
                    "smtp_authentication_failed",
                    "gmail_app_password_or_account_action_required",
                    "smtp_tls_or_authentication_required",
                }:
                    self._fail(row, enrollment, "failed", "authentication")
                else:
                    self._transient(row, enrollment, "transport")
            except (socket.timeout, TimeoutError):
                self._transient(row, enrollment, "timeout")
            except (ssl.SSLError, smtplib.SMTPException, OSError):
                self._transient(row, enrollment, "transport")
            except Exception:
                self._transient(row, enrollment, "provider")
            db.session.commit()
            processed += 1
        return processed

    def latest(self, enrollment_id):
        return db.session.execute(
            db.select(OutboundMessage)
            .where(
                OutboundMessage.enrollment_id == enrollment_id,
                OutboundMessage.email_type == INVOICE_EMAIL_TYPE,
            )
            .order_by(OutboundMessage.created_at.desc(), OutboundMessage.id.desc())
        ).scalars().first()

    @staticmethod
    def serialize_delivery(row):
        return {
            "id": row.id,
            "channel": row.channel,
            "status": row.status,
            "purpose": row.purpose,
            "email_type": row.email_type,
            "invoice_number": row.invoice_number,
            "conversation_id": row.conversation_id,
            "recipient_masked": _mask_email(row.recipient),
            "attempts": row.attempts,
            "resend_count": row.resend_count,
            "failure_category": row.failure_category,
            "sent_at": _iso(row.sent_at),
            "can_retry": row.status in {"failed", "disabled", "sending"},
        }

    def serialize(self, enrollment, generation=None):
        if enrollment is None:
            return None
        generation = generation or self.invoice_templates.for_enrollment(enrollment.id)
        if generation is None or generation.status != "generated":
            return {
                "status": "invoice_pending",
                "recipient_masked": _mask_email(enrollment.email),
                "message": "Invoice must be generated first.",
                "can_resend": False,
            }
        row = self.latest(enrollment.id)
        if row is None and not self._configured():
            return {
                "status": "disabled",
                "recipient_masked": _mask_email(enrollment.email),
                "message": "Email delivery is not configured.",
                "can_resend": False,
            }
        if row is None:
            return {
                "status": "pending",
                "recipient_masked": _mask_email(enrollment.email),
                "message": "Preparing invoice email…",
                "can_resend": True,
            }
        messages = {
            "pending": "Preparing invoice email…",
            "sending": "Preparing invoice email…",
            "sent": f"Invoice emailed to {_mask_email(row.recipient)}",
            "failed": "Invoice email failed. You can still download it or request another attempt.",
            "disabled": "Email delivery is not configured.",
        }
        return {
            "id": row.id,
            "status": row.status,
            "recipient_masked": _mask_email(row.recipient),
            "message": messages.get(row.status, "Invoice email status is unavailable."),
            "attempt_count": row.attempts,
            "resend_count": row.resend_count,
            "sent_at": _iso(row.sent_at),
            "failure_category": row.failure_category,
            "provider": row.provider,
            "can_resend": row.status not in {"pending", "sending"},
        }

    def queue_live_test(self, recipient):
        if not current_app.config.get("EMAIL_LIVE_TEST_ENABLED"):
            raise InvoiceEmailError("Live invoice email testing is disabled.")
        if not self._configured():
            raise InvoiceEmailError(self._configuration_message())
        recipient = self._recipient(recipient)
        pdf = _demo_pdf()
        invoice_number = f"DEMO-INV-LIVE-{utc_now():%Y%m%d%H%M%S}"
        plain, html_body = self._render(
            first_name="John",
            invoice_number=invoice_number,
            course="2-Day Basic Certificate in Dental Assisting",
            intake="10–11 October 2026 — Development demonstration",
            development=True,
        )
        logo = self._logo()
        row = self.delivery.queue(
            "email",
            recipient,
            f"Q&M Training – Invoice {invoice_number}",
            plain,
            html_body=html_body,
            attachments=[
                {
                    "filename": f"{invoice_number}.pdf",
                    "data": pdf,
                    "content_type": "application/pdf",
                },
                {
                    "filename": "qm-training-logo.png",
                    "data": logo,
                    "content_type": "image/png",
                    "disposition": "inline",
                    "cid": "qm-training-logo",
                },
            ],
            purpose="invoice_live_test",
            email_type="invoice_live_test",
            provider="smtp",
            invoice_number=invoice_number,
            idempotency_key=f"invoice-live-test:{uuid4()}",
            document_sha256=hashlib.sha256(pdf).hexdigest(),
            status="pending",
            queued_at=utc_now(),
        )
        db.session.commit()
        # Process this one row with the same transport but without participant data.
        row.status = "sending"
        row.attempts = 1
        row.last_attempt_at = utc_now()
        db.session.commit()
        try:
            self.delivery._email(row)
            row.status = "sent"
            row.sent_at = utc_now()
            row.error = None
            row.failure_category = None
        except smtplib.SMTPAuthenticationError as error:
            row.status = "failed"
            row.failure_category = "authentication"
            row.error = "Email authentication failed. Please check the configured sender credentials."
            db.session.commit()
            raise InvoiceEmailError(row.error) from error
        except Exception as error:
            row.status = "failed"
            row.failure_category = "transport"
            row.error = "Email delivery failed. Review the SMTP configuration and retry."
            db.session.commit()
            raise InvoiceEmailError(row.error) from error
        db.session.commit()
        return row

    def _queue_manual(self, enrollment, generation, actor, action):
        count = db.session.execute(
            db.select(db.func.count())
            .select_from(OutboundMessage)
            .where(
                OutboundMessage.enrollment_id == enrollment.id,
                OutboundMessage.email_type == INVOICE_EMAIL_TYPE,
            )
        ).scalar_one()
        return self._queue(
            enrollment,
            generation,
            actor=actor,
            action=action,
            idempotency_key=(
                f"invoice:{generation.id}:v{generation.version}:manual:{uuid4()}"
            ),
            resend_count=count,
        )

    def _queue(
        self,
        enrollment,
        generation,
        *,
        actor,
        action,
        idempotency_key,
        resend_count,
    ):
        recipient = self._recipient(enrollment.email)
        pdf = self.invoice_templates.document_bytes(generation, actor="invoice-email")
        if len(pdf) > int(current_app.config.get("EMAIL_ATTACHMENT_MAX_BYTES", 10 * 1024 * 1024)):
            raise InvoiceEmailError("The invoice PDF is too large to email.")
        if not pdf.startswith(b"%PDF-"):
            raise InvoiceEmailError("The generated invoice PDF is invalid.")
        digest = hashlib.sha256(pdf).hexdigest()
        if generation.sha256 and digest != generation.sha256:
            raise InvoiceEmailError("The generated invoice PDF failed its integrity check.")
        intake = _intake_display(
            enrollment.intake_start_date or enrollment.preferred_intake_date,
            enrollment.intake_end_date,
        )
        plain, html_body = self._render(
            first_name=(enrollment.full_name or "Participant").split()[0],
            invoice_number=generation.invoice_number,
            course=(
                COURSE_EMAIL_NAME
                if "dental assisting" in str(enrollment.course or "").casefold()
                else enrollment.course or COURSE_EMAIL_NAME
            ),
            intake=intake,
            development=False,
            course_fee=_course_fee_text(enrollment.course_fee),
        )
        logo = self._logo()
        status = "pending" if self._configured() else "disabled"
        row = self.delivery.queue(
            "email",
            recipient,
            f"Q&M Training – Invoice {generation.invoice_number}",
            plain,
            html_body=html_body,
            attachments=[
                {
                    "filename": generation.filename,
                    "data": pdf,
                    "content_type": "application/pdf",
                },
                {
                    "filename": "qm-training-logo.png",
                    "data": logo,
                    "content_type": "image/png",
                    "disposition": "inline",
                    "cid": "qm-training-logo",
                },
            ],
            conversation_id=enrollment.conversation_id,
            purpose=f"invoice_email:{generation.id}:v{generation.version}",
            enrollment_id=enrollment.id,
            invoice_generation_id=generation.id,
            invoice_number=generation.invoice_number,
            email_type=INVOICE_EMAIL_TYPE,
            provider=current_app.config.get("EMAIL_PROVIDER", "smtp"),
            idempotency_key=idempotency_key,
            document_sha256=digest,
            resend_count=resend_count,
            status=status,
            error=None if status == "pending" else self._configuration_message(),
            failure_category=None if status == "pending" else "configuration",
            queued_at=utc_now(),
        )
        self._audit(enrollment, action, actor, delivery_id=row.id)
        return row

    def _eligible(self, enrollment, generation):
        if enrollment is None or generation is None:
            raise InvoiceEmailError("Invoice must be generated first.")
        if (
            not enrollment.confirmed_at
            or enrollment.status not in ELIGIBLE_ENROLLMENT_STATUSES
            or enrollment.status == "cancelled"
        ):
            raise InvoiceEmailError("This enrollment is not eligible for invoice email delivery.")
        if generation.status != "generated" or not generation.storage_key or not generation.sha256:
            raise InvoiceEmailError("Invoice must be generated first.")
        return enrollment, generation

    def _configured(self):
        config = current_app.config
        return bool(
            config.get("EMAIL_DELIVERY_ENABLED")
            and config.get("EMAIL_PROVIDER") == "smtp"
            and config.get("EMAIL_FROM_ADDRESS")
            and config.get("SMTP_HOST")
            and config.get("SMTP_USERNAME")
            and config.get("SMTP_PASSWORD")
        )

    @staticmethod
    def _configuration_message():
        return "Email delivery is not configured. Add the Google app password to SMTP_PASSWORD."

    @staticmethod
    def _recipient(value):
        value = str(value or "").strip()
        if any(character in value for character in "\r\n,;"):
            raise InvoiceEmailError("The enrollment email address is invalid.")
        try:
            return validate_email(value, check_deliverability=False).normalized.casefold()
        except EmailNotValidError as error:
            raise InvoiceEmailError("The enrollment email address is invalid.") from error

    def _logo(self):
        approved_root = (
            Path(__file__).resolve().parents[1] / "assets" / "email"
        ).resolve()
        path = Path(current_app.config["EMAIL_LOGO_PATH"]).resolve()
        if path.parent != approved_root or path.name != "qm-training-logo.png":
            raise InvoiceEmailError("The approved Q&M Training email logo is unavailable.")
        content = path.read_bytes()
        if not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise InvoiceEmailError("The approved Q&M Training email logo is invalid.")
        return content

    @staticmethod
    def _render(
        first_name, invoice_number, course, intake, development, course_fee="S$600 nett"
    ):
        warning = (
            "\nDevelopment demonstration only — this is not an official Q&M invoice.\n"
            if development
            else ""
        )
        plain = (
            f"Hi {first_name},\n\n"
            f"Thank you for enrolling in {course}.\n\n"
            "Please find your invoice attached to this email.\n\n"
            "Invoice details\n\n"
            f"Invoice number: {invoice_number}\n"
            f"Course: {course}\n"
            f"Course intake: {intake}\n"
            f"Total amount due: {course_fee}\n"
            f"{warning}\n"
            "You may also sign in to your Q&M Training account to download your invoice.\n\n"
            "If you have any questions or require assistance, please reply to this email.\n\n"
            "Kind regards,\nQ&M Training"
        )
        safe = {
            "first_name": html.escape(str(first_name)),
            "invoice_number": html.escape(str(invoice_number)),
            "course": html.escape(str(course)),
            "intake": html.escape(str(intake)),
            "course_fee": html.escape(str(course_fee)),
        }
        warning_html = (
            '<p style="padding:12px;background:#fff4d6;color:#6b4b00;">'
            "Development demonstration only — this is not an official Q&amp;M invoice.</p>"
            if development
            else ""
        )
        html_body = f"""<!doctype html>
<html><body style="margin:0;background:#f4f7f5;font-family:Arial,sans-serif;color:#173f3a;">
<div style="max-width:640px;margin:0 auto;background:#ffffff;padding:32px;">
<img src="cid:qm-training-logo" alt="Q&amp;M Training" style="width:180px;max-width:100%;height:auto;">
<p>Hi {safe['first_name']},</p>
<p>Thank you for enrolling in {safe['course']}.</p>
<p>Please find your invoice attached to this email.</p>
<h2 style="font-size:18px;">Invoice details</h2>
<table role="presentation" style="border-collapse:collapse;width:100%;">
<tr><td style="padding:6px 0;"><strong>Invoice number</strong></td><td>{safe['invoice_number']}</td></tr>
<tr><td style="padding:6px 0;"><strong>Course</strong></td><td>{safe['course']}</td></tr>
<tr><td style="padding:6px 0;"><strong>Course intake</strong></td><td>{safe['intake']}</td></tr>
<tr><td style="padding:6px 0;"><strong>Total amount due</strong></td><td>{safe['course_fee']}</td></tr>
</table>
{warning_html}
<p>You may also sign in to your Q&amp;M Training account to download your invoice.</p>
<p>If you have any questions or require assistance, please reply to this email.</p>
<p>Kind regards,<br>Q&amp;M Training</p>
</div></body></html>"""
        return plain, html_body

    @staticmethod
    def _automatic_key(generation):
        return f"invoice:{generation.id}:v{generation.version}:automatic"

    @staticmethod
    def _audit(enrollment, action, actor, **details):
        if enrollment is None:
            return
        db.session.add(
            FinanceAudit(
                enrollment_id=enrollment.id,
                action=action,
                actor=actor,
                details_json=json.dumps(details),
            )
        )

    def _fail(self, row, enrollment, status, category):
        row.status = status
        row.failure_category = category
        row.error = {
            "configuration": self._configuration_message(),
            "authentication": "Email authentication failed. Please check the configured sender credentials.",
            "recipient": "The registered recipient email address was rejected.",
        }[category]
        self._audit(
            enrollment,
            "invoice_email_failed",
            "system",
            delivery_id=row.id,
            category=category,
        )

    def _transient(self, row, enrollment, category):
        maximum = int(current_app.config.get("INVOICE_EMAIL_MAX_RETRIES", 3))
        row.failure_category = category
        row.error = "Email delivery failed. Staff may safely retry the same invoice."
        row.status = "pending" if row.attempts < maximum else "failed"
        self._audit(
            enrollment,
            "invoice_email_failed",
            "system",
            delivery_id=row.id,
            category=category,
            retry_pending=row.status == "pending",
        )

    def _recover_stuck(self):
        cutoff = utc_now() - timedelta(
            seconds=max(60, int(current_app.config.get("SMTP_TIMEOUT_SECONDS", 30)) * 2)
        )
        db.session.execute(
            db.update(OutboundMessage)
            .where(
                OutboundMessage.email_type == INVOICE_EMAIL_TYPE,
                OutboundMessage.status == "sending",
                OutboundMessage.last_attempt_at < cutoff,
            )
            .values(
                status="failed",
                failure_category="interrupted",
                error="Delivery was interrupted. Staff may safely retry after review.",
            )
        )
        db.session.commit()


def _mask_email(value):
    local, separator, domain = str(value or "").partition("@")
    if not separator:
        return "Unavailable"
    return f"{local[:1]}***@{domain}"


def _course_fee_text(value):
    amount = Decimal(str(value or 0)).quantize(Decimal("0.01"))
    rendered = f"{amount:.0f}" if amount == amount.to_integral() else f"{amount:.2f}"
    return f"S${rendered} nett"


def _intake_display(start, end=None):
    if start is None:
        return "Awaiting staff confirmation"
    end = end or start
    if start == end:
        return f"{start.day} {start.strftime('%B %Y')}"
    if start.year == end.year and start.month == end.month:
        return f"{start.day}–{end.day} {start.strftime('%B %Y')}"
    return f"{start.day} {start.strftime('%B %Y')}–{end.day} {end.strftime('%B %Y')}"


def _aware(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value):
    return value.isoformat() if value else None


def _demo_pdf():
    output = BytesIO()
    document = canvas.Canvas(output)
    document.drawString(72, 780, "Q&M Training — Development Invoice")
    document.drawString(72, 752, "John Tan — DEMO-INV development demonstration only")
    document.save()
    return output.getvalue()


__all__ = [
    "InvoiceEmailError",
    "InvoiceEmailService",
    "ResendCooldownError",
]
