"""Invoice email delivery is private, idempotent and fully SMTP-mocked."""

import smtplib
import ssl
from datetime import date
from pathlib import Path

import pytest

from backend.extensions import db
from backend.models import Conversation, EnrollmentDraft, User
from backend.models.communications import OutboundMessage
from backend.models.finance import FinanceAudit, InvoiceGeneration
from backend.services.invoice_email_service import InvoiceEmailError
from backend.tests.conftest import authenticated_client, send
from backend.tests.test_invoice_template_generation import (
    FakePdfConverter,
    confirmed_enrollment,
    invoice_app,
    _login,
    _confirm_two_course_via_chat,
)


LOGO = (
    Path(__file__).resolve().parents[1]
    / "assets"
    / "email"
    / "qm-training-logo.png"
)


class FakeSMTP:
    def __init__(self, sink, failure=None):
        self.sink = sink
        self.failure = failure
        self.started_tls = False
        self.logged_in = False

    def __call__(self, host, port, timeout):
        self.connection = (host, port, timeout)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def starttls(self, context):
        assert isinstance(context, ssl.SSLContext)
        assert context.verify_mode == ssl.CERT_REQUIRED
        self.started_tls = True

    def login(self, username, password):
        assert self.started_tls
        assert username == "smtp.test@example.com"
        assert password == "fictional-app-password"
        self.logged_in = True
        if self.failure == "authentication":
            raise smtplib.SMTPAuthenticationError(535, b"synthetic")

    def send_message(self, message):
        assert self.logged_in
        if self.failure == "timeout":
            raise TimeoutError("synthetic")
        if self.failure == "tls":
            raise ssl.SSLError("synthetic")
        self.sink.append(message)
        return {}


def configure_email(app, sink, failure=None, *, password=True):
    app.config.update(
        EMAIL_DELIVERY_ENABLED=True,
        EMAIL_PROVIDER="smtp",
        EMAIL_FROM_NAME="Q&M Training",
        EMAIL_FROM_ADDRESS="smtp.test@example.com",
        EMAIL_REPLY_TO="smtp.test@example.com",
        EMAIL_LOGO_PATH=str(LOGO),
        SMTP_HOST="smtp.gmail.com",
        SMTP_PORT=587,
        SMTP_USERNAME="smtp.test@example.com",
        SMTP_PASSWORD="fictional-app-password" if password else "",
        SMTP_USE_TLS=True,
        SMTP_TIMEOUT_SECONDS=30,
        INVOICE_EMAIL_MAX_RETRIES=3,
        INVOICE_EMAIL_RESEND_COOLDOWN_SECONDS=60,
    )
    app.extensions["delivery"].smtp_factory = FakeSMTP(sink, failure)


def generate_invoice(app, enrollment_id):
    app.extensions["invoice_template"].converter = FakePdfConverter()
    with app.app_context():
        return app.extensions["invoice_template"].generate(enrollment_id).id


