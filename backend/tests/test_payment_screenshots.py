"""Payment screenshots remain evidence until staff reconciles them."""

import io
from datetime import date, timedelta

import pytest
from openpyxl import load_workbook
from PIL import Image
from sqlalchemy import text
from werkzeug.security import generate_password_hash

from backend.extensions import db
from backend.models import Conversation, EnrollmentDraft, Message
from backend.models.communications import OutboundMessage
from backend.models.finance import Invoice, InvoiceGeneration, Payment, PaymentReview
from backend.services.payment_vision import PaymentExtraction, PaymentVision
from backend.services.database_migrations import apply_additive_migrations, backup_sqlite_before_schema_sync


def screenshot(color):
    image = Image.new("RGB", (80, 80), color)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def setup_invoice(app, client, fee="600.00"):
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    with app.app_context():
        draft = EnrollmentDraft(conversation_id=conversation_id, course="Certificate in Dental Clinic Front Desk Operations", full_name="Test Participant", email="test@example.com", mobile_number="81234567", preferred_intake_date=date.today() + timedelta(days=30), status="confirmed", course_fee=fee)
        db.session.add(draft)
        db.session.flush()
        invoice = Invoice(enrollment_id=draft.id, number=f"TEST-{draft.id}", course_name=draft.course, participant_name=draft.full_name, course_date=draft.preferred_intake_date, course_fee=fee, skillsfuture_amount="0.00", net_payable=fee, recipient_uen="TESTUEN", due_date=date.today() + timedelta(days=14))
        db.session.add(invoice)
        db.session.commit()
        return conversation_id, draft.id


class FakeVision:
    def __init__(self, amount="200.00", reference="TXN-ONE", claim=None):
        self.amount = amount
        self.reference = reference
        self.claim = claim

    def extract(self, image, mime):
        return PaymentExtraction(amount=self.amount, currency="SGD", recipient="TESTUEN", reference=self.reference, transaction_date=date.today().isoformat(), provider="DBS", screenshot_type="SkillsFuture claim" if self.claim else "PayNow success", skillsfuture_claim_amount=self.claim, skillsfuture_claim_status="submitted" if self.claim else None, confidence=.9, readable=True, successful=not bool(self.claim))


def upload(client, conversation_id, image, submission_id, payment_type="paynow"):
    return client.post(f"/api/conversations/{conversation_id}/evidence", data={"file": (io.BytesIO(image), "payment.png"), "payment_type": payment_type, "submission_id": submission_id}, content_type="multipart/form-data")


def test_partial_approvals_and_duplicate_retry(app, client):
    conversation_id, draft_id = setup_invoice(app, client)
    with app.app_context():
        app.extensions["finance"].vision = FakeVision()
    first = upload(client, conversation_id, screenshot("red"), "submission-one")
    assert first.status_code == 201
    assert first.get_json()["verdict"] == "pending_review"
    assert any(message["image_url"].endswith(first.get_json()["evidence"]["id"]) for message in first.get_json()["messages"] if message.get("image_url"))
    assert upload(client, conversation_id, screenshot("red"), "submission-one").get_json()["evidence"]["id"] == first.get_json()["evidence"]["id"]
    with app.app_context():
        finance = app.extensions["finance"]
        draft = db.session.get(EnrollmentDraft, draft_id)
        assert finance.serialize_enrollment(draft)["outstanding_amount"] == "600.00"
        review = db.session.get(PaymentReview, first.get_json()["review"]["id"])
        finance.decide_payment_review(review.id, "approve", "accounts", "Matched bank statement")
        db.session.commit()
        assert finance.serialize_enrollment(draft)["outstanding_amount"] == "400.00"
        with pytest.raises(ValueError):
            finance.decide_payment_review(review.id, "approve", "accounts", "retry")
        assert db.session.query(Payment).count() == 1
        assert db.session.query(OutboundMessage).filter_by(purpose="payment_decision").count() == 1
        assert "Remaining balance: S$400.00" in db.session.query(Message).filter_by(conversation_id=conversation_id, agent_name="payment").order_by(Message.created_at.desc()).first().content
        workbook = load_workbook(io.BytesIO(finance.payment_report_xlsx()))
        assert workbook.active.cell(2, 11).value == "200.00"
        assert workbook["Verified payments"].cell(2, 6).value == "200.00"

    with app.app_context():
        app.extensions["finance"].vision = FakeVision("400.00", "TXN-TWO")
    second = upload(client, conversation_id, screenshot("blue"), "submission-two")
    assert second.status_code == 201
    with app.app_context():
        finance = app.extensions["finance"]
        finance.decide_payment_review(second.get_json()["review"]["id"], "approve", "accounts", "Matched second bank statement")
        db.session.commit()
        assert finance.serialize_enrollment(db.session.get(EnrollmentDraft, draft_id))["outstanding_amount"] == "0.00"
        assert db.session.query(Payment).count() == 2
        assert db.session.query(OutboundMessage).filter_by(purpose="payment_decision").count() == 2


