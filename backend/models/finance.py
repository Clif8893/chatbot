"""Auditable financial snapshots; image and PDF bytes live in encrypted storage."""

from backend.extensions import db
from backend.models import new_id, utc_now
from backend.services.security import EncryptedText


class FinanceCase(db.Model):
    __tablename__ = "finance_cases"
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    enrollment_id = db.Column(db.String(36), db.ForeignKey("enrollment_drafts.id"), unique=True, nullable=False, index=True)
    status = db.Column(db.String(32), nullable=False, default="Enrolled", index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)
    enrollment = db.relationship("EnrollmentDraft")


class Invoice(db.Model):
    __tablename__ = "invoices"
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    enrollment_id = db.Column(db.String(36), db.ForeignKey("enrollment_drafts.id"), nullable=False, unique=True, index=True)
    number = db.Column(db.String(64), unique=True, nullable=False)
    course_name = db.Column(db.String(255), nullable=False)
    participant_name = db.Column(db.String(255), nullable=False)
    course_date = db.Column(db.Date)
    course_fee = db.Column(db.Numeric(10, 2), nullable=False)
    skillsfuture_amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    net_payable = db.Column(db.Numeric(10, 2), nullable=False)
    currency = db.Column(db.String(3), nullable=False, default="SGD")
    recipient_uen = db.Column(db.String(32), nullable=False)
    due_date = db.Column(db.Date, nullable=False)
    delivery_id = db.Column(db.String(36))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    sent_at = db.Column(db.DateTime(timezone=True))
    enrollment = db.relationship("EnrollmentDraft")


class FinancialDocument(db.Model):
    __tablename__ = "financial_documents"
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    enrollment_id = db.Column(db.String(36), db.ForeignKey("enrollment_drafts.id"), nullable=False, index=True)
    invoice_id = db.Column(db.String(36), db.ForeignKey("invoices.id"), nullable=False)
    kind = db.Column(db.String(24), nullable=False)
    number = db.Column(db.String(64), nullable=False)
    version = db.Column(db.Integer, nullable=False, default=1)
    storage_key = db.Column(db.String(255), nullable=False)
    sha256 = db.Column(db.String(64), nullable=False)
    signed = db.Column(db.Boolean, nullable=False, default=False)
    delivery_id = db.Column(db.String(36))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)

    @property
    def filename(self):
        return f"{self.number}-v{self.version}.pdf"


class InvoiceGeneration(db.Model):
    """Single active template-generated invoice document per enrollment."""

    __tablename__ = "invoice_generations"
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    enrollment_id = db.Column(
        db.String(36),
        db.ForeignKey("enrollment_drafts.id"),
        nullable=False,
        unique=True,
        index=True,
    )
    invoice_number = db.Column(
        db.String(64), nullable=False, unique=True, index=True
    )
    storage_key = db.Column(db.String(255))
    status = db.Column(
        db.String(24), nullable=False, default="pending", index=True
    )
    generated_at = db.Column(db.DateTime(timezone=True))
    last_attempted_at = db.Column(db.DateTime(timezone=True))
    attempt_count = db.Column(db.Integer, nullable=False, default=0)
    failure_reason = db.Column(db.String(255))
    sha256 = db.Column(db.String(64))
    version = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )
    enrollment = db.relationship("EnrollmentDraft")

    @property
    def filename(self):
        return f"{self.invoice_number}.pdf"


class PaymentEvidence(db.Model):
    __tablename__ = "payment_evidence"
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    enrollment_id = db.Column(db.String(36), db.ForeignKey("enrollment_drafts.id"), nullable=False, index=True)
    sha256 = db.Column(db.String(64), nullable=False, index=True)
    channel = db.Column(db.String(24), nullable=False)
    source_event_id = db.Column(db.String(255), unique=True)
    mime_type = db.Column(db.String(64), nullable=False)
    storage_key = db.Column(db.String(255), nullable=False)
    extracted_json = db.Column(EncryptedText(), nullable=False, default="{}")
    payment_type = db.Column(db.String(24), nullable=False, default="paynow")
    verdict = db.Column(db.String(32), nullable=False, default="review")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)