def test_confirmation_automatically_emails_generated_invoice(tmp_path, fake_ai):
    sent = []
    app = invoice_app(tmp_path, fake_ai)
    configure_email(app, sent)
    app.extensions["invoice_template"].converter = FakePdfConverter()
    owner = authenticated_client(app, email="owner@example.com")
    conversation_id = owner.post("/api/conversations").get_json()["conversation_id"]
    send(owner, conversation_id, "I want to enroll")
    send(owner, conversation_id, "1")
    send(owner, conversation_id, "1")
    send(
        owner,
        conversation_id,
        "Full name: Test Student; NRIC/FIN: S1234567D; Date of birth: 15 May 2000; "
        "Email: test.student@example.com; Mobile number: 91234567; "
        "SkillsFuture S$400; PayNow S$200",
    )
    response = send(owner, conversation_id, "Confirm")
    assert response.status_code == 200
    assert "invoice is being prepared" in response.get_json()["message"]["content"].casefold()
    assert len(sent) == 1

    with app.app_context():
        delivery = OutboundMessage.query.filter_by(email_type="invoice").one()
        draft = EnrollmentDraft.query.one()
        generation = InvoiceGeneration.query.one()
        assert delivery.status == "sent"
        assert delivery.recipient == draft.email == "test.student@example.com"
        assert delivery.invoice_number == generation.invoice_number
        assert delivery.document_sha256 == generation.sha256
        assert delivery.attempts == 1
        assert FinanceAudit.query.filter_by(action="invoice_email_automatic_queued").count() == 1
        assert FinanceAudit.query.filter_by(action="invoice_email_delivery_attempt").count() == 1
        assert FinanceAudit.query.filter_by(action="invoice_email_sent").count() == 1

    status = owner.get(f"/api/conversations/{conversation_id}/status").get_json()
    email_status = status["finance"]["invoice_email"]
    assert email_status["status"] == "sent"
    assert email_status["recipient_masked"] == "t***@example.com"
    assert "test.student@example.com" not in str(email_status)
    calls_before = (
        len(fake_ai.classify_calls),
        len(fake_ai.enquiry_calls),
        len(fake_ai.enrollment_calls),
    )
    email_question = send(owner, conversation_id, "Which email did you send it to?")
    assert email_question.status_code == 200
    assert "t***@example.com" in email_question.get_json()["message"]["content"]
    assert calls_before == (
        len(fake_ai.classify_calls),
        len(fake_ai.enquiry_calls),
        len(fake_ai.enrollment_calls),
    )


def test_grouped_invoice_revision_emails_exactly_once_per_document_version(
    tmp_path, fake_ai
):
    sent = []
    app = invoice_app(tmp_path, fake_ai)
    configure_email(app, sent)
    app.extensions["invoice_template"].converter = FakePdfConverter()
    owner = authenticated_client(app, email="group-email@example.com")
    conversation_id = _confirm_two_course_via_chat(owner)
    assert len(sent) == 1

    send(owner, conversation_id, "Cancel my Front Desk Operations course")
    send(owner, conversation_id, "Confirm course cancellation")
    assert len(sent) == 2
    send(owner, conversation_id, "Confirm course cancellation")
    assert len(sent) == 2

    latest = sent[-1]
    plain = latest.get_body(preferencelist=("plain",)).get_content()
    assert "Basic Certificate in Dental Assisting" in plain
    assert "Certificate in Dental Clinic Front Desk Operations" not in plain
    assert "S$600 nett" in plain
    with app.app_context():
        deliveries = OutboundMessage.query.filter_by(email_type="invoice").all()
        assert len(deliveries) == 2
        assert len({row.idempotency_key for row in deliveries}) == 2
        assert InvoiceGeneration.query.one().version == 2


def test_cancelled_course_readdition_queues_one_email_for_restored_invoice_version(
    tmp_path, fake_ai
):
    sent = []
    app = invoice_app(tmp_path, fake_ai)
    configure_email(app, sent)
    app.extensions["invoice_template"].converter = FakePdfConverter()
    owner = authenticated_client(app, email="readd-group-email@example.com")
    conversation_id = _confirm_two_course_via_chat(owner)

    send(owner, conversation_id, "Cancel course 2")
    send(owner, conversation_id, "Confirm course cancellation")
    send(owner, conversation_id, "Add the Front Desk Operations course")
    send(owner, conversation_id, "1")
    send(owner, conversation_id, "Confirm course addition")
    assert len(sent) == 3

    send(owner, conversation_id, "Confirm course addition")
    assert len(sent) == 3
    plain = sent[-1].get_body(preferencelist=("plain",)).get_content()
    assert "Basic Certificate in Dental Assisting" in plain
    assert "Certificate in Dental Clinic Front Desk Operations" in plain
    assert "S$1,050 nett" in plain
    with app.app_context():
        deliveries = OutboundMessage.query.filter_by(email_type="invoice").all()
        assert len(deliveries) == 3
        assert len({row.idempotency_key for row in deliveries}) == 3
        assert InvoiceGeneration.query.one().version == 3