def test_duplicate_screenshot_and_claim_do_not_reduce_balance(app, client):
    conversation_id, draft_id = setup_invoice(app, client)
    with app.app_context():
        app.extensions["finance"].vision = FakeVision(claim="200.00")
    image = screenshot("green")
    first = upload(client, conversation_id, image, "claim-one", "skillsfuture")
    assert "skillsfuture_claim_requires_accounts_confirmation" in first.get_json()["review"]["reasons"]
    second = upload(client, conversation_id, image, "claim-two")
    assert "duplicate_screenshot" in second.get_json()["review"]["reasons"]
    with app.app_context():
        finance = app.extensions["finance"]
        assert finance.serialize_enrollment(db.session.get(EnrollmentDraft, draft_id))["outstanding_amount"] == "600.00"
        with pytest.raises(ValueError):
            finance.decide_payment_review(second.get_json()["review"]["id"], "approve", "accounts", "duplicate")


def test_screenshot_is_scoped_to_conversation(app, client):
    conversation_id, _ = setup_invoice(app, client)
    with app.app_context():
        app.extensions["finance"].vision = FakeVision()
    result = upload(client, conversation_id, screenshot("yellow"), "scoped-one").get_json()
    url = f"/api/conversations/{conversation_id}/evidence/{result['evidence']['id']}"
    assert client.get(url).status_code == 200
    other_client = app.test_client()
    assert other_client.get(url).status_code in {401, 403, 404}
    assert client.get(f"/api/conversations/not-this-chat/evidence/{result['evidence']['id']}").status_code == 404
    assert client.get(f"/api/staff/evidence/{result['evidence']['id']}").status_code in {401, 403}


def test_staff_review_and_excel_are_role_scoped(app, client):
    conversation_id, _ = setup_invoice(app, client)
    with app.app_context():
        app.extensions["finance"].vision = FakeVision()
    result = upload(client, conversation_id, screenshot("purple"), "staff-one").get_json()
    app.config["STAFF_ACCOUNTS"] = {
        "viewer": {"password_hash": generate_password_hash("viewer-secret"), "role": "operations"},
        "accounts": {"password_hash": generate_password_hash("accounts-secret"), "role": "accountant"},
    }
    viewer = app.test_client()
    assert viewer.post("/api/staff/login", json={"username": "viewer", "password": "viewer-secret"}).status_code == 200
    assert viewer.get("/api/staff/payment-reviews").status_code == 403
    assert viewer.get("/api/staff/reports/payments.xlsx").status_code == 403
    accountant = app.test_client()
    auth = accountant.post("/api/staff/login", json={"username": "accounts", "password": "accounts-secret"}).get_json()
    assert accountant.get("/api/staff/payment-reviews").get_json()["reviews"][0]["evidence_id"] == result["evidence"]["id"]
    assert accountant.get("/api/staff/reports/payments.xlsx").status_code == 200
    assert accountant.get(f"/api/staff/evidence/{result['evidence']['id']}").status_code == 200
    response = accountant.post(f"/api/staff/payment-reviews/{result['review']['id']}/decision", json={"decision": "clarify", "note": "Need bank reference"}, headers={"X-CSRF-Token": auth["csrf_token"]})
    assert response.status_code == 200
    assert response.get_json()["review"]["status"] == "clarification_requested"
    assert accountant.post(f"/api/staff/payment-reviews/{result['review']['id']}/decision", json={"decision": "approve", "note": "retry"}, headers={"X-CSRF-Token": auth["csrf_token"]}).status_code == 400


