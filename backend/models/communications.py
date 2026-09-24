"""Durable inbox and outbound delivery state."""
from backend.extensions import db
from backend.models import new_id, utc_now
from backend.services.security import EncryptedText


class OutboundMessage(db.Model):
    __tablename__ = "outbound_messages"
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    conversation_id = db.Column(db.String(36), db.ForeignKey("conversations.id"), index=True)
    enrollment_id = db.Column(db.String(36), db.ForeignKey("enrollment_drafts.id"), index=True)
    invoice_generation_id = db.Column(db.String(36), db.ForeignKey("invoice_generations.id"), index=True)
    channel = db.Column(db.String(16), nullable=False)
    recipient = db.Column(EncryptedText(), nullable=False)
    subject = db.Column(EncryptedText())
    body = db.Column(EncryptedText(), nullable=False)
    html_body = db.Column(EncryptedText())
    attachments = db.Column(EncryptedText(), default="[]")
    purpose = db.Column(db.String(128), index=True)
    email_type = db.Column(db.String(64), index=True)
    provider = db.Column(db.String(32))
    invoice_number = db.Column(db.String(64), index=True)
    idempotency_key = db.Column(db.String(255), unique=True, index=True)
    document_sha256 = db.Column(db.String(64))
    status = db.Column(db.String(32), default="queued", nullable=False)
    attempts = db.Column(db.Integer, default=0, nullable=False)
    resend_count = db.Column(db.Integer, default=0, nullable=False)
    provider_id = db.Column(db.String(255), index=True)
    error = db.Column(db.String(255))
    failure_category = db.Column(db.String(64))
    queued_at = db.Column(db.DateTime(timezone=True), default=utc_now, nullable=False)
    last_attempt_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)
    sent_at = db.Column(db.DateTime(timezone=True))


class InboxEvent(db.Model):
    __tablename__ = "inbox_events"
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    external_id = db.Column(db.String(255), unique=True, nullable=False)
    channel = db.Column(db.String(16), nullable=False)
    payload = db.Column(EncryptedText(), nullable=False)
    status = db.Column(db.String(32), default="pending", nullable=False)
    attempts = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utc_now)