def test_message_has_approved_structure_attachment_cid_and_no_sensitive_fields(tmp_path, fake_ai):
    sent = []
    app = invoice_app(tmp_path, fake_ai)
    configure_email(app, sent)
    enrollment_id = confirmed_enrollment(app, full_name="<b>Test</b> Student")
    generation_id = generate_invoice(app, enrollment_id)
    with app.app_context():
        draft = db.session.get(EnrollmentDraft, enrollment_id)
        generation = db.session.get(InvoiceGeneration, generation_id)
        app.extensions["invoice_email"].queue_automatic(draft, generation)

    message = sent[0]
    assert message["From"] == "Q&M Training <smtp.test@example.com>"
    assert message["To"] == "test.student@example.com"
    assert message["Reply-To"] == "smtp.test@example.com"
    assert message["Subject"] == "Q&M Training – Invoice DEMO-INV-2027-000001"
    plain = message.get_body(preferencelist=("plain",)).get_content()
    html = message.get_body(preferencelist=("html",)).get_content()
    assert "Basic Certificate in Dental Assisting" in plain
    assert "8–9 February 2027" in plain
    assert "S$600 nett" in plain
    assert "Development demonstration only" not in plain
    assert "&lt;b&gt;Test&lt;/b&gt;" in html
    assert "cid:qm-training-logo" in html
    combined = plain + html
    assert "S1234567D" not in combined
    assert "15 May 2000" not in combined
    parts = list(message.walk())
    assert any(part.get_content_type() == "image/png" and part["Content-ID"] == "<qm-training-logo>" for part in parts)
    pdfs = [part for part in parts if part.get_content_type() == "application/pdf"]
    assert len(pdfs) == 1
    assert pdfs[0].get_filename() == "DEMO-INV-2027-000001.pdf"
    assert pdfs[0].get_payload(decode=True).startswith(b"%PDF-")


def test_idempotency_restart_and_historical_records_do_not_send(tmp_path, fake_ai):
    sent = []
    app = invoice_app(tmp_path, fake_ai)
    configure_email(app, sent)
    enrollment_id = confirmed_enrollment(app)
    generation_id = generate_invoice(app, enrollment_id)
    with app.app_context():
        draft = db.session.get(EnrollmentDraft, enrollment_id)
        generation = db.session.get(InvoiceGeneration, generation_id)
        first = app.extensions["invoice_email"].queue_automatic(draft, generation)
        second = app.extensions["invoice_email"].queue_automatic(draft, generation)
        assert first.id == second.id
        assert OutboundMessage.query.filter_by(email_type="invoice").count() == 1
        assert app.extensions["invoice_email"].process() == 0
    assert len(sent) == 1

    historical_sent = []
    restart_path = tmp_path / "restart"
    restart_path.mkdir()
    restarted = invoice_app(restart_path, fake_ai)
    configure_email(restarted, historical_sent)
    confirmed_enrollment(restarted)
    with restarted.app_context():
        assert restarted.extensions["invoice_email"].process() == 0
    assert historical_sent == []


@pytest.mark.parametrize("failure", ["authentication", "timeout", "tls"])
def test_smtp_failures_are_safe_and_preserve_enrollment_and_pdf(tmp_path, fake_ai, failure):
    sent = []
    app = invoice_app(tmp_path, fake_ai)
    configure_email(app, sent, failure)
    enrollment_id = confirmed_enrollment(app)
    generation_id = generate_invoice(app, enrollment_id)
    with app.app_context():
        draft = db.session.get(EnrollmentDraft, enrollment_id)
        generation = db.session.get(InvoiceGeneration, generation_id)
        original = app.extensions["invoice_template"].document_bytes(generation)
        delivery = app.extensions["invoice_email"].queue_automatic(draft, generation)
        assert delivery.status in {"failed", "pending"}
        assert delivery.failure_category in {"authentication", "timeout", "transport"}
        assert db.session.get(EnrollmentDraft, enrollment_id).status == "confirmed"
        assert app.extensions["invoice_template"].document_bytes(generation) == original
        assert generation.invoice_number == "DEMO-INV-2027-000001"
        assert "synthetic" not in (delivery.error or "")
    assert sent == []


