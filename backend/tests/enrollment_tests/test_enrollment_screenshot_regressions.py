"""Regressions for natural enrollment starts and multi-field extraction."""

from backend.agents.schemas import EnrollmentExtraction, EnrollmentFields
from backend.models import EnrollmentDraft
from backend.services.enrollment.enrollment_parser import parse_enrollment_fields
from backend.tests.conftest import send


def test_natural_dental_assisting_request_starts_without_course_correction(
    app, client, conversation_id
):
    response = send(
        client, conversation_id, "I want to enrol for the dental assisting course."
    ).get_json()

    assert response["enrollment"]["status"] == "collecting"
    assert "Needs correction" not in response["message"]["content"]
    assert "Enrollment is currently available" not in response["message"]["content"]
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.course == app.extensions["catalogue"].course_name


def test_repeated_vague_enrollment_request_keeps_course_gate_without_draft(
    app, client, conversation_id
):
    first = send(client, conversation_id, "Sign me up.").get_json()
    second = send(
        client, conversation_id, "I would like to join the course."
    ).get_json()

    assert first["enrollment"]["status"] is None
    assert second["enrollment"]["status"] is None
    assert "Please choose a course" in first["message"]["content"]
    assert "Please choose a course" in second["message"]["content"]
    with app.app_context():
        assert EnrollmentDraft.query.count() == 0


def test_model_explanatory_course_text_is_not_validated_as_a_field(
    app, client, conversation_id, fake_ai, monkeypatch
):
    def explanatory_result(*args, **kwargs):
        return EnrollmentExtraction(
            extracted_fields=EnrollmentFields(
                course="Enrollment is currently available for Dental Assisting."
            ),
            confirmation="none",
            correction_requested=False,
        )

    monkeypatch.setattr(fake_ai, "extract_enrollment", explanatory_result)
    response = send(
        client, conversation_id, "I want to enrol for the dental assisting course."
    ).get_json()
    assert "Needs correction" not in response["message"]["content"]
    with app.app_context():
        assert EnrollmentDraft.query.one().course == app.extensions["catalogue"].course_name


def test_parser_stops_name_before_email_and_phone_boundaries():
    email_fields = parse_enrollment_fields(
        "My name is Test Student and my email is test.student@example.com."
    )
    phone_fields = parse_enrollment_fields(
        "I am Alex Tan, mobile number 91234567."
    )

    assert email_fields["full_name"] == "Test Student"
    assert email_fields["email"] == "test.student@example.com"
    assert phone_fields["full_name"] == "Alex Tan"
    assert phone_fields["mobile_number"] == "91234567"


def test_multi_field_sentence_saves_only_clean_name_and_email(
    app, client, conversation_id
):
    send(client, conversation_id, "I want to enrol in Dental Assisting.")
    send(client, conversation_id, "1")
    response = send(
        client,
        conversation_id,
        "My name is Test Student and my email is test.student@example.com.",
    ).get_json()

    assert "Test Student and my" not in response["message"]["content"]
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.full_name == "Test Student"
        assert draft.email == "test.student@example.com"


def test_malformed_later_name_does_not_replace_saved_valid_name(
    app, client, conversation_id
):
    send(client, conversation_id, "I want to enrol in Dental Assisting.")
    send(client, conversation_id, "1")
    send(client, conversation_id, "Full name: Test Student")
    send(
        client,
        conversation_id,
        "My name is and my email is corrected.student@example.com",
    )

    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.full_name == "Test Student"
        assert draft.email == "corrected.student@example.com"
        assert "and my" not in draft.full_name.casefold()
