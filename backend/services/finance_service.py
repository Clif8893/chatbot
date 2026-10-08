"""Enrollment finance pipeline with deterministic review and delivery gates."""

import csv
import hashlib
import json
import re
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from io import BytesIO, StringIO
from pathlib import Path
from uuid import uuid4

from flask import current_app
from PIL import Image
from sqlalchemy.exc import IntegrityError

from backend.extensions import db
from backend.models import EnrollmentDraft, utc_now
from backend.models.finance import (CreditNoteRequest, FinanceAudit, FinanceCase, FinancialDocument, Invoice, MasterInvoiceExport, Payment, PaymentEvidence, PaymentReview)
from backend.models.operations import CourseDate
from backend.services.document_service import DocumentService
from backend.services.payment_vision import PaymentExtraction, PaymentVision
from backend.services.privacy import mask_nric
from backend.services.security import EncryptedStorage


def money(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or result != result.quantize(Decimal("0.01")):
            raise ValueError("Amount must be a non-negative decimal with at most two fractional digits")
        return result.quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError):
        raise ValueError("Amount is not valid") from None


def normalize_reference(value):
    return re.sub(r"\s+", "", str(value or "")).upper()


def iso(value):
    return value.isoformat() if value else None


def _intake_display(start, end=None):
    if start is None:
        return None
    end = end or start
    if start == end:
        return f"{start.day} {start.strftime('%B %Y')}"
    if start.year == end.year and start.month == end.month:
        return f"{start.day}–{end.day} {start.strftime('%B %Y')}"
    if start.year == end.year:
        return f"{start.day} {start.strftime('%B')}–{end.day} {end.strftime('%B %Y')}"
    return (
        f"{start.day} {start.strftime('%B %Y')}–"
        f"{end.day} {end.strftime('%B %Y')}"
    )


