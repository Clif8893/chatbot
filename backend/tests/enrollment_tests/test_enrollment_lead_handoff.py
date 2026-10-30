"""Enrollment-only lead handoff and staged conversation regressions.

All agent calls use the injected FakeAIService fixture; this module never calls an
external model provider.
"""

from backend.extensions import db
from backend.models import EnrollmentDraft
from backend.models.operations import Lead
from backend.tests.conftest import send


COURSE = "Basic Certificate in Dental Assisting"
INTAKE = "18 November 2026"
NRIC = "S1234567D"


def seed_lead(
    app,
    conversation_id,
    *,
    name="Dhiraj Test",
    phone="91234567",
    status="qualified",
):
    with app.app_context():
        draft = app.extensions["lead_handoff"].receive(
            conversation_id=conversation_id,
            full_name=name,
            phone_number=phone,
            selected_course=COURSE,
            preferred_intake_date=INTAKE,
            lead_status=status,
        )
        draft_id = draft.id
        db.session.commit()
        return draft_id


def test_enrollment_starts_without_previous_lead_and_asks_one_field_at_a_time(
    client, conversation_id
):
    opening = send(client, conversation_id, "I want to enroll").get_json()["message"][
        "content"
    ]
    assert COURSE in opening
    assert "NRIC/FIN:" not in opening

    intake_choices = send(client, conversation_id, "1").get_json()["message"][
        "content"
    ]
    assert "Available Intake Dates" in intake_choices

    first_detail = send(client, conversation_id, "1").get_json()["message"][
        "content"
    ]
    assert "May I have your full name" in first_detail
    assert "invoice preparation" not in first_detail


def test_simulated_lead_continues_at_email_without_reasking_saved_fields(
    app, client, conversation_id
):
    seed_lead(app, conversation_id)

    reply = send(client, conversation_id, "Continue my enrollment").get_json()[
        "message"
    ]["content"]

    assert "Great, Dhiraj" in reply
    assert COURSE in reply
    assert "May I have your email address?" in reply
    assert "May I have your full name" not in reply
    assert "mobile number?" not in reply
    assert "select one of the available course intakes" not in reply


def test_faq_interrupts_and_resumes_at_the_same_missing_field(
    app, client, conversation_id
):
    seed_lead(app, conversation_id)
    send(client, conversation_id, "Continue my enrollment")

    faq_reply = send(client, conversation_id, "How much is the course fee?").get_json()[
        "message"
    ]["content"]
    assert "$600" in faq_reply
    assert "May I have your email address?" in faq_reply

    resumed = send(
        client, conversation_id, "My email is dhiraj.test@example.com"
    ).get_json()["message"]["content"]
    assert "date of birth" in resumed.casefold()
    assert "full name" not in resumed.casefold()


def test_nric_is_requested_only_after_contact_and_dob_with_reason(
    app, client, conversation_id
):
    seed_lead(app, conversation_id)

    email_reply = send(client, conversation_id, "Continue my enrollment").get_json()[
        "message"
    ]["content"]
    assert "NRIC/FIN?" not in email_reply

    dob_reply = send(
        client, conversation_id, "My email is dhiraj.test@example.com"
    ).get_json()["message"]["content"]
    assert "date of birth" in dob_reply.casefold()
    assert "NRIC/FIN?" not in dob_reply

    nric_reply = send(
        client, conversation_id, "My DOB is 15/05/1990"
    ).get_json()["message"]["content"]
    assert "official registration and invoice preparation" in nric_reply
    assert "will be masked" in nric_reply


def test_final_confirmation_has_one_complete_masked_summary(
    app, client, conversation_id
):
    seed_lead(app, conversation_id)
    send(client, conversation_id, "My email is dhiraj.test@example.com")
    send(client, conversation_id, "My DOB is 15/05/1990")

    summary = send(client, conversation_id, f"My NRIC is {NRIC}").get_json()
    content = summary["message"]["content"]
    assert summary["enrollment"]["status"] == "awaiting_confirmation"
    assert content.count("Please review your enrolment before final submission.") == 1
    assert COURSE in content
    assert "18–19 November 2026" in content
    assert "Dhiraj Test" in content
    assert "S*******D" in content
    assert NRIC not in content
    assert "d***@example.com" in content
    assert "****4567" in content

    confirmed = send(client, conversation_id, "Confirm enrollment").get_json()
    assert confirmed["enrollment"]["status"] == "confirmed"


def test_identical_handoff_is_idempotent_and_shared_phone_is_allowed(
    app, client
):
    first_id = client.post("/api/conversations").get_json()["conversation_id"]
    second_id = client.post("/api/conversations").get_json()["conversation_id"]
    first_draft_id = seed_lead(app, first_id)
    assert seed_lead(app, first_id) == first_draft_id

    second_draft_id = seed_lead(app, second_id, name="Another Student")
    assert second_draft_id != first_draft_id

    with app.app_context():
        assert EnrollmentDraft.query.count() == 2


def test_phone_or_nric_alone_is_allowed_but_exact_identity_duplicate_is_blocked(
    client,
):
    first_id = client.post("/api/conversations").get_json()["conversation_id"]
    second_id = client.post("/api/conversations").get_json()["conversation_id"]
    third_id = client.post("/api/conversations").get_json()["conversation_id"]
    fourth_id = client.post("/api/conversations").get_json()["conversation_id"]

    def complete(conversation_id, name, nric, mobile="91234567"):
        send(client, conversation_id, "I want to enroll in Dental Assisting")
        send(client, conversation_id, "1")
        return send(
            client,
            conversation_id,
            f"Full name: {name}; mobile: {mobile}; email: {name.replace(' ', '.').casefold()}@example.com; "
            f"DOB: 15/05/1990; NRIC: {nric}",
        ).get_json()

    first = complete(first_id, "First Student", "S1234567D")
    assert first["enrollment"]["status"] == "awaiting_confirmation"
    second = complete(second_id, "Second Student", "T0000000G")
    assert second["enrollment"]["status"] == "awaiting_confirmation"

    third = complete(third_id, "Third Student", "S1234567D", "92345678")
    assert third["enrollment"]["status"] == "awaiting_confirmation"

    exact_duplicate = complete(fourth_id, "Fourth Student", "S1234567D")
    assert exact_duplicate["enrollment"]["status"] == "duplicate_review"
    assert "same NRIC/FIN, mobile number, course and intake" in exact_duplicate["message"]["content"]


def test_lead_and_enrollment_state_are_isolated_by_conversation(
    app, client
):
    first_id = client.post("/api/conversations").get_json()["conversation_id"]
    second_id = client.post("/api/conversations").get_json()["conversation_id"]
    seed_lead(app, first_id, name="Dhiraj Test", phone="91234567")
    seed_lead(app, second_id, name="Alicia Test", phone="92345678")

    first_reply = send(client, first_id, "Continue my enrollment").get_json()[
        "message"
    ]["content"]
    second_reply = send(client, second_id, "Continue my enrollment").get_json()[
        "message"
    ]["content"]
    assert "Dhiraj" in first_reply and "Alicia" not in first_reply
    assert "Alicia" in second_reply and "Dhiraj" not in second_reply

    with app.app_context():
        first = EnrollmentDraft.query.filter_by(conversation_id=first_id).one()
        second = EnrollmentDraft.query.filter_by(conversation_id=second_id).one()
        assert first.mobile_number == "91234567"
        assert second.mobile_number == "92345678"
        assert Lead.query.filter_by(conversation_id=first_id).one().status == "qualified"
        assert Lead.query.filter_by(conversation_id=second_id).one().status == "qualified"