def test_retry_reuses_same_invoice_and_missing_password_is_safe(tmp_path, fake_ai):
    app = invoice_app(tmp_path, fake_ai)
    configure_email(app, [], "authentication")
    enrollment_id = confirmed_enrollment(app)
    generation_id = generate_invoice(app, enrollment_id)
    with app.app_context():
        draft = db.session.get(EnrollmentDraft, enrollment_id)
        generation = db.session.get(InvoiceGeneration, generation_id)
        delivery = app.extensions["invoice_email"].queue_automatic(draft, generation)
        original = (delivery.id, delivery.invoice_number, delivery.document_sha256, delivery.attachments)
        sent = []
        app.extensions["delivery"].smtp_factory = FakeSMTP(sent)
        retried = app.extensions["invoice_email"].staff_retry(delivery.id, "accountant")
        assert (retried.id, retried.invoice_number, retried.document_sha256, retried.attachments) == original
        assert retried.status == "sent"
        assert len(sent) == 1

    disabled_path = tmp_path / "disabled"
    disabled_path.mkdir()
    disabled = invoice_app(disabled_path, fake_ai)
    configure_email(disabled, [], password=False)
    disabled_id = confirmed_enrollment(disabled)
    disabled_generation = generate_invoice(disabled, disabled_id)
    with disabled.app_context():
        row = disabled.extensions["invoice_email"].queue_automatic(
            db.session.get(EnrollmentDraft, disabled_id),
            db.session.get(InvoiceGeneration, disabled_generation),
        )
        assert row.status == "disabled"
        assert row.failure_category == "configuration"
        assert "password" not in str(disabled.extensions["invoice_email"].serialize(db.session.get(EnrollmentDraft, disabled_id))).casefold()


def test_participant_resend_is_owner_scoped_rejects_override_and_applies_cooldown(tmp_path, fake_ai):
    sent = []
    app = invoice_app(tmp_path, fake_ai)
    configure_email(app, sent)
    owner = authenticated_client(app, email="owner@example.com")
    other = authenticated_client(app, email="other@example.com")
    enrollment_id = confirmed_enrollment(app)
    generation_id = generate_invoice(app, enrollment_id)
    with app.app_context():
        draft = db.session.get(EnrollmentDraft, enrollment_id)
        draft.conversation.user_id = User.query.filter_by(email="owner@example.com").one().id
        app.extensions["invoice_email"].queue_automatic(
            draft, db.session.get(InvoiceGeneration, generation_id)
        )
        db.session.commit()

    path = f"/api/participant/enrollments/{enrollment_id}/invoice-email/resend"
    assert other.post(path, json={}).status_code == 404
    assert owner.post(path, json={"recipient": "attacker@example.com"}).status_code == 400
    cooldown = owner.post(path, json={"confirm": True})
    assert cooldown.status_code == 429
    assert len(sent) == 1

    with app.app_context():
        latest = app.extensions["invoice_email"].latest(enrollment_id)
        latest.created_at = latest.created_at.replace(year=2025)
        db.session.commit()
    resent = owner.post(path, json={"confirm": True})
    assert resent.status_code == 200
    assert len(sent) == 2
    assert all(message["To"] == "test.student@example.com" for message in sent)

    with app.app_context():
        draft = db.session.get(EnrollmentDraft, enrollment_id)
        draft.email = "changed-address@example.com"
        db.session.commit()
    changed = owner.post(path, json={"confirm": True})
    assert changed.status_code == 409
    assert "staff must verify" in changed.get_json()["error"]["message"].casefold()
    assert len(sent) == 2