class FinanceService:
    def __init__(self, catalogue, delivery_service, storage_service=None, vision=None, document_service=None):
        self.catalogue = catalogue
        self.delivery = delivery_service
        self.storage = storage_service or EncryptedStorage()
        self.vision = vision
        self.documents = document_service or DocumentService()

    def get_case(self, draft):
        case = db.session.execute(db.select(FinanceCase).where(FinanceCase.enrollment_id == draft.id)).scalar_one_or_none()
        if case is None:
            case = FinanceCase(enrollment_id=draft.id)
            db.session.add(case)
            db.session.flush()
        return case

    def _audit(self, draft, action, actor="system", **details):
        db.session.add(FinanceAudit(enrollment_id=draft.id, action=action, actor=actor, details_json=json.dumps(details)))

    def _invoice(self, draft):
        return db.session.execute(db.select(Invoice).where(Invoice.enrollment_id == draft.id)).scalar_one_or_none()

    def get_document(self, document_id):
        document = db.session.get(FinancialDocument, document_id)
        if document is None:
            raise LookupError("Document not found")
        return document

    def document_bytes(self, document):
        return self.storage.read(document.storage_key)

    def _latest_document(self, draft, kind):
        return db.session.execute(db.select(FinancialDocument).where(FinancialDocument.enrollment_id == draft.id, FinancialDocument.kind == kind).order_by(FinancialDocument.created_at.desc(), FinancialDocument.version.desc())).scalars().first()

    def _save_document(self, invoice, kind, number=None, payment=None, reason=None):
        previous = self._latest_document(invoice.enrollment, kind)
        content, signed = self.documents.render(invoice, kind, number, payment, reason)
        record = FinancialDocument(enrollment_id=invoice.enrollment_id, invoice_id=invoice.id, kind=kind, number=number or invoice.number,
                                   version=(previous.version + 1 if previous else 1), storage_key=self.storage.save(content),
                                   sha256=hashlib.sha256(content).hexdigest(), signed=signed)
        db.session.add(record)
        db.session.flush()
        self._audit(invoice.enrollment, f"{kind}_generated", document_id=record.id, version=record.version, signed=signed)
        return record

    def document_blockers(self, draft, document=None):
        result = []
        signature = current_app.config.get("FINANCE_SIGNATURE_PATH") or current_app.config.get("INVOICE_SIGNATURE_PATH")
        if not signature or not Path(signature).is_file():
            result.append("Approved invoice signature asset is missing")
        if not draft.preferred_intake_date:
            result.append("Course date must be confirmed by staff")
        if draft.status not in {'confirmed', 'paid', 'receipt_issued'}:
            result.append("Participant must confirm course date and enrollment details")
        if document and not document.signed:
            result.append("Document is an unsigned draft; regenerate with approved assets")
        return result

    def create_invoice(self, draft):
        if not draft.full_name or not draft.email or not draft.course:
            raise ValueError("A validated participant name, course and email are required")
        existing = self._invoice(draft)
        if existing:
            return existing
        selected_course = self.catalogue.course_for(draft.course_id or draft.course)
        if selected_course is None:
            raise ValueError("The selected course is not in the approved catalogue")
        selected_fee = draft.course_fee or self.catalogue.course_fee_for(selected_course)
        if selected_fee is None:
            raise ValueError("The approved course fee must be confirmed before invoicing")
        fee = money(selected_fee)
        uen = selected_course.get("paynow", {}).get("uen")
        if not uen:
            raise ValueError("Approved PayNow recipient UEN is required")
        export = MasterInvoiceExport.query.filter_by(enrollment_id=draft.id).one_or_none()
        invoice_number = (
            export.invoice_number
            if export is not None and export.invoice_number
            else f"QM-INV-{utc_now():%Y%m%d}-{uuid4().hex[:12].upper()}"
        )
        invoice = Invoice(enrollment_id=draft.id, number=invoice_number,
                          course_name=draft.course, participant_name=draft.full_name, course_date=draft.preferred_intake_date,
                          course_fee=fee, skillsfuture_amount=Decimal("0.00"), net_payable=fee, recipient_uen=uen,
                          due_date=date.today() + timedelta(days=int(current_app.config.get("PAYMENT_DUE_DAYS", 14))))
        db.session.add(invoice)
        db.session.flush()
        self.get_case(draft)
        document = self._save_document(invoice, "invoice")
        self._queue_document(draft, document)
        return invoice

    def _queue_document(self, draft, document):
        if self.document_blockers(draft, document):
            self._audit(draft, "document_delivery_blocked", document_id=document.id)
            return None
        invoice = db.session.get(Invoice, document.invoice_id)
        course = self.catalogue.course_for(draft.course_id or draft.course)
        if course is None:
            raise ValueError("The selected course is not in the approved catalogue")
        skillsfuture = course.get("skillsfuture", {})
        portal = skillsfuture.get("claim_page", "https://sfc.myskillsfuture.gov.sg/claim")
        if document.kind == "invoice":
            subject = f"Q&M course invoice {invoice.number}"
            body = (f"Dear {draft.full_name},\n\nYour course invoice is attached. Course date: {iso(invoice.course_date)}.\n"
                    f"Total amount due: S${invoice.course_fee:.2f}. "
                    f"UEN: {invoice.recipient_uen}. Use reference {invoice.number}.\n")
            if skillsfuture.get("basic_tier_accepted"):
                body += (f"SkillsFuture instructions: sign in yourself to the official MySkillsFuture portal, check your basic-tier balance, "
                         f"select the course/intake and submit the claim with the attached invoice: {portal}\n"
                         "Do not send us your Singpass password or OTP. Mid-Career Credits are not accepted for this course.\n")
        else:
            subject = f"Q&M {document.kind.replace('_', ' ')} {document.number}"
            body = f"Dear {draft.full_name},\n\nYour {document.kind.replace('_', ' ')} for invoice {invoice.number} is attached.\n\nQ&M Course Administration"
        attachments = [{"filename": f"{document.number}-v{document.version}.pdf", "storage_key": document.storage_key, "mime_type": "application/pdf", "content_type": "application/pdf"}]
        outbound = self.delivery.queue("email", draft.email, subject, body, attachments=attachments, conversation_id=draft.conversation_id, purpose=document.kind)
        document.delivery_id = outbound.id
        if document.kind == "invoice":
            invoice.delivery_id = outbound.id
            if draft.mobile_number:
                self.delivery.queue("whatsapp", draft.mobile_number, None,
                    f"Enrollment recorded: {draft.course}, {iso(invoice.course_date)}, {draft.full_name}. Invoice {invoice.number} for S${invoice.course_fee:.2f} is queued for delivery to {draft.email}.",
                    conversation_id=draft.conversation_id, purpose="enrollment_confirmation")
        self._audit(draft, "document_queued", document_id=document.id, delivery_id=outbound.id)
        return outbound

    def regenerate_invoice(self, draft, actor="staff"):
        invoice = self._invoice(draft) or self.create_invoice(draft)
        invoice.course_date = draft.preferred_intake_date
        document = self._save_document(invoice, "invoice")
        self._queue_document(draft, document)
        self._audit(draft, "invoice_revised", actor, document_id=document.id)
        return document

    def assign_course_date(self, draft, course_date, actor="staff"):
        previous = iso(draft.preferred_intake_date)
        draft.preferred_intake_date = course_date
        self._audit(draft, "course_date_assigned", actor, previous=previous, selected=iso(course_date))
        if self._invoice(draft):
            self.regenerate_invoice(draft, actor)
            if self._latest_document(draft, "receipt"):
                self.issue_receipt(draft, regenerate=True)

    def resend_document(self, draft, kind):
        if kind not in {"invoice", "receipt"}:
            raise ValueError("Unsupported document type")
        document = self._latest_document(draft, kind)
        if not document:
            raise ValueError(f"No {kind} has been generated")
        from backend.models.communications import OutboundMessage
        existing = db.session.get(OutboundMessage, document.delivery_id) if document.delivery_id else None
        if existing and existing.status in {"queued", "sending"}:
            return existing
        return self._queue_document(draft, document)

    def sync_delivery_status(self):
        from backend.models.communications import OutboundMessage
        count = 0
        for invoice in db.session.execute(db.select(Invoice).where(Invoice.delivery_id.is_not(None))).scalars():
            outbound = db.session.get(OutboundMessage, invoice.delivery_id)
            case = self.get_case(invoice.enrollment)
            if outbound and outbound.status in {"sent", "delivered", "read"} and invoice.sent_at is None:
                invoice.sent_at = outbound.sent_at or utc_now()
                if case.status == "Enrolled":
                    case.status = "Invoice Sent"
                    self._audit(invoice.enrollment, "invoice_sent", delivery_id=outbound.id)
                    case.status = "Awaiting Payment"
                count += 1
        for document in db.session.execute(db.select(FinancialDocument).where(FinancialDocument.kind == "receipt", FinancialDocument.delivery_id.is_not(None))).scalars():
            outbound = db.session.get(OutboundMessage, document.delivery_id)
            case = db.session.execute(db.select(FinanceCase).where(FinanceCase.enrollment_id == document.enrollment_id)).scalar_one()
            if outbound and outbound.status in {"sent", "delivered", "read"} and case.status == "Paid":
                case.status = "Receipt Issued"
                self._audit(db.session.get(EnrollmentDraft, document.enrollment_id), "receipt_sent", document_id=document.id)
                count += 1
        return count

    def _validate_image(self, content, mime_type):
        if mime_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise ValueError("Payment evidence must be a PNG, JPEG or WebP image")
        if not content or len(content) > int(current_app.config.get("PAYMENT_MAX_IMAGE_BYTES", 5 * 1024 * 1024)):
            raise ValueError("Payment image is missing or too large")
        try:
            with Image.open(BytesIO(content)) as image:
                if image.width * image.height > 20_000_000:
                    raise ValueError("Image dimensions exceed limit")
                expected = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}[mime_type]
                if image.format != expected:
                    raise ValueError("Image content does not match its media type")
                image.verify()
        except (OSError, Image.DecompressionBombError):
            raise ValueError("Payment image is not readable") from None

    def _checks(self, invoice, extraction, digest):
        facts = extraction.to_dict()
        reasons = []
        if not extraction.readable:
            reasons.append("unreadable")
        if extraction.error:
            reasons.append(extraction.error)
        if not extraction.successful:
            reasons.append("transfer_not_confirmed_successful")
        if extraction.multiple_payments:
            reasons.append("multiple_payments")
        if not 0 <= extraction.confidence <= 1 or extraction.confidence < float(current_app.config.get("PAYMENT_CONFIDENCE_THRESHOLD", .98)):
            reasons.append("low_confidence")
        try:
            if money(extraction.amount) != money(invoice.net_payable):
                reasons.append("amount_mismatch")
        except ValueError:
            reasons.append("amount_unreadable")
        if (extraction.currency or "").upper() != "SGD":
            reasons.append("currency_mismatch")
        recipient = normalize_reference(extraction.recipient)
        allowed = {normalize_reference(invoice.recipient_uen)}
        for alias in current_app.config.get("PAYNOW_RECIPIENT_ALIASES", []):
            allowed.add(normalize_reference(alias))
        if not recipient or recipient not in allowed:
            reasons.append("recipient_mismatch")
        reference = normalize_reference(extraction.reference)
        if not reference or len(reference) < 4:
            reasons.append("reference_missing")
        elif db.session.execute(db.select(Payment.id).where(Payment.reference == reference)).first():
            reasons.append("duplicate_transaction_reference")
        if normalize_reference(extraction.invoice_number) != normalize_reference(invoice.number):
            reasons.append("invoice_reference_missing_or_mismatch")
        try:
            transaction_date = date.fromisoformat(extraction.transaction_date or "")
            if transaction_date > date.today() or transaction_date < invoice.created_at.date() - timedelta(days=1):
                reasons.append("transaction_date_implausible")
        except (ValueError, TypeError):
            reasons.append("transaction_date_unreadable")
        if db.session.execute(db.select(PaymentEvidence.id).where(PaymentEvidence.sha256 == digest)).first():
            reasons.append("duplicate_screenshot")
        if db.session.execute(db.select(Payment.id).where(Payment.invoice_id == invoice.id, Payment.method == "paynow")).first():
            reasons.append("invoice_paynow_already_paid")
        if self.get_case(invoice.enrollment).status in {"Cancelled", "Credit Note Required"}:
            reasons.append("enrollment_cancelled_or_credit_requested")
        return reasons, facts

    def submit_payment(self, draft, image_bytes, mime_type, channel="whatsapp", source_event_id=None, payment_type="paynow"):
        if payment_type not in {"paynow", "skillsfuture"}:
            raise ValueError("Only PayNow and SkillsFuture evidence is supported")
        self._validate_image(image_bytes, mime_type)
        invoice = self._invoice(draft) or self.create_invoice(draft)
        if source_event_id:
            prior = db.session.execute(db.select(PaymentEvidence).where(PaymentEvidence.source_event_id == source_event_id)).scalar_one_or_none()
            if prior:
                if prior.enrollment_id != draft.id:
                    raise ValueError("Payment event has already been used")
                return self._payment_result(prior)
        digest = hashlib.sha256(image_bytes).hexdigest()
        vision = self.vision or PaymentVision(current_app.config.get("ANTHROPIC_API_KEY", ""), current_app.config.get("PAYMENT_VISION_MODEL", ""))
        try:
            extraction = vision.extract(image_bytes, mime_type)
            if isinstance(extraction, dict):
                extraction = PaymentExtraction(**extraction)
            if not isinstance(extraction, PaymentExtraction):
                raise ValueError("Invalid extraction")
        except Exception:
            extraction = PaymentExtraction(error="vision_extraction_failed")
        reasons, facts = self._checks(invoice, extraction, digest)
        if payment_type == "skillsfuture":
            reasons.append("skillsfuture_claim_requires_accounts_confirmation")
        if not current_app.config.get("PAYMENT_AUTO_CONFIRM", False):
            reasons.append("automatic_confirmation_not_enabled")
        evidence = PaymentEvidence(enrollment_id=draft.id, sha256=digest, channel=channel, source_event_id=source_event_id,
                                   mime_type=mime_type, storage_key=self.storage.save(image_bytes), extracted_json=json.dumps(facts), payment_type=payment_type)
        db.session.add(evidence)
        db.session.flush()
        if not reasons:
            try:
                with db.session.begin_nested():
                    self._confirm_payment(draft, evidence, invoice.net_payable, "paynow", extraction.reference, extraction.transaction_date, "vision_policy")
            except IntegrityError:
                reasons.append("duplicate_transaction_reference")
        if reasons:
            evidence.verdict = "review"
            review = PaymentReview(enrollment_id=draft.id, evidence_id=evidence.id, reasons_json=json.dumps(list(dict.fromkeys(reasons))))
            db.session.add(review)
            db.session.flush()
            self._audit(draft, "payment_review_requested", evidence_id=evidence.id, reasons=reasons)
            staff_email = current_app.config.get("STAFF_ALERT_EMAIL")
            if staff_email:
                self.delivery.queue("email", staff_email, "Q&M payment needs review", f"Enrollment {draft.id} requires staff payment review. Reasons: {', '.join(reasons)}. Sign in to the staff portal to view protected evidence.", purpose="payment_review", conversation_id=draft.conversation_id)
        else:
            evidence.verdict = "confirmed"
        return self._payment_result(evidence)

    def _confirm_payment(self, draft, evidence, amount, method, reference, transaction_date, actor):
        invoice = self._invoice(draft)
        if self.get_case(draft).status in {"Cancelled", "Credit Note Required"}:
            raise ValueError("Cannot confirm payment on a cancelled enrollment or pending credit note")
        expected = invoice.net_payable if method == "paynow" else invoice.skillsfuture_amount
        if money(amount) != money(expected) or money(expected) <= 0:
            raise ValueError("Confirmed amount must match the outstanding payment component")
        reference = normalize_reference(reference)
        if len(reference) < 4:
            raise ValueError("A verified unique bank or claim reference is required")
        if db.session.execute(db.select(Payment.id).where(Payment.reference == reference)).first():
            raise ValueError("Transaction reference has already been confirmed")
        if db.session.execute(db.select(Payment.id).where(Payment.invoice_id == invoice.id, Payment.method == method)).first():
            raise ValueError("This invoice payment component is already confirmed")
        try:
            paid_date = date.fromisoformat(transaction_date)
        except (TypeError, ValueError):
            raise ValueError("A valid transaction date is required") from None
        if paid_date > date.today():
            raise ValueError("A future transfer cannot be confirmed")
        payment = Payment(enrollment_id=draft.id, invoice_id=invoice.id, evidence_id=evidence.id if evidence else None,
                          amount=money(amount), method=method, reference=reference, transaction_date=paid_date, confirmed_by=actor)
        db.session.add(payment)
        db.session.flush()
        self._audit(draft, "payment_confirmed", actor, payment_id=payment.id, method=method, amount=str(amount))
        total = sum((row.amount for row in db.session.execute(db.select(Payment).where(Payment.invoice_id == invoice.id)).scalars()), Decimal(0))
        if total == invoice.course_fee:
            self.get_case(draft).status = "Paid"
            self.issue_receipt(draft)
            from backend.models.operations import CourseDate
            intake = db.session.get(CourseDate, draft.intake_id) if draft.intake_id else None
            course = self.catalogue.course_for(draft.course_id or draft.course)
            self.delivery.queue("email", draft.email, "Q&M course payment confirmation",
                f"Dear {draft.full_name},\n\nYour S${invoice.course_fee:.2f} course payment is confirmed.\n"
                f"Course: {draft.course}\nDates: {draft.preferred_intake_date or 'Awaiting staff confirmation'}"
                + (f" to {intake.end_date}" if intake else "")
                + f"\nTraining time: {course['training_time']}\nVenue: {course.get('venue', {}).get('customer_reply', 'Staff confirmation required')}\n"
                + "Your receipt email is processed separately. Contact course administration if you need attendance assistance.",
                conversation_id=draft.conversation_id, purpose="course_payment_confirmation")
            if draft.mobile_number:
                self.delivery.queue("whatsapp", draft.mobile_number, None, f"Payment confirmed: S${payment.amount:.2f}, {paid_date.isoformat()}, reference {reference}. Your receipt is queued for email delivery.", conversation_id=draft.conversation_id, purpose="payment_confirmation")
        return payment

    def decide_payment_review(self, review_id, decision, actor, note, reference=None, amount=None, transaction_date=None):
        review = db.session.get(PaymentReview, review_id)
        if not review:
            raise LookupError("Payment review not found")
        if review.status != "pending":
            raise ValueError("Payment review has already been decided")
        if decision not in {"approve", "reject"} or not str(note or "").strip():
            raise ValueError("An approve/reject decision and review note are required")
        evidence = db.session.get(PaymentEvidence, review.evidence_id)
        draft = db.session.get(EnrollmentDraft, review.enrollment_id)
        facts = json.loads(evidence.extracted_json)
        if decision == "approve":
            reasons = json.loads(review.reasons_json)
            if "duplicate_screenshot" in reasons or "duplicate_transaction_reference" in reasons:
                raise ValueError("Duplicate payment evidence cannot be approved; submit distinct evidence after reconciliation")
            self._confirm_payment(draft, evidence, amount if amount is not None else facts.get("amount"), evidence.payment_type,
                                  reference or facts.get("reference"), transaction_date or facts.get("transaction_date"), actor)
            evidence.verdict = "confirmed"
        else:
            evidence.verdict = "rejected"
        review.status = "approved" if decision == "approve" else "rejected"
        review.note, review.decided_by, review.decided_at = str(note).strip(), actor, utc_now()
        self._audit(draft, "payment_review_decided", actor, review_id=review.id, decision=decision)
        return review

    def confirm_skillsfuture(self, draft, reference, transaction_date, actor, note):
        if not str(note or "").strip():
            raise ValueError("Accounts reconciliation note is required")
        invoice = self._invoice(draft)
        if not invoice or invoice.skillsfuture_amount <= 0:
            raise ValueError("No SkillsFuture claim is recorded on this invoice")
        return self._confirm_payment(draft, None, invoice.skillsfuture_amount, "skillsfuture", reference, transaction_date, actor)

    def issue_receipt(self, draft, regenerate=False):
        invoice = self._invoice(draft)
        if not invoice:
            raise ValueError("Invoice not found")
        payments = list(db.session.execute(db.select(Payment).where(Payment.invoice_id == invoice.id)).scalars())
        if sum((row.amount for row in payments), Decimal(0)) != invoice.course_fee:
            raise ValueError("All PayNow and SkillsFuture payment components must be confirmed before a receipt is issued")
        existing = self._latest_document(draft, "receipt")
        if existing and not regenerate:
            return existing
        payment = next((row for row in payments if row.method == "paynow"), payments[0])
        number = existing.number if existing else invoice.number.replace("INV", "RCT", 1)
        document = self._save_document(invoice, "receipt", number, payment)
        self._queue_document(draft, document)
        return document

    def request_credit_note(self, draft, reason, actor):
        invoice = self._invoice(draft)
        if not invoice:
            raise ValueError("Invoice not found")
        if not str(reason or "").strip():
            raise ValueError("A cancellation reason is required")
        prior = db.session.execute(db.select(CreditNoteRequest).where(CreditNoteRequest.enrollment_id == draft.id, CreditNoteRequest.status.in_(["pending", "approved"]))).scalars().first()
        if prior:
            raise ValueError("A credit note is already pending or approved")
        if db.session.execute(db.select(Payment.id).where(Payment.invoice_id == invoice.id)).first():
            raise ValueError("This invoice has confirmed payment; paid cancellations require a separately reconciled refund workflow")
        record = CreditNoteRequest(enrollment_id=draft.id, invoice_id=invoice.id, reason=str(reason).strip(), requested_by=actor)
        db.session.add(record)
        self.get_case(draft).status = "Credit Note Required"
        db.session.flush()
        self._audit(draft, "credit_note_requested", actor, request_id=record.id)
        return record

    def decide_credit_note(self, request_id, decision, actor, note=""):
        record = db.session.get(CreditNoteRequest, request_id)
        if not record:
            raise LookupError("Credit note request not found")
        if record.status != "pending" or decision not in {"approve", "reject"}:
            raise ValueError("A pending request and approve/reject decision are required")
        draft = db.session.get(EnrollmentDraft, record.enrollment_id)
        invoice = self._invoice(draft)
        if decision == "approve":
            if db.session.execute(db.select(Payment.id).where(Payment.invoice_id == invoice.id)).first():
                raise ValueError("Confirmed payments must be reconciled before cancellation")
            document = self._save_document(invoice, "credit_note", invoice.number.replace("INV", "CN", 1), reason=record.reason)
            record.document_id = document.id
            self._queue_document(draft, document)
            self.get_case(draft).status = "Cancelled"
            draft.status = "cancelled"
        else:
            self.get_case(draft).status = "Awaiting Payment" if invoice.sent_at else "Enrolled"
        record.status = "approved" if decision == "approve" else "rejected"
        record.note, record.decided_by, record.decided_at = str(note or "").strip(), actor, utc_now()
        self._audit(draft, "credit_note_decided", actor, request_id=record.id, decision=decision)
        return record

    def _payment_result(self, evidence):
        review = db.session.execute(db.select(PaymentReview).where(PaymentReview.evidence_id == evidence.id)).scalar_one_or_none()
        return {"verdict": evidence.verdict, "evidence": self.serialize_evidence(evidence),
                "review": self.serialize_review(review) if review else None,
                "enrollment": self.serialize_enrollment(db.session.get(EnrollmentDraft, evidence.enrollment_id))}

    def serialize_document(self, document):
        return {"id": document.id, "kind": document.kind, "number": document.number, "version": document.version, "signed": document.signed, "delivery_id": document.delivery_id, "created_at": iso(document.created_at), "download_url": f"/api/staff/documents/{document.id}"}

    def serialize_evidence(self, evidence):
        return {"id": evidence.id, "enrollment_id": evidence.enrollment_id, "channel": evidence.channel, "payment_type": evidence.payment_type, "mime_type": evidence.mime_type, "verdict": evidence.verdict, "created_at": iso(evidence.created_at), "download_url": f"/api/staff/evidence/{evidence.id}"}

    def serialize_review(self, review):
        evidence = db.session.get(PaymentEvidence, review.evidence_id)
        return {"id": review.id, "enrollment_id": review.enrollment_id, "evidence_id": review.evidence_id, "reasons": json.loads(review.reasons_json), "status": review.status, "note": review.note, "decided_by": review.decided_by, "created_at": iso(review.created_at), "extracted": json.loads(evidence.extracted_json), "evidence": self.serialize_evidence(evidence)}

    def serialize_credit_note(self, record):
        return {"id": record.id, "enrollment_id": record.enrollment_id, "invoice_id": record.invoice_id, "status": record.status, "reason": record.reason, "note": record.note, "document_id": record.document_id, "requested_by": record.requested_by, "decided_by": record.decided_by, "created_at": iso(record.created_at)}

    def serialize_enrollment(self, draft):
        invoice = self._invoice(draft)
        template_service = getattr(self, "invoice_templates", None)
        invoice_document = (
            template_service.for_enrollment(draft.id) if template_service else None
        )
        email_service = getattr(self, "invoice_email", None)
        case = db.session.execute(db.select(FinanceCase).where(FinanceCase.enrollment_id == draft.id)).scalar_one_or_none()
        docs = list(db.session.execute(db.select(FinancialDocument).where(FinancialDocument.enrollment_id == draft.id).order_by(FinancialDocument.created_at)).scalars())
        payments = list(db.session.execute(db.select(Payment).where(Payment.enrollment_id == draft.id)).scalars())
        total = sum((row.amount for row in payments), Decimal(0))
        allocation = []
        if Decimal(draft.skillsfuture_amount or 0) > 0:
            allocation.append({"method": "SkillsFuture Credit", "amount": str(draft.skillsfuture_amount)})
        if Decimal(draft.paynow_amount or 0) > 0:
            allocation.append({"method": "PayNow", "amount": str(draft.paynow_amount)})
        if draft.payment_method == "utap":
            allocation.append({"method": "UTAP reimbursement", "amount": None})
        intake_display = _intake_display(draft.intake_start_date or draft.preferred_intake_date, draft.intake_end_date)
        previous_intake = (
            db.session.get(CourseDate, draft.previous_intake_id)
            if draft.previous_intake_id
            else None
        )
        expected = Decimal(draft.course_fee or (invoice.course_fee if invoice else 0) or 0)
        payment_verification_status = (
            "Verified" if expected > 0 and total == expected else "Awaiting staff verification"
        )
        return {"id": draft.id, "conversation_id": draft.conversation_id, "full_name": draft.full_name, "email": draft.email, "mobile_number": draft.mobile_number,
                "course": draft.course, "course_id": draft.course_id, "course_fee": str(draft.course_fee) if draft.course_fee is not None else None,
                "friendly_reference": draft.friendly_reference, "course_date": iso(draft.preferred_intake_date), "intake_id": draft.intake_id,
                "intake_start_date": iso(draft.intake_start_date), "intake_end_date": iso(draft.intake_end_date),
                "intake_display": intake_display, "intake_is_demo": bool(draft.intake_is_demo),
                "payment_allocation": allocation, "payment_verification_status": payment_verification_status,
                "nric_masked": mask_nric(draft.nric),
                "status": "Cancelled" if draft.status == "cancelled" else (case.status if case else ("Enquiry" if draft.status == "collecting" else "Enrolled")), "draft_status": draft.status,
                "cancellation_requested_at": iso(draft.cancellation_requested_at), "cancelled_at": iso(draft.cancelled_at),
                "cancellation_reason": draft.cancellation_reason,
                "reschedule_requested_at": iso(draft.reschedule_requested_at), "rescheduled_at": iso(draft.rescheduled_at),
                "previous_intake_display": _intake_display(previous_intake.date, previous_intake.end_date) if previous_intake else None,
                "refund_review_required": bool(draft.refund_review_required),
                "documents_blocked": self.document_blockers(draft, docs[-1] if docs else None), "created_at": iso(draft.created_at),
                "invoice": {"id": invoice.id, "number": invoice.number, "course_fee": str(invoice.course_fee), "skillsfuture_amount": str(invoice.skillsfuture_amount), "net_payable": str(invoice.net_payable), "due_date": iso(invoice.due_date), "sent_at": iso(invoice.sent_at), "delivery_id": invoice.delivery_id} if invoice else None,
                "invoice_document": template_service.serialize(invoice_document) if template_service else None,
                "invoice_email": email_service.serialize(draft, invoice_document) if email_service else None,
                "paid_amount": f"{total:.2f}", "outstanding_amount": f"{invoice.course_fee - total:.2f}" if invoice and (not case or case.status != "Cancelled") else "0.00",
                "overdue": bool(invoice and invoice.due_date < date.today() and total < invoice.course_fee and (not case or case.status != "Cancelled")),
                "payments": [{"id": row.id, "evidence_id": row.evidence_id, "amount": str(row.amount), "method": row.method, "reference": row.reference, "transaction_date": iso(row.transaction_date), "confirmed_by": row.confirmed_by} for row in payments],
                "documents": [self.serialize_document(row) for row in docs],
                "credit_notes": [self.serialize_credit_note(row) for row in db.session.execute(db.select(CreditNoteRequest).where(CreditNoteRequest.enrollment_id == draft.id)).scalars()]}

    def serialize_case(self, case):
        result = self.serialize_enrollment(db.session.get(EnrollmentDraft, case.enrollment_id))
        result["case_id"] = case.id
        return result

    def list_enrollments(self, status=None):
        rows = [self.serialize_enrollment(row) for row in db.session.execute(db.select(EnrollmentDraft).order_by(EnrollmentDraft.created_at.desc())).scalars()]
        return [row for row in rows if not status or row["status"] == status or (status == "Overdue" and row["overdue"])]

    def dashboard(self):
        rows = self.list_enrollments()
        return {"total_enrollments": len(rows), "status_counts": dict(Counter(row["status"] for row in rows)),
                "pending_reviews": db.session.execute(db.select(db.func.count()).select_from(PaymentReview).where(PaymentReview.status == "pending")).scalar(),
                "pending_receipts": sum(row["status"] == "Paid" for row in rows), "overdue": sum(row["overdue"] for row in rows),
                "credit_note_requests": db.session.execute(db.select(db.func.count()).select_from(CreditNoteRequest).where(CreditNoteRequest.status == "pending")).scalar(),
                "paid_total": f"{sum((money(row['paid_amount']) for row in rows), Decimal(0)):.2f}",
                "outstanding_total": f"{sum((money(row['outstanding_amount']) for row in rows), Decimal(0)):.2f}"}

    def payment_report_csv(self):
        output = StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["Enrollment ID", "Participant", "Course", "Course date", "Invoice", "Status", "Paid SGD", "Outstanding SGD", "Overdue"])
        def safe(value):
            value = str(value or "")
            return "'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r", "\n")) else value
        for row in self.list_enrollments():
            writer.writerow([safe(value) for value in [row["id"], row["full_name"], row["course"], row["course_date"], row["invoice"]["number"] if row["invoice"] else "", row["status"], row["paid_amount"], row["outstanding_amount"], row["overdue"]]])
        return output.getvalue()

    def run_balance_reminders(self, today=None):
        """Queue one reminder per intake seven days before training, repeat safe."""
        from backend.models.communications import OutboundMessage
        today = today or date.today()
        count = 0
        for draft in db.session.execute(db.select(EnrollmentDraft).where(EnrollmentDraft.preferred_intake_date == today + timedelta(days=7))).scalars():
            record = self.serialize_enrollment(draft)
            if not record['invoice'] or money(record['outstanding_amount']) <= 0 or record['status'] in {'Cancelled', 'Credit Note Required'}:
                continue
            operations = current_app.extensions.get('operations')
            if current_app.config.get('REQUIRE_CONSENT') and operations:
                consent = operations.consent_for(draft.conversation_id)
                if not (consent and consent.granted):
                    continue
            purpose = f"balance_reminder:{draft.id}:{draft.preferred_intake_date}"
            if db.session.execute(db.select(OutboundMessage.id).where(OutboundMessage.purpose == purpose)).first():
                continue
            self.delivery.queue("email", draft.email, "Q&M outstanding course balance reminder",
                f"Dear {draft.full_name}, your {draft.course} starts on {draft.preferred_intake_date}. "
                f"Invoice {record['invoice']['number']} has an outstanding balance of S${record['outstanding_amount']}. "
                "Please complete payment and email your evidence. If you have paid or your claim is pending, contact course administration for review.",
                conversation_id=draft.conversation_id, purpose=purpose)
            self._audit(draft, "balance_reminder_queued", outstanding=record['outstanding_amount'], course_date=str(draft.preferred_intake_date))
            operations = current_app.extensions.get('operations')
            if operations:
                operations.escalate(draft.conversation_id, f"Outstanding balance S${record['outstanding_amount']} seven days before training.", "outstanding_balance")
            count += 1
        return count

    def queue_nightly_report(self):
        from backend.models.communications import OutboundMessage
        recipient = current_app.config.get("ACCOUNTANT_REPORT_EMAIL")
        if not recipient:
            return None
        purpose = f"nightly_report:{date.today().isoformat()}"
        existing = db.session.execute(db.select(OutboundMessage).where(OutboundMessage.purpose == purpose)).scalars().first()
        if existing:
            return existing
        key = self.storage.save(self.payment_report_csv().encode("utf-8"))
        return self.delivery.queue("email", recipient, f"Q&M payment status report {date.today().isoformat()}", "The protected accounting payment status report is attached. Sign in to the accountant portal for evidence and approval actions.", attachments=[{"filename": "payment-status.csv", "storage_key": key, "content_type": "text/csv"}], purpose=purpose)
