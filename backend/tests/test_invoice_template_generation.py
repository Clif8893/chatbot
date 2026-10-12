"""Template invoice generation is deterministic, private and model-free."""

import hashlib
import threading
import time
from datetime import date
from decimal import Decimal
from email.message import EmailMessage
from io import BytesIO
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from werkzeug.security import generate_password_hash

from backend.app import create_app
from backend.extensions import db
from backend.models import Conversation, EnrollmentDraft, User, utc_now
from backend.models.communications import OutboundMessage
from backend.models.finance import FinanceAudit, InvoiceGeneration, MasterInvoiceExport
from backend.services.invoice_template_service import ExcelUnavailableError
from backend.tests.conftest import authenticated_client, send


def _test_template_eml(tmp_path):
    """Create a synthetic attachment so tests never require the private company email."""
    destination = tmp_path / "invoice-template.eml"
    if destination.exists():
        return destination
    workbook = Workbook()
    workbook.active.title = "Invoice"
    stream = BytesIO()
    workbook.save(stream)
    message = EmailMessage()
    message["Subject"] = "Synthetic invoice template for automated tests"
    message.set_content("This message contains synthetic test data only.")
    message.add_attachment(
        stream.getvalue(),
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="College & CN Template.xlsx",
    )
    destination.write_bytes(message.as_bytes())
    return destination


class FakePdfConverter:
    def __init__(self, *, failure=None, delay=0):
        self.failure = failure
        self.delay = delay
        self.calls = []
        self._guard = threading.Lock()

    def convert(self, workbook_path, pdf_path, sheet_name="Invoice"):
        if self.failure:
            raise self.failure
        workbook = load_workbook(workbook_path, data_only=False)
        sheet = workbook[sheet_name]
        snapshot = {
            cell: sheet[cell].value
            for cell in ("C14", "I14", "C7", "I7", "C22", "H30", "C26", "H36", "H31", "H33")
        }
        snapshot["_sheetnames"] = tuple(workbook.sheetnames)
        snapshot["_image_count"] = len(sheet._images)
        snapshot["_merged_ranges"] = tuple(str(item) for item in sheet.merged_cells.ranges)
        snapshot["_print_area"] = str(sheet.print_area)
        with self._guard:
            self.calls.append(snapshot)
        if self.delay:
            time.sleep(self.delay)
        pdf = canvas.Canvas(str(pdf_path), pagesize=A4)
        pdf.drawString(72, 760, f"Invoice {snapshot['C7']}")
        pdf.drawString(72, 740, f"Participant {snapshot['C14']}")
        pdf.drawString(72, 720, f"NRIC {snapshot['I14']}")
        pdf.drawString(72, 700, f"Course {snapshot['C22']}")
        pdf.save()


def invoice_app(tmp_path, fake_ai, *, approved=True):
    source_eml = _test_template_eml(tmp_path)
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "invoice-template-test-secret",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / 'invoice.db').as_posix()}",
            "STORAGE_DIR": str(tmp_path / "private"),
            "MASTER_INVOICE_PATH": str(tmp_path / "private" / "Master_Invoice_List.xlsx"),
            "INVOICE_GENERATION_ENABLED": True,
            "INVOICE_TEMPLATE_APPROVED": approved,
            "INVOICE_TEMPLATE_SOURCE_PATH": str(source_eml),
            "ENABLE_DEMO_INTAKES": True,
            "ENROLLMENT_CURRENT_DATE": date(2026, 9, 22),
            "STAFF_ACCOUNTS": {
                role: {
                    "password_hash": generate_password_hash(f"{role}-password"),
                    "role": role,
                }
                for role in ("admin", "accountant", "operations")
            },
        },
        ai_service=fake_ai,
    )
    return app


