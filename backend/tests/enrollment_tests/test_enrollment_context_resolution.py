"""Owner-scoped, course-aware participant self-service regressions."""

from datetime import date
from decimal import Decimal

import pytest

from backend.extensions import db
from backend.models import Conversation, EnrollmentDraft, utc_now
from backend.models.operations import CourseDate
from backend.tests.conftest import authenticated_client, send
from backend.tests.enrollment_tests.test_enrollment_management import (
    add_confirmed,
    management_app,
)


@pytest.mark.parametrize(
    "question",
    [
        "enrollment status",
        "Enrollment status",
        "What is my enrollment status?",
        "Check my enrollment.",
        "Is my enrollment confirmed?",
        "Am I registered?",
        "Did my registration go through?",
        "Check status lah",
        "My course status",
        "Status please",
    ],
)
def test_status_variations_are_deterministic(tmp_path, fake_ai, question):
    app = management_app(tmp_path, fake_ai, f"context-{abs(hash(question))}.db")
    client = authenticated_client(app)
    conversation_id, draft_id = add_confirmed(app, client)
    with app.app_context():
        draft = db.session.get(EnrollmentDraft, draft_id)
        draft.course_id = "dental-assisting-basic"
        draft.friendly_reference = "ENR-2026-000001"
        db.session.commit()
    before = (len(fake_ai.classify_calls), len(fake_ai.faq_calls), len(fake_ai.enrollment_calls))
    result = send(client, conversation_id, question).get_json()
    after = (len(fake_ai.classify_calls), len(fake_ai.faq_calls), len(fake_ai.enrollment_calls))
    content = result["message"]["content"]
    assert after == before
    assert "Enrollment Status: Confirmed" in content
    assert "ENR-2026-000001" in content
    assert "10" in content and "11 October 2026" in content
    assert "S1234567D" not in content


def _add_infection_control(app, client, reference="ENR-2026-000002"):
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    with app.app_context():
        conversation = db.session.get(Conversation, conversation_id)
        intake = db.session.get(CourseDate, "infection-control-2026-11-15")
        draft = EnrollmentDraft(
            conversation=conversation,
            course_id="dental-infection-control",
            course="1-Day Dental Infection Control & Sterilisation Workshop",
            course_fee=Decimal("350"),
            full_name="Test Student",
            nric="S1234567D",
            date_of_birth=date(2000, 5, 15),
            email="test.student@example.com",
            mobile_number="91234567",
            payment_method="paynow",
            skillsfuture_amount=Decimal("0"),
            paynow_amount=Decimal("350"),
            intake_id=intake.id,
            preferred_intake_date=intake.date,
            intake_start_date=intake.date,
            intake_end_date=intake.end_date,
            intake_is_demo=intake.is_demo,
            friendly_reference=reference,
            status="confirmed",
            validation_status="valid",
            confirmed_at=utc_now(),
        )
        db.session.add(draft)
        db.session.flush()
        app.extensions["intakes"].allocate(draft)
        db.session.commit()
        return conversation_id, draft.id


@pytest.mark.parametrize("question", ["What time is my enrollment?", "When is my class?", "Which intake did I select?"])
def test_training_lookup_uses_selected_course_and_intake(tmp_path, fake_ai, question):
    app = management_app(tmp_path, fake_ai, f"training-{abs(hash(question))}.db")
    client = authenticated_client(app)
    conversation_id, _ = _add_infection_control(app, client)
    before = len(fake_ai.classify_calls)
    result = send(client, conversation_id, question).get_json()["message"]["content"]
    assert len(fake_ai.classify_calls) == before
    assert "1-Day Dental Infection Control" in result
    assert "15 November 2026" in result
    assert "9:30am-5:30pm" in result
    assert "S$600" not in result
    assert "Basic Certificate" not in result


def test_explicit_reference_is_owner_scoped_and_resolves_multiple(tmp_path, fake_ai):
    app = management_app(tmp_path, fake_ai)
    owner = authenticated_client(app, email="owner@example.com")
    first_conversation, first_id = add_confirmed(app, owner)
    second_conversation, _ = _add_infection_control(app, owner, "ENR-2026-000099")
    with app.app_context():
        first = db.session.get(EnrollmentDraft, first_id)
        first.course_id = "dental-assisting-basic"
        first.friendly_reference = "ENR-2026-000098"
        db.session.commit()
    blank = owner.post("/api/conversations").get_json()["conversation_id"]
    resolved = send(owner, blank, "Status for ENR-2026-000099").get_json()["message"]["content"]
    assert "Infection Control" in resolved
    assert "ENR-2026-000099" in resolved

    stranger = authenticated_client(app, email="stranger@example.com")
    stranger_conversation = stranger.post("/api/conversations").get_json()["conversation_id"]
    rejected = send(stranger, stranger_conversation, "Status for ENR-2026-000099").get_json()["message"]["content"]
    assert "No saved enrollment" in rejected
    assert "ENR-2026-000099" not in rejected


def test_generic_course_questions_are_focused_and_ambiguous_questions_clarify(tmp_path, fake_ai):
    app = management_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    fee = send(client, conversation_id, "How much is Infection Control?").get_json()["message"]["content"]
    timing = send(client, conversation_id, "What time is the Infection Control workshop?").get_json()["message"]["content"]
    ambiguous = send(client, conversation_id, "How much is the course?").get_json()["message"]["content"]
    assert "S$350 nett" in fee and "S$600" not in fee
    assert "9:30am to 5:30pm" in timing and "S$350" not in timing
    assert "Which course would you like to check?" in ambiguous
    assert "2-Day Basic Certificate" in ambiguous and "1-Day Dental Infection Control" in ambiguous


def test_confirmation_invoice_preparation_text_occurs_once(tmp_path, fake_ai):
    app = management_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    send(client, conversation_id, "I want to enrol")
    send(client, conversation_id, "2")
    send(client, conversation_id, "1")
    send(
        client,
        conversation_id,
        "Full name: Test Student; NRIC/FIN: S1234567D; Date of birth: 15 May 2000; "
        "Email: test.student@example.com; Mobile number: 91234567; PayNow S$350",
    )
    confirmed = send(client, conversation_id, "Confirm").get_json()["message"]["content"]
    assert confirmed.casefold().count("invoice is being prepared") == 1
    assert "1-Day Dental Infection Control" in confirmed
    assert "18 October 2026" in confirmed
    assert "ENR-2026-" in confirmed
    assert confirmed.count("Need to make a change?") == 1