def test_staff_invoice_email_controls_enforce_finance_roles(tmp_path, fake_ai):
    app = invoice_app(tmp_path, fake_ai)
    configure_email(app, [], "authentication")
    enrollment_id = confirmed_enrollment(app)
    generation_id = generate_invoice(app, enrollment_id)
    with app.app_context():
        delivery = app.extensions["invoice_email"].queue_automatic(
            db.session.get(EnrollmentDraft, enrollment_id),
            db.session.get(InvoiceGeneration, generation_id),
        )
        delivery_id = delivery.id

    operations = app.test_client()
    operations_token = _login(operations, "operations")
    denied = operations.post(
        f"/api/staff/enrollments/{enrollment_id}/invoice-email/retry",
        json={"delivery_id": delivery_id},
        headers={"X-CSRF-Token": operations_token},
    )
    assert denied.status_code == 403
    operations_deliveries = operations.get("/api/staff/deliveries").get_json()["deliveries"]
    assert all(item["id"] != delivery_id for item in operations_deliveries)

    sent = []
    app.extensions["delivery"].smtp_factory = FakeSMTP(sent)
    accountant = app.test_client()
    accountant_token = _login(accountant, "accountant")
    accepted = accountant.post(
        f"/api/staff/enrollments/{enrollment_id}/invoice-email/retry",
        json={"delivery_id": delivery_id},
        headers={"X-CSRF-Token": accountant_token},
    )
    assert accepted.status_code == 200
    assert accepted.get_json()["invoice_email"]["status"] == "sent"
    assert len(sent) == 1
    accountant_deliveries = accountant.get("/api/staff/deliveries").get_json()["deliveries"]
    invoice_delivery = next(item for item in accountant_deliveries if item["id"] == delivery_id)
    assert invoice_delivery["recipient_masked"] == "t***@example.com"
    assert "test.student@example.com" not in str(invoice_delivery)


def test_invalid_or_unsafe_recipients_and_ineligible_records_are_excluded(tmp_path, fake_ai):
    app = invoice_app(tmp_path, fake_ai)
    configure_email(app, [])
    enrollment_id = confirmed_enrollment(app)
    generation_id = generate_invoice(app, enrollment_id)
    with app.app_context():
        service = app.extensions["invoice_email"]
        draft = db.session.get(EnrollmentDraft, enrollment_id)
        generation = db.session.get(InvoiceGeneration, generation_id)
        draft.email = "victim@example.com\r\nBcc: attacker@example.com"
        db.session.commit()
        with pytest.raises(InvoiceEmailError, match="email address is invalid"):
            service.queue_automatic(draft, generation)
        db.session.rollback()
        draft.email = "test.student@example.com"
        draft.status = "cancelled"
        db.session.commit()
        with pytest.raises(InvoiceEmailError, match="not eligible"):
            service.queue_automatic(draft, generation)
        assert OutboundMessage.query.count() == 0


def test_live_test_command_is_opt_in_and_sends_one_fictional_message(tmp_path, fake_ai):
    sent = []
    app = invoice_app(tmp_path, fake_ai)
    configure_email(app, sent)
    runner = app.test_cli_runner()
    refused = runner.invoke(args=["send-test-invoice-email"])
    assert refused.exit_code != 0
    assert sent == []

    app.config.update(
        EMAIL_LIVE_TEST_ENABLED=True,
        EMAIL_TEST_RECIPIENT="controlled-test@example.com",
    )
    delivered = runner.invoke(args=["send-test-invoice-email"])
    assert delivered.exit_code == 0
    assert len(sent) == 1
    assert sent[0]["To"] == "controlled-test@example.com"
    assert "Development demonstration only" in sent[0].get_body(
        preferencelist=("plain",)
    ).get_content()
    assert "fictional-app-password" not in delivered.output
    with app.app_context():
        assert EnrollmentDraft.query.count() == 0