def confirmed_enrollment(
    app,
    *,
    skillsfuture="400.00",
    paynow="200.00",
    full_name="Test Student",
    status="confirmed",
    course_date=date(2027, 2, 8),
):
    with app.app_context():
        conversation = Conversation()
        db.session.add(conversation)
        db.session.flush()
        draft = EnrollmentDraft(
            conversation_id=conversation.id,
            course="Basic Certificate in Dental Assisting",
            course_fee=Decimal("600.00"),
            full_name=full_name,
            nric="S1234567D",
            date_of_birth=date(2000, 5, 15),
            email="test.student@example.com",
            mobile_number="91234567",
            payment_method=(
                "skillsfuture_paynow"
                if Decimal(skillsfuture) and Decimal(paynow)
                else "skillsfuture"
                if Decimal(skillsfuture)
                else "paynow"
            ),
            skillsfuture_amount=Decimal(skillsfuture),
            paynow_amount=Decimal(paynow),
            preferred_intake_date=course_date,
            intake_start_date=course_date,
            intake_end_date=date(2027, 2, 9) if course_date else None,
            status=status,
            validation_status="valid",
            confirmed_at=utc_now() if status == "confirmed" else None,
        )
        db.session.add(draft)
        db.session.flush()
        export = MasterInvoiceExport(
            enrollment_id=draft.id,
            export_key=draft.id,
            invoice_number="DEMO-INV-2027-000001",
            invoice_sequence=1,
            invoice_date=date(2026, 9, 22),
            status="exported",
            export_version=1,
        )
        db.session.add(export)
        db.session.commit()
        return draft.id


@pytest.mark.parametrize(
    ("skillsfuture", "paynow"),
    [("0.00", "600.00"), ("600.00", "0.00"), ("400.00", "200.00")],
)
def test_generates_official_template_pdf_for_full_course_fee_without_payment_split(
    tmp_path, fake_ai, skillsfuture, paynow
):
    source_eml = _test_template_eml(tmp_path)
    source_hash = hashlib.sha256(source_eml.read_bytes()).hexdigest()
    app = invoice_app(tmp_path, fake_ai)
    enrollment_id = confirmed_enrollment(
        app, skillsfuture=skillsfuture, paynow=paynow
    )
    converter = FakePdfConverter()
    app.extensions["invoice_template"].converter = converter

    with app.app_context():
        service = app.extensions["invoice_template"]
        record = service.generate(enrollment_id)
        assert record.status == "generated"
        assert record.invoice_number == "DEMO-INV-2027-000001"
        assert record.version == 1
        assert record.attempt_count == 1
        assert record.storage_key == "invoices/DEMO-INV-2027-000001.pdf"
        assert (Path(app.config["STORAGE_DIR"]) / record.storage_key).is_file()
        assert OutboundMessage.query.count() == 0
        assert InvoiceGeneration.query.count() == 1

        values = converter.calls[0]
        assert values["C14"] == "Test Student"
        assert values["I14"] == "S*******D"
        assert "S1234567D" not in str(values)
        assert values["C7"] == "DEMO-INV-2027-000001"
        assert values["I7"].date() == date(2026, 9, 22)
        assert values["C22"] == "Basic Certificate in Dental Assisting"
        assert values["C26"] == "8–9 February 2027"
        assert values["H30"] == 600.0
        assert values["H31"] is None
        assert values["H33"] is None
        assert values["H36"] == "=H30"
        assert values["_sheetnames"] == ("Invoice",)
        assert values["_image_count"] == 1
        assert "A5:K5" in values["_merged_ranges"]
        assert values["_print_area"] == "'Invoice'!$A$1:$K$43"
        assert not fake_ai.classify_calls
        assert not fake_ai.faq_calls
        assert not fake_ai.enrollment_calls
        assert hashlib.sha256(source_eml.read_bytes()).hexdigest() == source_hash
        assert not list((tmp_path / "private").glob("qm-invoice-*"))
        assert not list((tmp_path / "private" / "invoices").glob("*.tmp"))


def test_idempotency_regeneration_and_failed_regeneration_preserve_valid_pdf(
    tmp_path, fake_ai
):
    app = invoice_app(tmp_path, fake_ai)
    enrollment_id = confirmed_enrollment(app)
    converter = FakePdfConverter()
    app.extensions["invoice_template"].converter = converter

    with app.app_context():
        service = app.extensions["invoice_template"]
        first = service.generate(enrollment_id)
        original = service.document_bytes(first)
        same = service.generate(enrollment_id)
        assert same.id == first.id
        assert same.version == 1
        assert len(converter.calls) == 1

        draft = db.session.get(EnrollmentDraft, enrollment_id)
        draft.full_name = "Corrected Student"
        db.session.commit()
        regenerated = service.generate(enrollment_id, force=True)
        assert regenerated.id == first.id
        assert regenerated.version == 2
        assert len(converter.calls) == 2
        assert converter.calls[1]["C14"] == "Corrected Student"
        latest_valid = service.document_bytes(regenerated)

        service.converter = FakePdfConverter(failure=RuntimeError("synthetic"))
        failed = service.generate(enrollment_id, force=True)
        assert failed.status == "failed"
        assert failed.version == 2
        assert service.document_bytes(failed) == latest_valid
        assert "previous valid PDF was preserved" in failed.failure_reason


