"""Outbox transport. Tests replace HTTP and SMTP; queuing never sends."""
import json
import smtplib
import socket
import ssl
from email.message import EmailMessage
from email.utils import formataddr
from datetime import timedelta

import requests
from flask import current_app

from backend.extensions import db
from backend.models import utc_now
from backend.models.communications import OutboundMessage
from backend.services.security import EncryptedStorage


class EmailConfigurationError(RuntimeError):
    """Raised without exposing credentials when SMTP configuration is incomplete."""


class EmailRecipientError(ValueError):
    """Raised for unsafe or malformed recipient/header input."""


SMTP_SAFE_MESSAGES = {
    "email_not_configured": "Email delivery is not configured.",
    "smtp_authentication_failed": "Gmail rejected the configured credentials.",
    "gmail_app_password_or_account_action_required": "Gmail requires an app password or account action.",
    "smtp_tls_or_authentication_required": "The SMTP server requires TLS or authentication.",
    "smtp_dns_failed": "The SMTP host could not be resolved.",
    "smtp_connection_failed": "The SMTP server connection failed.",
    "smtp_timeout": "The SMTP operation timed out.",
    "smtp_tls_failed": "The secure SMTP connection failed.",
    "smtp_recipient_rejected": "The configured recipient was rejected.",
    "smtp_sender_rejected": "The configured sender was rejected.",
    "smtp_rate_limited": "Gmail temporarily limited the SMTP request.",
    "smtp_provider_rejected": "The SMTP provider rejected the request.",
}


class SMTPDeliveryError(RuntimeError):
    """Sanitised SMTP failure suitable for persistence and CLI output."""

    def __init__(self, category, response_code=None):
        self.category = category
        self.response_code = int(response_code) if response_code is not None else None
        self.safe_message = SMTP_SAFE_MESSAGES[category]
        super().__init__(self.safe_message)


def normalize_smtp_password(value):
    """Remove Gmail's display grouping spaces without disclosing the value."""
    password = str(value or "")
    stripped = password.strip()
    if any(character in password for character in "\r\n"):
        raise EmailConfigurationError("SMTP password contains an invalid newline.")
    if stripped.startswith(("'", '"')) or stripped.endswith(("'", '"')):
        raise EmailConfigurationError("SMTP password contains invalid surrounding quotes.")
    return password.replace(" ", "")


def safe_smtp_error(error, stage=None):
    """Map provider/network errors without retaining raw responses."""
    if isinstance(error, SMTPDeliveryError):
        return error
    if isinstance(error, EmailConfigurationError):
        return SMTPDeliveryError("email_not_configured")
    if isinstance(error, EmailRecipientError):
        return SMTPDeliveryError("smtp_recipient_rejected")
    code = getattr(error, "smtp_code", None)
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        responses = list((error.recipients or {}).values())
        code = responses[0][0] if responses else code
        return SMTPDeliveryError("smtp_recipient_rejected", code)
    if isinstance(error, smtplib.SMTPSenderRefused):
        return SMTPDeliveryError("smtp_sender_rejected", code)
    if isinstance(error, smtplib.SMTPAuthenticationError):
        category = {
            535: "smtp_authentication_failed",
            534: "gmail_app_password_or_account_action_required",
            530: "smtp_tls_or_authentication_required",
        }.get(code, "smtp_authentication_failed")
        return SMTPDeliveryError(category, code)
    if isinstance(error, (socket.timeout, TimeoutError)):
        return SMTPDeliveryError("smtp_timeout", code)
    if isinstance(error, socket.gaierror):
        return SMTPDeliveryError("smtp_dns_failed", code)
    if isinstance(error, (ssl.SSLError, smtplib.SMTPNotSupportedError)) or stage in {
        "starttls",
        "tls_ehlo",
    }:
        return SMTPDeliveryError("smtp_tls_failed", code)
    if isinstance(error, (ConnectionRefusedError, smtplib.SMTPConnectError)):
        return SMTPDeliveryError("smtp_connection_failed", code)
    if code == 530:
        return SMTPDeliveryError("smtp_tls_or_authentication_required", code)
    if code in {421, 450, 451, 452, 454}:
        return SMTPDeliveryError("smtp_rate_limited", code)
    if isinstance(error, OSError) or stage == "connect":
        return SMTPDeliveryError("smtp_connection_failed", code)
    return SMTPDeliveryError("smtp_provider_rejected", code)


