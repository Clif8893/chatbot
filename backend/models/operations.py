"""Durable lead nurturing, consent, staff review and course administration state."""

from datetime import time

from backend.extensions import db
from backend.models import new_id, utc_now
from backend.services.security import EncryptedText


class ContactMapping(db.Model):
    __tablename__ = "contact_mappings"
    __table_args__ = (db.UniqueConstraint("channel", "identity_digest", name="uq_contact_identity"),)
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    channel = db.Column(db.String(24), nullable=False)
    external_id = db.Column(EncryptedText(), nullable=False)
    identity_digest = db.Column(db.String(64), nullable=False)
    conversation_id = db.Column(db.String(36), db.ForeignKey("conversations.id"), nullable=False, index=True)


class Lead(db.Model):
    __tablename__ = "leads"
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    conversation_id = db.Column(db.String(36), db.ForeignKey("conversations.id"), nullable=False, unique=True)
    status = db.Column(db.String(32), nullable=False, default="enquiry", index=True)
    channel = db.Column(db.String(24), nullable=True)
    recipient = db.Column(EncryptedText(), nullable=True)
    last_inbound_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_contact_at = db.Column(db.DateTime(timezone=True), nullable=True)
    followup_count = db.Column(db.Integer, nullable=False, default=0)
    sequence = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)


class ConsentRecord(db.Model):
    __tablename__ = "consent_records"
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    conversation_id = db.Column(db.String(36), db.ForeignKey("conversations.id"), nullable=False, index=True)
    granted = db.Column(db.Boolean, nullable=False)
    version = db.Column(db.String(64), nullable=False)
    source = db.Column(db.String(64), nullable=False)
    recorded_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)


class FollowupRun(db.Model):
    __tablename__ = "followup_runs"
    __table_args__ = (db.UniqueConstraint("lead_id", "sequence", "step", name="uq_followup_step"),)
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    lead_id = db.Column(db.String(36), db.ForeignKey("leads.id"), nullable=False, index=True)
    sequence = db.Column(db.Integer, nullable=False)
    step = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(24), nullable=False, default="queued")
    delivery_id = db.Column(db.String(36), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)


class StaffEscalation(db.Model):
    __tablename__ = "staff_escalations"
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    conversation_id = db.Column(db.String(36), db.ForeignKey("conversations.id"), nullable=False, index=True)
    reason = db.Column(EncryptedText(), nullable=False)
    category = db.Column(db.String(64), nullable=False)
    status = db.Column(db.String(24), nullable=False, default="open", index=True)
    note = db.Column(EncryptedText(), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    resolved_at = db.Column(db.DateTime(timezone=True), nullable=True)


class AuditEvent(db.Model):
    __tablename__ = "operations_audit"
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    conversation_id = db.Column(db.String(36), db.ForeignKey("conversations.id"), nullable=True, index=True)
    actor = db.Column(db.String(128), nullable=False)
    action = db.Column(db.String(64), nullable=False)
    detail = db.Column(EncryptedText(), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)


class CourseDate(db.Model):
    __tablename__ = "course_dates"
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    course_id = db.Column(
        db.String(64), nullable=False, default="dental-assisting-basic", index=True
    )
    date = db.Column(db.Date, nullable=False, unique=True)
    end_date = db.Column(db.Date, nullable=False)
    start_time = db.Column(db.Time, nullable=False, default=lambda: time(9, 30))
    end_time = db.Column(db.Time, nullable=False, default=lambda: time(17, 30))
    label = db.Column(db.String(255), nullable=False, default="")
    capacity = db.Column(db.Integer, nullable=False)
    participant_count = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(db.String(24), nullable=False, default="open", index=True)
    is_demo = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)


class CatalogueContent(db.Model):
    __tablename__ = "catalogue_content"
    key = db.Column(db.String(32), primary_key=True)
    value = db.Column(db.JSON, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)