@pytest.mark.parametrize(
    ("change", "expected_status"),
    [
        ({"preferred_intake_date": None, "intake_start_date": None}, "needs_review"),
        ({"status": "cancelled"}, "needs_review"),
        ({"email": None}, "needs_review"),
    ],
)
def test_awaiting_date_cancelled_and_incomplete_enrollments_are_excluded(
    tmp_path, fake_ai, change, expected_status
):
    app = invoice_app(tmp_path, fake_ai)
    enrollment_id = confirmed_enrollment(app)
    converter = FakePdfConverter()
    app.extensions["invoice_template"].converter = converter
    with app.app_context():
        draft = db.session.get(EnrollmentDraft, enrollment_id)
        for key, value in change.items():
            setattr(draft, key, value)
        db.session.commit()
        record = app.extensions["invoice_template"].generate(draft)
        assert record.status == expected_status
        assert not converter.calls


def test_unapproved_template_is_needs_review_and_does_not_create_pdf(tmp_path, fake_ai):
    app = invoice_app(tmp_path, fake_ai, approved=False)
    enrollment_id = confirmed_enrollment(app)
    converter = FakePdfConverter()
    app.extensions["invoice_template"].converter = converter
    with app.app_context():
        record = app.extensions["invoice_template"].generate(enrollment_id)
        assert record.status == "needs_review"
        assert "Q&M approval" in record.failure_reason
        assert record.storage_key is None
        assert not converter.calls


@pytest.mark.parametrize(
    ("source_exists", "failure", "message"),
    [
        (False, None, "template is unavailable"),
        (True, ExcelUnavailableError(), "Microsoft Excel is unavailable"),
        (True, RuntimeError("synthetic"), "could not be generated"),
    ],
)
def test_missing_template_excel_unavailable_and_conversion_failure_are_retryable(
    tmp_path, fake_ai, source_exists, failure, message
):
    app = invoice_app(tmp_path, fake_ai)
    enrollment_id = confirmed_enrollment(app)
    service = app.extensions["invoice_template"]
    if not source_exists:
        service.source_path = tmp_path / "missing.xlsx"
    else:
        service.converter = FakePdfConverter(failure=failure)
    with app.app_context():
        record = service.generate(enrollment_id)
        assert record.status == "failed"
        assert message in record.failure_reason
        assert record.attempt_count == 1


def test_formula_injection_is_written_as_text(tmp_path, fake_ai):
    app = invoice_app(tmp_path, fake_ai)
    enrollment_id = confirmed_enrollment(app, full_name="=HYPERLINK Test")
    converter = FakePdfConverter()
    app.extensions["invoice_template"].converter = converter
    with app.app_context():
        record = app.extensions["invoice_template"].generate(enrollment_id)
        assert record.status == "generated"
        assert converter.calls[0]["C14"] == "'=HYPERLINK Test"


def _login(client, role):
    response = client.post(
        "/api/staff/login",
        json={"username": role, "password": f"{role}-password"},
    )
    assert response.status_code == 200
    return response.get_json()["csrf_token"]


def test_only_accountant_and_admin_can_generate_download_and_audit(tmp_path, fake_ai):
    app = invoice_app(tmp_path, fake_ai)
    enrollment_id = confirmed_enrollment(app)
    app.extensions["invoice_template"].converter = FakePdfConverter()

    operations = app.test_client()
    token = _login(operations, "operations")
    denied = operations.post(
        f"/api/staff/enrollments/{enrollment_id}/invoice",
        headers={"X-CSRF-Token": token},
    )
    assert denied.status_code == 403

    accountant = app.test_client()
    token = _login(accountant, "accountant")
    generated = accountant.post(
        f"/api/staff/enrollments/{enrollment_id}/invoice",
        headers={"X-CSRF-Token": token},
    )
    assert generated.status_code == 200
    document = generated.get_json()["invoice_document"]
    downloaded = accountant.get(document["download_url"])
    assert downloaded.status_code == 200
    assert downloaded.data.startswith(b"%PDF-")

    admin = app.test_client()
    _login(admin, "admin")
    assert admin.get(document["download_url"]).status_code == 200
    assert app.test_client().get(document["download_url"]).status_code == 401
    assert app.test_client().get(
        f"/api/conversations/example/documents/{document['id']}"
    ).status_code == 401
    with app.app_context():
        actions = {row.action for row in FinanceAudit.query.all()}
        assert "template_invoice_generated" in actions
        assert "template_invoice_downloaded" in actions