class Payment(db.Model):
    __tablename__ = "payments"
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    enrollment_id = db.Column(db.String(36), db.ForeignKey("enrollment_drafts.id"), nullable=False, index=True)
    invoice_id = db.Column(db.String(36), db.ForeignKey("invoices.id"), nullable=False)
    evidence_id = db.Column(db.String(36), db.ForeignKey("payment_evidence.id"))
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    method = db.Column(db.String(24), nullable=False, default="paynow")
    reference = db.Column(db.String(255), unique=True, nullable=False)
    transaction_date = db.Column(db.Date, nullable=False)
    confirmed_by = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    __table_args__ = (db.UniqueConstraint("invoice_id", "method", name="uq_invoice_payment_method"),)


class PaymentReview(db.Model):
    __tablename__ = "payment_reviews"
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    enrollment_id = db.Column(db.String(36), db.ForeignKey("enrollment_drafts.id"), nullable=False, index=True)
    evidence_id = db.Column(db.String(36), db.ForeignKey("payment_evidence.id"), nullable=False, unique=True)
    reasons_json = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(24), nullable=False, default="pending", index=True)
    note = db.Column(db.Text)
    decided_by = db.Column(db.String(255))
    decided_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)


class CreditNoteRequest(db.Model):
    __tablename__ = "credit_note_requests"
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    enrollment_id = db.Column(db.String(36), db.ForeignKey("enrollment_drafts.id"), nullable=False, index=True)
    invoice_id = db.Column(db.String(36), db.ForeignKey("invoices.id"), nullable=False)
    reason = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(24), nullable=False, default="pending", index=True)
    note = db.Column(db.Text)
    requested_by = db.Column(db.String(255), nullable=False)
    decided_by = db.Column(db.String(255))
    document_id = db.Column(db.String(36), db.ForeignKey("financial_documents.id"))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    decided_at = db.Column(db.DateTime(timezone=True))


class FinanceAudit(db.Model):
    __tablename__ = "finance_audit"
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    enrollment_id = db.Column(db.String(36), db.ForeignKey("enrollment_drafts.id"), nullable=False, index=True)
    action = db.Column(db.String(64), nullable=False)
    actor = db.Column(db.String(255), nullable=False)
    details_json = db.Column(db.Text, nullable=False, default="{}")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)


class MasterInvoiceExport(db.Model):
    """Idempotent export state for one confirmed enrollment."""

    __tablename__ = "master_invoice_exports"
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    enrollment_id = db.Column(
        db.String(36),
        db.ForeignKey("enrollment_drafts.id"),
        nullable=False,
        unique=True,
        index=True,
    )
    status = db.Column(db.String(24), nullable=False, default="pending", index=True)
    export_key = db.Column(db.String(64), unique=True, nullable=True, index=True)
    invoice_number = db.Column(db.String(64), unique=True, nullable=True, index=True)
    invoice_sequence = db.Column(db.Integer, unique=True, nullable=True)
    invoice_date = db.Column(db.Date, nullable=True)
    workbook_row = db.Column(db.Integer, nullable=True)
    export_version = db.Column(db.Integer, nullable=False, default=0)
    content_hash = db.Column(db.String(64), nullable=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    last_error = db.Column(db.String(255))
    last_attempted_at = db.Column(db.DateTime(timezone=True))
    exported_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )
    enrollment = db.relationship("EnrollmentDraft")


class MasterInvoiceAudit(db.Model):
    """Non-sensitive audit trail for workbook generation and access."""

    __tablename__ = "master_invoice_audit"
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    action = db.Column(db.String(32), nullable=False, index=True)
    actor = db.Column(db.String(255), nullable=False)
    record_count = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)


# Integration alias; both names refer to the same mapped table.
FinanceDocument = FinancialDocument