class DeliveryService:
    def __init__(self, storage=None, http=None, smtp_factory=None):
        self.storage = storage or EncryptedStorage()
        self.http = http or requests
        self.smtp_factory = smtp_factory or smtplib.SMTP

    def queue(
        self,
        channel,
        recipient,
        subject,
        body,
        attachments=None,
        conversation_id=None,
        purpose=None,
        *,
        html_body=None,
        **metadata,
    ):
        if channel not in {"email", "whatsapp"} or not recipient:
            raise ValueError("A supported delivery channel and recipient are required.")
        stored = []
        for attachment in attachments or []:
            if isinstance(attachment, dict):
                filename = attachment.get("filename", "attachment.pdf")
                data = attachment.get("data") or attachment.get("content")
                key = attachment.get("storage_key") or attachment.get("key")
                mime = attachment.get("content_type") or attachment.get("mime_type", "application/pdf")
                disposition = attachment.get("disposition", "attachment")
                cid = attachment.get("cid")
            else:
                filename, data = attachment[:2]
                mime, key, disposition, cid = "application/pdf", None, "attachment", None
            if data is not None:
                key = self.storage.save(data)
            if not key:
                raise ValueError("Attachment requires content or a storage key.")
            stored.append(
                {
                    "filename": filename,
                    "key": key,
                    "content_type": mime,
                    "disposition": disposition,
                    "cid": cid,
                }
            )
        outbound = OutboundMessage(channel=channel, recipient=str(recipient), subject=subject,
                                   body=body, html_body=html_body,
                                   attachments=json.dumps(stored),
                                   conversation_id=conversation_id, purpose=purpose,
                                   **metadata)
        db.session.add(outbound)
        db.session.flush()
        return outbound

    def dispatch(self, limit=100):
        rows = db.session.execute(db.select(OutboundMessage).where(OutboundMessage.status == "queued")
                                  .order_by(OutboundMessage.created_at).limit(limit)).scalars().all()
        for row in rows:
            # Claim atomically: overlapping schedulers must not dispatch a row twice.
            claimed = db.session.execute(db.update(OutboundMessage).where(OutboundMessage.id == row.id, OutboundMessage.status == 'queued')
                         .values(status='sending', attempts=OutboundMessage.attempts + 1))
            db.session.commit()
            if claimed.rowcount != 1:
                continue
            db.session.refresh(row)
            try:
                if row.channel == "email":
                    self._email(row)
                else:
                    self._whatsapp(row)
                row.status = "sent"
                row.sent_at = utc_now()
                row.error = None
            except Exception:
                # Sending may have succeeded before a network timeout. Staff must
                # explicitly retry; automatic retry could duplicate financial mail.
                row.status = "failed"
                row.error = "Delivery failed or acknowledgement was not received. Review before retry."
            db.session.commit()
        return len(rows)

    def _email(self, row):
        config = current_app.config
        from_address = config.get("EMAIL_FROM_ADDRESS") or config.get("SMTP_FROM")
        password = normalize_smtp_password(config.get("SMTP_PASSWORD"))
        if (
            not config.get("SMTP_HOST")
            or not from_address
            or not config.get("SMTP_USERNAME")
            or not password
        ):
            raise EmailConfigurationError(
                "Email delivery is not configured."
            )
        for value in (from_address, row.recipient, row.subject or ""):
            if any(character in str(value) for character in "\r\n"):
                raise EmailRecipientError("Email headers contain unsafe characters.")
        if any(character in str(row.recipient) for character in ",;"):
            raise EmailRecipientError("Exactly one recipient is required.")
        message = EmailMessage()
        message["From"] = formataddr(
            (config.get("EMAIL_FROM_NAME", "Q&M Training"), from_address)
        )
        message["To"] = row.recipient
        if config.get("EMAIL_REPLY_TO"):
            if any(character in config["EMAIL_REPLY_TO"] for character in "\r\n"):
                raise EmailRecipientError("Reply-To contains unsafe characters.")
            message["Reply-To"] = config["EMAIL_REPLY_TO"]
        message["Subject"] = row.subject or "Q&M course administration"
        message["Message-ID"] = f"<{row.id}@qm-course.local>"
        message.set_content(row.body)
        files = json.loads(row.attachments or "[]")
        if row.html_body:
            message.add_alternative(row.html_body, subtype="html")
            html_part = message.get_payload()[-1]
            for file in files:
                if file.get("disposition") != "inline":
                    continue
                main, sub = file["content_type"].split("/", 1)
                html_part.add_related(
                    self.storage.read(file["key"]),
                    maintype=main,
                    subtype=sub,
                    cid=f"<{file['cid']}>",
                    filename=file["filename"],
                    disposition="inline",
                )
        for file in files:
            if file.get("disposition") == "inline":
                continue
            main, sub = file["content_type"].split("/", 1)
            message.add_attachment(self.storage.read(file["key"]), maintype=main, subtype=sub, filename=file["filename"])
        smtp = None
        stage = "connect"
        try:
            smtp = self.smtp_factory(
                config["SMTP_HOST"],
                config["SMTP_PORT"],
                timeout=config.get("SMTP_TIMEOUT_SECONDS", 30),
            )
            stage = "ehlo"
            self._ehlo(smtp)
            if config.get("SMTP_USE_TLS", config.get("SMTP_STARTTLS", True)):
                stage = "starttls"
                smtp.starttls(context=ssl.create_default_context())
                stage = "tls_ehlo"
                self._ehlo(smtp)
            if config.get("SMTP_USERNAME"):
                stage = "authentication"
                smtp.login(config["SMTP_USERNAME"], password)
            stage = "send"
            try:
                rejected = smtp.send_message(
                    message,
                    from_addr=from_address,
                    to_addrs=[row.recipient],
                )
            except TypeError:
                # Small test doubles and older SMTP adapters may only accept the
                # message object; the validated headers still contain one recipient.
                rejected = smtp.send_message(message)
            if rejected:
                raise smtplib.SMTPRecipientsRefused(rejected)
        except Exception as error:
            raise safe_smtp_error(error, stage) from error
        finally:
            self._close_smtp(smtp)
        row.provider_id = message["Message-ID"].strip("<>")

    def diagnose_email(self):
        """Check SMTP through authentication without creating or sending mail."""
        config = current_app.config
        report = {
            "dns_resolution": False,
            "tcp_connection": False,
            "ehlo": False,
            "starttls": False,
            "tls_ehlo": False,
            "authentication": False,
            "failure_category": None,
            "smtp_response_code": None,
        }
        smtp = None
        stage = "configuration"
        try:
            host = config.get("SMTP_HOST")
            username = config.get("SMTP_USERNAME")
            from_address = config.get("EMAIL_FROM_ADDRESS") or config.get("SMTP_FROM")
            password = normalize_smtp_password(config.get("SMTP_PASSWORD"))
            if not host or not username or not from_address or not password:
                raise EmailConfigurationError("Email delivery is not configured.")
            stage = "dns"
            socket.getaddrinfo(host, config["SMTP_PORT"], type=socket.SOCK_STREAM)
            report["dns_resolution"] = True
            stage = "connect"
            smtp = self.smtp_factory(
                host,
                config["SMTP_PORT"],
                timeout=config.get("SMTP_TIMEOUT_SECONDS", 30),
            )
            report["tcp_connection"] = True
            stage = "ehlo"
            self._ehlo(smtp)
            report["ehlo"] = True
            stage = "starttls"
            smtp.starttls(context=ssl.create_default_context())
            report["starttls"] = True
            stage = "tls_ehlo"
            self._ehlo(smtp)
            report["tls_ehlo"] = True
            stage = "authentication"
            smtp.login(username, password)
            report["authentication"] = True
        except Exception as error:
            safe = safe_smtp_error(error, stage)
            report["failure_category"] = safe.category
            report["smtp_response_code"] = safe.response_code
        finally:
            self._close_smtp(smtp)
        return report

    @staticmethod
    def _ehlo(smtp):
        if not hasattr(smtp, "ehlo"):
            return
        result = smtp.ehlo()
        if isinstance(result, tuple) and result and not 200 <= int(result[0]) < 300:
            raise smtplib.SMTPHeloError(result[0], b"")

    @staticmethod
    def _close_smtp(smtp):
        if smtp is None:
            return
        try:
            if hasattr(smtp, "quit"):
                smtp.quit()
            elif hasattr(smtp, "__exit__"):
                smtp.__exit__(None, None, None)
        except Exception:
            try:
                if hasattr(smtp, "close"):
                    smtp.close()
            except Exception:
                pass

    def _whatsapp(self, row):
        config = current_app.config
        token, phone = config.get("WHATSAPP_ACCESS_TOKEN"), config.get("WHATSAPP_PHONE_NUMBER_ID")
        if not token or not phone:
            raise RuntimeError("WhatsApp transport is not configured.")
        # Proactive messages outside Meta's customer-service window require an
        # approved template; free text is used only for recent inbound replies.
        from backend.models.communications import InboxEvent
        recent = db.session.execute(db.select(InboxEvent).where(InboxEvent.channel == "whatsapp",
                    InboxEvent.created_at >= utc_now() - timedelta(hours=24))).scalars().all()
        in_window = any(json.loads(event.payload).get("from") == row.recipient for event in recent)
        if not in_window:
            template = config.get("WHATSAPP_FOLLOWUP_TEMPLATE")
            if not template:
                raise RuntimeError("Approved WhatsApp template required outside service window.")
            payload = {"messaging_product": "whatsapp", "to": row.recipient, "type": "template",
                       "template": {"name": template, "language": {"code": config["WHATSAPP_TEMPLATE_LANGUAGE"]},
                                    "components": [{"type": "body", "parameters": [{"type": "text", "text": row.body}]}]}}
        else:
            payload = {"messaging_product": "whatsapp", "to": row.recipient,
                       "type": "text", "text": {"body": row.body}}
        response = self.http.post(f"https://graph.facebook.com/{config['WHATSAPP_API_VERSION']}/{phone}/messages",
                                 headers={"Authorization": f"Bearer {token}"}, json=payload, timeout=20)
        response.raise_for_status()
        row.provider_id = response.json()["messages"][0]["id"]

    def provider_status(self, provider_id, status):
        row = db.session.execute(db.select(OutboundMessage).where(OutboundMessage.provider_id == provider_id)).scalar_one_or_none()
        if row and status in {"sent", "delivered", "read", "failed"}:
            rank = {"sent": 1, "delivered": 2, "read": 3, "failed": 4}
            if rank.get(status, 0) >= rank.get(row.status, 0):
                row.status = status
            db.session.commit()


__all__ = [
    "DeliveryService",
    "EmailConfigurationError",
    "EmailRecipientError",
    "SMTPDeliveryError",
    "normalize_smtp_password",
    "safe_smtp_error",
]