def test_generation_state_survives_application_restart(tmp_path, fake_ai):
    app = invoice_app(tmp_path, fake_ai)
    enrollment_id = confirmed_enrollment(app)
    app.extensions["invoice_template"].converter = FakePdfConverter()
    with app.app_context():
        first = app.extensions["invoice_template"].generate(enrollment_id)
        record_id = first.id

    restarted = invoice_app(tmp_path, fake_ai)
    restarted.extensions["invoice_template"].converter = FakePdfConverter()
    with restarted.app_context():
        service = restarted.extensions["invoice_template"]
        record = service.get(record_id)
        assert record.status == "generated"
        assert record.version == 1
        assert service.document_bytes(record).startswith(b"%PDF-")
        assert not restarted.extensions["invoice_template"].converter.calls


def test_confirmation_automatically_generates_after_master_invoice_assignment(
    tmp_path, fake_ai
):
    app = invoice_app(tmp_path, fake_ai)
    converter = FakePdfConverter()
    app.extensions["invoice_template"].converter = converter
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "1")
    ready = send(
        client,
        conversation_id,
        "Full name: Test Student; NRIC/FIN: S1234567D; "
        "Date of birth: 15 May 2000; Email: test.student@example.com; "
        "Mobile number: 91234567; SkillsFuture S$400; PayNow S$200",
    ).get_json()
    assert ready["enrollment"]["status"] == "awaiting_confirmation"
    confirmed = send(client, conversation_id, "Confirm").get_json()
    assert confirmed["enrollment"]["status"] == "confirmed"
    with app.app_context():
        export = MasterInvoiceExport.query.one()
        document = InvoiceGeneration.query.one()
        assert export.invoice_number == document.invoice_number
        assert document.status == "generated"
        assert len(converter.calls) == 1
        assert OutboundMessage.query.count() == 0


def test_concurrent_requests_create_one_active_document(tmp_path, fake_ai):
    app = invoice_app(tmp_path, fake_ai)
    enrollment_id = confirmed_enrollment(app)
    converter = FakePdfConverter(delay=0.1)
    app.extensions["invoice_template"].converter = converter
    errors = []

    def run():
        try:
            with app.app_context():
                app.extensions["invoice_template"].generate(enrollment_id)
        except Exception as error:  # pragma: no cover - diagnostic capture
            errors.append(error)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    with app.app_context():
        assert InvoiceGeneration.query.count() == 1
        record = InvoiceGeneration.query.one()
        assert record.invoice_number == "DEMO-INV-2027-000001"
        assert record.version == 1
        assert len(converter.calls) == 1