def test_openai_vision_missing_fields_are_not_invented():
    class Response:
        output_text = '{"amount":"10.00","currency":"SGD","recipient":"JOSXX DHIIXX","reference":null,"transaction_date":"2026-10-22","provider":"DBS","screenshot_type":"PayNow success","readable":true,"successful":true,"confidence":0.7}'

    class Client:
        class responses:
            @staticmethod
            def create(**kwargs):
                assert kwargs["input"][0]["content"][1]["type"] == "input_image"
                return Response()

    facts = PaymentVision(client=Client()).extract(b"image", "image/png")
    assert facts.amount == "10.00"
    assert facts.reference is None
    assert facts.skillsfuture_claim_amount is None


@pytest.mark.parametrize("payload,expected_claim,expected_error", [
    ('{"amount":null,"currency":null,"recipient":null,"reference":null,"transaction_date":null,"provider":null,"screenshot_type":"SkillsFuture claim","skillsfuture_claim_amount":"200.00","skillsfuture_claim_status":"submitted","readable":true,"successful":false}', "200.00", None),
    ("not json", None, "vision_extraction_failed"),
])
def test_vision_claim_and_unreadable_result(payload, expected_claim, expected_error):
    class Response:
        output_text = payload

    class Client:
        class responses:
            @staticmethod
            def create(**kwargs):
                return Response()

    facts = PaymentVision(client=Client()).extract(b"image", "image/png")
    assert facts.skillsfuture_claim_amount == expected_claim
    assert facts.error == expected_error
    assert not facts.successful


def test_legacy_payment_constraint_migrates_for_partials(app, client):
    _, draft_id = setup_invoice(app, client)
    with app.app_context():
        db.session.remove()
        with db.engine.begin() as connection:
            connection.execute(text("DROP TABLE payments"))
            connection.execute(text("CREATE TABLE payments (id VARCHAR(36) PRIMARY KEY, enrollment_id VARCHAR(36) NOT NULL, invoice_id VARCHAR(36) NOT NULL, evidence_id VARCHAR(36), amount NUMERIC(10,2) NOT NULL, method VARCHAR(24) NOT NULL, reference VARCHAR(255) NOT NULL UNIQUE, transaction_date DATE NOT NULL, confirmed_by VARCHAR(255) NOT NULL, created_at DATETIME NOT NULL, CONSTRAINT uq_invoice_payment_method UNIQUE(invoice_id, method))"))
        assert backup_sqlite_before_schema_sync() is not None
        apply_additive_migrations()
        invoice = db.session.query(Invoice).filter_by(enrollment_id=draft_id).one()
        db.session.add_all([
            Payment(enrollment_id=draft_id, invoice_id=invoice.id, amount="100.00", method="paynow", reference="MIGRATE-ONE", transaction_date=date.today(), confirmed_by="accounts"),
            Payment(enrollment_id=draft_id, invoice_id=invoice.id, amount="200.00", method="paynow", reference="MIGRATE-TWO", transaction_date=date.today(), confirmed_by="accounts"),
        ])
        db.session.commit()
        assert db.session.query(Payment).count() == 2


