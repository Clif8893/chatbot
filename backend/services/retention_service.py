"""Configured retention of cases, communications, and encrypted file objects."""
import json
from datetime import timedelta
from pathlib import Path
from flask import current_app
from backend.extensions import db
from backend.models import Conversation, EnrollmentDraft, Message, utc_now
from backend.models.communications import InboxEvent, OutboundMessage
from backend.models.finance import CreditNoteRequest, FinanceAudit, FinanceCase, FinancialDocument, Invoice, InvoiceGeneration, Payment, PaymentEvidence, PaymentReview
from backend.models.operations import AuditEvent, ConsentRecord, ContactMapping, FollowupRun, Lead, StaffEscalation


class RetentionService:
    def run(self, dry_run=True, now=None):
        days = current_app.config['RETENTION_DAYS']
        if type(days) is not int or days <= 0:
            raise ValueError('Retention days must be a positive integer.')
        cutoff = (now or utc_now()) - timedelta(days=days)
        candidates = db.session.execute(db.select(Conversation).where(Conversation.updated_at < cutoff)).scalars().all()
        ids = []
        for row in candidates:
            lead = db.session.execute(db.select(Lead).where(Lead.conversation_id == row.id, Lead.updated_at >= cutoff)).first()
            active_finance = db.session.execute(db.select(FinanceCase).join(EnrollmentDraft, EnrollmentDraft.id == FinanceCase.enrollment_id).where(EnrollmentDraft.conversation_id == row.id, FinanceCase.updated_at >= cutoff)).first()
            recent_message = db.session.execute(db.select(Message.id).where(Message.conversation_id == row.id, Message.created_at >= cutoff)).first()
            if not (lead or active_finance or recent_message):
                ids.append(row.id)
        inbox = db.session.execute(db.select(InboxEvent).where(InboxEvent.created_at < cutoff)).scalars().all()
        if dry_run:
            return {'dry_run': True, 'expired_cases': len(ids), 'expired_inbox_events': len(inbox), 'cutoff': cutoff.isoformat()}
        drafts = list(db.session.execute(db.select(EnrollmentDraft.id).where(EnrollmentDraft.conversation_id.in_(ids))).scalars())
        lead_ids = list(db.session.execute(db.select(Lead.id).where(Lead.conversation_id.in_(ids))).scalars())
        expired_keys = set(db.session.execute(db.select(FinancialDocument.storage_key).where(FinancialDocument.enrollment_id.in_(drafts))).scalars())
        expired_keys.update(db.session.execute(db.select(PaymentEvidence.storage_key).where(PaymentEvidence.enrollment_id.in_(drafts))).scalars())
        generated_invoice_keys = set(
            key
            for key in db.session.execute(
                db.select(InvoiceGeneration.storage_key).where(
                    InvoiceGeneration.enrollment_id.in_(drafts)
                )
            ).scalars()
            if key
        )
        expired_outbox = db.session.execute(db.select(OutboundMessage).where((OutboundMessage.conversation_id.in_(ids)) | (OutboundMessage.created_at < cutoff))).scalars()
        for row in expired_outbox:
            expired_keys.update(item['key'] for item in json.loads(row.attachments or '[]'))
        for row in inbox:
            key = json.loads(row.payload).get('_attachment_key')
            if key:
                expired_keys.add(key)
        # Explicit order works with PostgreSQL foreign keys and preserves shared course data.
        for model in (CreditNoteRequest, PaymentReview, Payment, PaymentEvidence, FinanceAudit, FinancialDocument, InvoiceGeneration, Invoice, FinanceCase):
            db.session.execute(db.delete(model).where(model.enrollment_id.in_(drafts)))
        db.session.execute(db.delete(FollowupRun).where(FollowupRun.lead_id.in_(lead_ids)))
        for model in (OutboundMessage, StaffEscalation, ConsentRecord, ContactMapping, AuditEvent, Lead, Message, EnrollmentDraft):
            db.session.execute(db.delete(model).where(model.conversation_id.in_(ids)))
        db.session.execute(db.delete(Conversation).where(Conversation.id.in_(ids)))
        for row in inbox:
            db.session.delete(row)
        # Old outbox messages have their own communication retention window.
        db.session.execute(db.delete(OutboundMessage).where(OutboundMessage.created_at < cutoff))
        db.session.commit()
        storage_root = Path(current_app.config['STORAGE_DIR']).resolve()
        for key in generated_invoice_keys:
            path = (storage_root / key).resolve()
            if storage_root in path.parents and path.is_file():
                path.unlink()
        referenced = set(db.session.execute(db.select(FinancialDocument.storage_key)).scalars())
        referenced.update(db.session.execute(db.select(PaymentEvidence.storage_key)).scalars())
        for raw in db.session.execute(db.select(OutboundMessage.attachments)).scalars():
            referenced.update(item['key'] for item in json.loads(raw or '[]'))
        for raw in db.session.execute(db.select(InboxEvent.payload)).scalars():
            key = json.loads(raw).get('_attachment_key')
            if key:
                referenced.add(key)
        removed = 0
        for path in Path(current_app.config['STORAGE_DIR']).glob('*'):
            if path.is_file() and path.name not in referenced and all(c in '0123456789abcdef-' for c in path.name) and (path.name in expired_keys or path.stat().st_mtime < cutoff.timestamp()):
                path.unlink()
                removed += 1
        return {'dry_run': False, 'expired_cases': len(ids), 'expired_inbox_events': len(inbox), 'removed_objects': removed}