def test_participant_owner_download_status_natural_request_and_idempotency(
    tmp_path, fake_ai
):
    app = invoice_app(tmp_path, fake_ai)
    converter = FakePdfConverter()
    app.extensions["invoice_template"].converter = converter
    owner = authenticated_client(app, email="invoice-owner@example.com")
    conversation_id = owner.post("/api/conversations").get_json()["conversation_id"]
    send(owner, conversation_id, "I want to enroll")
    send(owner, conversation_id, "1")
    send(owner, conversation_id, "1")
    send(
        owner,
        conversation_id,
        "Full name: Test Student; NRIC/FIN: S1234567D; "
        "Date of birth: 15 May 2000; Email: test.student@example.com; "
        "Mobile number: 91234567; SkillsFuture S$400; PayNow S$200",
    )
    confirmed = send(owner, conversation_id, "Confirm").get_json()
    assert confirmed["enrollment"]["status"] == "confirmed"
    assert confirmed["show_invoice_action"] is True

    with app.app_context():
        enrollment_id = EnrollmentDraft.query.one().id
        record = InvoiceGeneration.query.one()
        assert record.status == "generated"
        invoice_number = record.invoice_number

    status = owner.get(f"/api/conversations/{conversation_id}/status").get_json()
    document = status["finance"]["invoice_document"]
    assert document["status"] == "generated"
    assert document["invoice_number"] == invoice_number
    assert document["download_url"] == (
        f"/api/participant/enrollments/{enrollment_id}/invoice"
    )
    assert "/api/staff/" not in str(document)

    calls_before = len(fake_ai.classify_calls) + len(fake_ai.faq_calls) + len(fake_ai.enrollment_calls)
    request_reply = send(owner, conversation_id, "Can I get my invoice?").get_json()
    calls_after = len(fake_ai.classify_calls) + len(fake_ai.faq_calls) + len(fake_ai.enrollment_calls)
    assert request_reply["show_invoice_action"] is True
    assert "invoice is ready" in request_reply["message"]["content"].casefold()
    assert calls_after == calls_before

    first = owner.get(document["download_url"])
    second = owner.get(document["download_url"])
    assert first.status_code == second.status_code == 200
    assert first.mimetype == "application/pdf"
    assert "attachment" in first.headers["Content-Disposition"].casefold()
    assert first.data.startswith(b"%PDF-")
    assert len(converter.calls) == 1

    other = authenticated_client(app, email="invoice-other@example.com")
    assert other.get(document["download_url"]).status_code == 404
    assert app.test_client().get(document["download_url"]).status_code == 401
    with app.app_context():
        downloads = FinanceAudit.query.filter_by(
            action="template_invoice_downloaded", actor="participant"
        ).count()
        assert downloads == 2


def test_failed_automatic_pdf_generation_keeps_enrollment_confirmed(
    tmp_path, fake_ai
):
    app = invoice_app(tmp_path, fake_ai)
    app.extensions["invoice_template"].converter = FakePdfConverter(
        failure=RuntimeError("synthetic")
    )
    owner = authenticated_client(app, email="failed-invoice@example.com")
    conversation_id = owner.post("/api/conversations").get_json()["conversation_id"]
    send(owner, conversation_id, "I want to enroll")
    send(owner, conversation_id, "1")
    send(owner, conversation_id, "1")
    send(
        owner,
        conversation_id,
        "Full name: Test Student; NRIC/FIN: S1234567D; "
        "Date of birth: 15 May 2000; Email: test.student@example.com; "
        "Mobile number: 91234567; PayNow S$600",
    )
    confirmed = send(owner, conversation_id, "Confirm").get_json()
    assert confirmed["enrollment"]["status"] == "confirmed"
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        record = InvoiceGeneration.query.one()
        assert draft.status == "confirmed"
        assert draft.confirmed_at is not None
        assert record.status == "failed"
        enrollment_id = draft.id
    failed = owner.get(f"/api/participant/enrollments/{enrollment_id}/invoice")
    assert failed.status_code == 503
    assert "backend" not in str(failed.get_json()).casefold()


def test_participant_invoice_endpoint_returns_safe_non_ready_states(tmp_path, fake_ai):
    app = invoice_app(tmp_path, fake_ai)
    owner = authenticated_client(app, email="invoice-states@example.com")
    enrollment_id = confirmed_enrollment(app)
    app.extensions["invoice_template"].converter = FakePdfConverter()
    with app.app_context():
        draft = db.session.get(EnrollmentDraft, enrollment_id)
        draft.conversation.user_id = User.query.filter_by(
            email="invoice-states@example.com"
        ).one().id
        record = app.extensions["invoice_template"].generate(draft)
        assert record.status == "generated"

    path = f"/api/participant/enrollments/{enrollment_id}/invoice"
    expected = (("pending", 202), ("needs_review", 409), ("failed", 503))
    for state, status_code in expected:
        with app.app_context():
            record = InvoiceGeneration.query.one()
            record.status = state
            record.failure_reason = "Safe test state"
            db.session.commit()
        response = owner.get(path)
        assert response.status_code == status_code
        assert "storage_key" not in str(response.get_json())

    with app.app_context():
        draft = db.session.get(EnrollmentDraft, enrollment_id)
        draft.status = "cancelled"
        db.session.commit()
    assert owner.get(path).status_code == 409