def test_upload_works_with_existing_not_null_review_corrections(app, client):
    conversation_id, _ = setup_invoice(app, client)
    with app.app_context():
        db.session.remove()
        with db.engine.begin() as connection:
            connection.execute(text("DROP TABLE payment_reviews"))
            connection.execute(text("CREATE TABLE payment_reviews (id VARCHAR(36) PRIMARY KEY, enrollment_id VARCHAR(36) NOT NULL, evidence_id VARCHAR(36) NOT NULL UNIQUE, reasons_json TEXT NOT NULL, status VARCHAR(24) NOT NULL, note TEXT, decided_by VARCHAR(255), decided_at DATETIME, created_at DATETIME NOT NULL, corrections_json TEXT NOT NULL DEFAULT '{}')"))
        app.extensions["finance"].vision = FakeVision()
    response = upload(client, conversation_id, screenshot("pink"), "legacy-corrections")
    assert response.status_code == 201
    assert response.get_json()["review"]["status"] == "pending_review"


def test_planned_skillsfuture_allocation_is_not_verified_payment(app, client):
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    with app.app_context():
        draft = EnrollmentDraft(conversation_id=conversation_id, course="Basic Certificate in Dental Assisting", full_name="Test Participant", email="test@example.com", mobile_number="81234567", preferred_intake_date=date.today() + timedelta(days=30), status="confirmed", course_fee="600.00", skillsfuture_amount="200.00")
        db.session.add(draft)
        db.session.flush()
        invoice = app.extensions["finance"].create_invoice(draft)
        db.session.commit()
        assert str(invoice.skillsfuture_amount) == "200.00"
        assert str(invoice.net_payable) == "400.00"
        assert app.extensions["finance"].serialize_enrollment(draft)["outstanding_amount"] == "600.00"


def test_front_desk_screenshot_links_generated_invoice_without_guessed_payee(app, client):
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    with app.app_context():
        draft = EnrollmentDraft(conversation_id=conversation_id, course="Certificate in Dental Clinic Front Desk Operations", full_name="Test Participant", email="test@example.com", mobile_number="81234567", preferred_intake_date=date.today() + timedelta(days=30), status="confirmed", course_fee="450.00")
        db.session.add(draft)
        db.session.flush()
        db.session.add(InvoiceGeneration(enrollment_id=draft.id, invoice_number=f"DEMO-INV-{draft.id[:8]}", status="generated", storage_key="generated-invoice.pdf"))
        db.session.commit()
        app.extensions["finance"].vision = FakeVision("10.00", "FRONT-DESK-ONE")
    result = upload(client, conversation_id, screenshot("orange"), "front-desk-upload")
    assert result.status_code == 201
    assert "payee_not_configured" in result.get_json()["review"]["reasons"]
    with app.app_context():
        evidence = result.get_json()["evidence"]
        invoice = db.session.get(Invoice, evidence["invoice_id"])
        assert invoice.number.startswith("DEMO-INV-")
        assert invoice.recipient_uen == "STAFF_CONFIRMATION_REQUIRED"


def test_missing_reference_stays_pending_until_bank_reconciliation(app, client):
    conversation_id, draft_id = setup_invoice(app, client)
    with app.app_context():
        app.extensions["finance"].vision = FakeVision("10.00", None)
    result = upload(client, conversation_id, screenshot("cyan"), "missing-ref").get_json()
    assert result["verdict"] == "pending_review"
    assert "reference_missing" in result["review"]["reasons"]
    with app.app_context():
        finance = app.extensions["finance"]
        assert finance.serialize_enrollment(db.session.get(EnrollmentDraft, draft_id))["outstanding_amount"] == "600.00"
        finance.decide_payment_review(result["review"]["id"], "approve", "accounts", "Matched incoming DBS credit", reference="BANK-VERIFIED-10")
        db.session.commit()
        assert finance.serialize_enrollment(db.session.get(EnrollmentDraft, draft_id))["outstanding_amount"] == "590.00"
