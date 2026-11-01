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
    before = (len(fake_ai.classify_calls), len(fake_ai.enquiry_calls), len(fake_ai.enrollment_calls))
    result = send(client, conversation_id, question).get_json()
    after = (len(fake_ai.classify_calls), len(fake_ai.enquiry_calls), len(fake_ai.enrollment_calls))
    content = result["message"]["content"]
    assert after == before
    assert "Enrollment Status: Confirmed" in content
    assert "ENR-2026-000001" in content
    assert "18–19 November 2026" in content
    assert "S1234567D" not in content


def _add_front_desk(app, client, reference="ENR-2026-000002"):
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    with app.app_context():
        conversation = db.session.get(Conversation, conversation_id)
        intake = db.session.get(CourseDate, "front-desk-operations-2027-01-09")
        draft = EnrollmentDraft(
            conversation=conversation,
            course_id="dental-clinic-front-desk-operations",
            course="Certificate in Dental Clinic Front Desk Operations",
            course_fee=Decimal("450"),
            full_name="Test Student",
            nric="S1234567D",
            date_of_birth=date(2000, 5, 15),
            email="test.student@example.com",
            mobile_number="91234567",
            payment_method="paynow",
            skillsfuture_amount=Decimal("0"),
            paynow_amount=Decimal("450"),
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


@pytest.mark.parametrize("question", ["What time is my enrollment?", "When is my class?"])
def test_training_lookup_uses_selected_course_and_intake(tmp_path, fake_ai, question):
    app = management_app(tmp_path, fake_ai, f"training-{abs(hash(question))}.db")
    client = authenticated_client(app)
    conversation_id, _ = _add_front_desk(app, client)
    before = len(fake_ai.classify_calls)
    result = send(client, conversation_id, question).get_json()["message"]["content"]
    assert len(fake_ai.classify_calls) == before
    assert "Certificate in Dental Clinic Front Desk Operations" in result
    assert "9 January 2027" in result
    assert "Staff confirmation is required" in result
    assert "9:30am" not in result
    assert "S$600" not in result
    assert "Basic Certificate" not in result


def test_explicit_reference_is_owner_scoped_and_resolves_multiple(tmp_path, fake_ai):
    app = management_app(tmp_path, fake_ai)
    owner = authenticated_client(app, email="owner@example.com")
    first_conversation, first_id = add_confirmed(app, owner)
    second_conversation, _ = _add_front_desk(app, owner, "ENR-2026-000099")
    with app.app_context():
        first = db.session.get(EnrollmentDraft, first_id)
        first.course_id = "dental-assisting-basic"
        first.friendly_reference = "ENR-2026-000098"
        db.session.commit()
    blank = owner.post("/api/conversations").get_json()["conversation_id"]
    resolved = send(owner, blank, "Status for ENR-2026-000099").get_json()["message"]["content"]
    assert "Front Desk Operations" in resolved
    assert "ENR-2026-000099" in resolved

    stranger = authenticated_client(app, email="stranger@example.com")
    stranger_conversation = stranger.post("/api/conversations").get_json()["conversation_id"]
    rejected = send(stranger, stranger_conversation, "Status for ENR-2026-000099").get_json()["message"]["content"]
    assert "No saved enrollment" in rejected
    assert "ENR-2026-000099" not in rejected


def test_named_course_questions_are_focused_and_generic_questions_cover_both(tmp_path, fake_ai):
    app = management_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    fee = send(client, conversation_id, "How much is Front Desk Operations?").get_json()["message"]["content"]
    timing = send(client, conversation_id, "What time is the Front Desk Operations workshop?").get_json()["message"]["content"]
    fresh_conversation = client.post("/api/conversations").get_json()["conversation_id"]
    generic = send(client, fresh_conversation, "How much is the course?").get_json()["message"]["content"]
    assert "$450 nett" in fee and "$600" not in fee
    assert "Training hours: to be confirmed" in timing and "9:30am" not in timing
    assert "Which course would you like to check?" not in generic
    assert "$600" in generic and "$450" in generic


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
        "Email: test.student@example.com; Mobile number: 91234567; PayNow S$450",
    )
    confirmed = send(client, conversation_id, "Confirm").get_json()["message"]["content"]
    assert confirmed.casefold().count("invoice is being prepared") == 1
    assert "Certificate in Dental Clinic Front Desk Operations" in confirmed
    assert "2 December 2026" in confirmed
    assert "ENR-2026-" in confirmed
    assert confirmed.count("Need to make a change?") == 1
