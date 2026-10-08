"""Regression coverage for controlled cleanup and configurable courses."""

from datetime import date
from decimal import Decimal

import pytest

from backend.app import create_app
from backend.extensions import db
from backend.models import Conversation, EnrollmentDraft, User, utc_now
from backend.models.communications import OutboundMessage
from backend.models.operations import CourseDate
from backend.services.maintenance_service import CONFIRMATION_PHRASE
from backend.tests.conftest import authenticated_client, send


def _app(tmp_path, fake_ai, **overrides):
    config = {
        "TESTING": True,
        "SECRET_KEY": "controlled-cleanup-test",
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        "STORAGE_DIR": str(tmp_path / "private"),
        "MASTER_INVOICE_PATH": str(tmp_path / "private" / "Master_Invoice_List.xlsx"),
        "ENABLE_DEMO_INTAKES": True,
        "ENROLLMENT_CURRENT_DATE": date(2026, 9, 24),
        "INVOICE_GENERATION_ENABLED": False,
    }
    config.update(overrides)
    return create_app(config, ai_service=fake_ai)


def test_course_choice_and_infection_control_rules(tmp_path, fake_ai):
    app = _app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]

    opening = send(client, conversation_id, "I want to enrol").get_json()["message"]["content"]
    assert "1. 2-Day Basic Certificate" in opening
    assert "2. 1-Day Dental Infection Control" in opening

    selected = send(client, conversation_id, "2").get_json()["message"]["content"]
    assert "18 October 2026" in selected
    assert "15 November 2026" in selected
    assert "10–11 October 2026" not in selected
    assert not any(word in selected.casefold() for word in ("prototype", "testing note", "fictional"))

    send(client, conversation_id, "1")
    ready = send(
        client,
        conversation_id,
        "Full name: Test Student; NRIC/FIN: S1234567D; Date of birth: 15 May 2000; "
        "Email: test.student@example.com; Mobile number: 91234567; PayNow S$350",
    ).get_json()
    assert ready["enrollment"]["status"] == "awaiting_confirmation"
    assert "S$350.00" in ready["message"]["content"] or "S$350 nett" in ready["message"]["content"]

    confirmed = send(client, conversation_id, "Confirm").get_json()
    assert confirmed["enrollment"]["status"] == "confirmed"
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.friendly_reference.startswith("ENR-2026-")
        assert draft.course == "1-Day Dental Infection Control & Sterilisation Workshop"
        assert draft.course_fee == Decimal("350.00")
        assert draft.payment_method == "paynow"
        assert draft.skillsfuture_amount == Decimal("0.00")
        assert draft.paynow_amount == Decimal("350.00")


def test_infection_control_rejects_skillsfuture(tmp_path, fake_ai):
    app = _app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    send(client, conversation_id, "I want to enrol")
    send(client, conversation_id, "infection control workshop")
    send(client, conversation_id, "18 October 2026")
    reply = send(client, conversation_id, "I want to use SkillsFuture").get_json()["message"]["content"]
    assert "payable in full by PayNow" in reply
    assert "not currently available" in reply


def test_cleanup_is_guarded_backed_up_and_preserves_accounts_and_courses(tmp_path, fake_ai):
    app = _app(tmp_path, fake_ai)
    with app.app_context():
        user = User(
            email="preserved@example.com",
            full_name="Preserved User",
            password_hash="not-a-real-password-hash",
        )
        conversation = Conversation(user=user)
        draft = EnrollmentDraft(
            conversation=conversation,
            course="2-Day Basic Certificate in Dental Assisting",
            course_fee=Decimal("600.00"),
            status="confirmed",
            confirmed_at=utc_now(),
        )
        db.session.add_all([user, conversation, draft])
        db.session.add(
            OutboundMessage(
                channel="email",
                recipient="test.recipient@example.com",
                subject="Fictional invoice delivery test",
                body="Fictional test message",
                purpose="invoice_live_test",
                status="failed",
            )
        )
        intake_count = CourseDate.query.count()
        db.session.commit()
        service = app.extensions["maintenance"]
        assert service.dry_run()["enrollment_total"] == 1
        with pytest.raises(ValueError):
            service.clear("wrong")
        result = service.clear(CONFIRMATION_PHRASE)
        assert result["before"]["enrollment_total"] == 1
        assert result["after"]["enrollment_total"] == 0
        assert result["after"]["outbound_messages"] == 0
        assert User.query.filter_by(email="preserved@example.com").one()
        assert CourseDate.query.count() == intake_count
        assert all(row.participant_count == 0 for row in CourseDate.query.all())
        assert result["backup"]["database"]
        assert app.extensions["master_invoice"].validate_workbook()


def test_cleanup_refuses_production(tmp_path, fake_ai):
    app = _app(tmp_path, fake_ai, ENABLE_DEMO_INTAKES=False)
    with app.app_context():
        app.extensions["maintenance"].production = True
        with pytest.raises(RuntimeError, match="disabled in production"):
            app.extensions["maintenance"].clear(CONFIRMATION_PHRASE)
