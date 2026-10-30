"""Enrollment collection, validation, correction, and confirmation tests."""

import json
import pytest

from backend.app import create_app
from backend.extensions import db
from backend.models import EnrollmentDraft
from backend.services.course_catalogue import CATALOGUE_PATH, CourseCatalogue
from backend.services.enrollment.validation import has_valid_identity_checksum, validate_field
from backend.tests.conftest import FakeAIService, authenticated_client, send


SYNTHETIC_NAME = "Test Student"
SYNTHETIC_NRIC = "S1234567D"
SYNTHETIC_EMAIL = "test.student@example.com"
SYNTHETIC_MOBILE = "91234567"
EXPECTED_INTAKE = {
    "id": "dental-assisting-2026-11-18",
    "start_date": "2026-11-18",
    "end_date": "2026-11-19",
    "display_date": "18–19 November 2026",
    "is_demo": False,
}


def complete_draft(client, conversation_id):
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "1")
    send(client, conversation_id, f"My name is {SYNTHETIC_NAME}")
    send(client, conversation_id, f"My NRIC is {SYNTHETIC_NRIC}")
    send(client, conversation_id, "My DOB is 1990-01-02")
    send(client, conversation_id, f"My email is {SYNTHETIC_EMAIL}")
    send(client, conversation_id, f"My mobile number is {SYNTHETIC_MOBILE}")
    return send(client, conversation_id, f"My mobile number is {SYNTHETIC_MOBILE}")


@pytest.fixture()
def dated_catalogue(tmp_path):
    data = json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))
    data["course"]["intakes"] = [
        {
            "id": "approved-2027-10-17-1",
            "course_id": "dental-assisting-basic",
            "start_date": "2027-10-17",
            "end_date": "2027-10-18",
            "display_date": "17–18 October 2027",
            "intake_type": "Weekend",
            "capacity": 20,
            "participant_count": 0,
            "status": "open",
            "is_demo": False,
        }
    ]
    path = tmp_path / "course_information.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return CourseCatalogue(path)


def test_partial_submission_asks_only_the_next_remaining_field(client, conversation_id):
    first = send(
        client, conversation_id, "I want to enroll in Dental Assisting"
    ).get_json()
    send(client, conversation_id, "1")
    second = send(
        client, conversation_id, f"My name is {SYNTHETIC_NAME}"
    ).get_json()
    assert "S$600 nett" in first["message"]["content"]
    reply = second["message"]["content"]
    assert "Saved successfully" in reply
    assert "May I have your 8-digit Singapore mobile number?" in reply
    assert "invoice preparation" not in reply
    assert "Date of birth" not in reply
    assert "Email address" not in reply
    assert "Payment method" not in reply
    assert "Full name as shown on NRIC: Test Student" in reply


def test_starting_enrollment_requires_course_selection_before_participant_details(
    client, conversation_id
):
    reply = send(client, conversation_id, "I want to enroll").get_json()["message"][
        "content"
    ]
    assert "Basic Certificate in Dental Assisting" in reply
    assert "Certificate in Dental Clinic Front Desk Operations" in reply
    assert "S$600 nett" in reply
    assert "S$450 nett" in reply
    assert "Reply with the course number, course name, or both." in reply
    assert "Enrollment Started" not in reply
    assert "Available intake" not in reply
    assert "NRIC/FIN:" not in reply


def test_multiple_fields_can_be_extracted_from_one_message(client, conversation_id):
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "1")
    response = send(
        client,
        conversation_id,
        f"Full name: {SYNTHETIC_NAME}; NRIC: {SYNTHETIC_NRIC}; "
        f"DOB: 1990-01-02; email: {SYNTHETIC_EMAIL}; "
        f"mobile: {SYNTHETIC_MOBILE}; PayNow amount: S$600",
    )
    payload = response.get_json()
    assert payload["enrollment"]["status"] == "awaiting_confirmation"
    assert payload["enrollment"]["missing_fields"] == []
    assert "S*******D" in payload["message"]["content"]
    assert "PayNow amount" not in payload["message"]["content"]
    assert "Total allocated" not in payload["message"]["content"]


def test_all_fields_are_extracted_from_copyable_structured_message(
    app, client, conversation_id
):
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "1")
    response = send(
        client,
        conversation_id,
        "\n".join(
            (
                f"Full name: {SYNTHETIC_NAME}",
                f"NRIC/FIN: {SYNTHETIC_NRIC}",
                "Date of birth: 15 May 2000",
                f"Email: {SYNTHETIC_EMAIL}",
                f"Mobile number: {SYNTHETIC_MOBILE}",
                "Payment method: SkillsFuture and PayNow",
                "SkillsFuture amount: S$400",
                "PayNow amount: S$200",
            )
        ),
    ).get_json()

    assert response["enrollment"] == {
        "status": "awaiting_confirmation",
        "course_id": "dental-assisting-basic",
        "course_name": "Basic Certificate in Dental Assisting",
        "course_fee": "600.00",
        "friendly_reference": None,
        "missing_fields": [],
        "demo_intakes_enabled": False,
        "selected_intake": EXPECTED_INTAKE,
    }
    reply = response["message"]["content"]
    assert "Course\n" in reply
    assert "Participant\n" in reply
    assert "Payment\n" not in reply
    assert "NRIC/FIN: S*******D" in reply
    assert "SkillsFuture amount" not in reply
    assert "PayNow amount" not in reply
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.nric == SYNTHETIC_NRIC
        assert draft.preferred_intake_date.isoformat() == "2026-11-18"
        assert draft.payment_method is None
        assert draft.skillsfuture_amount is None
        assert draft.paynow_amount is None


def test_all_fields_are_extracted_from_one_natural_language_sentence(
    app, client, conversation_id
):
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "1")
    response = send(
        client,
        conversation_id,
        f"{SYNTHETIC_NAME}, {SYNTHETIC_NRIC}, born 15 May 2000, "
        f"{SYNTHETIC_EMAIL}, {SYNTHETIC_MOBILE}.",
    ).get_json()

    assert response["enrollment"]["status"] == "awaiting_confirmation"
    assert response["enrollment"]["missing_fields"] == []
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.full_name == SYNTHETIC_NAME
        assert draft.nric == SYNTHETIC_NRIC
        assert draft.date_of_birth.isoformat() == "2000-05-15"
        assert draft.email == SYNTHETIC_EMAIL
        assert draft.mobile_number == SYNTHETIC_MOBILE
        assert draft.payment_method is None
        assert draft.skillsfuture_amount is None
        assert draft.paynow_amount is None


def test_partial_details_are_saved_then_completed_without_repetition(
    app, client, conversation_id
):
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "1")
    partial = send(
        client,
        conversation_id,
        f"Full name: {SYNTHETIC_NAME}; NRIC: {SYNTHETIC_NRIC}; "
        f"Email: {SYNTHETIC_EMAIL}",
    ).get_json()
    partial_reply = partial["message"]["content"]
    assert "8-digit Singapore mobile number" in partial_reply
    assert "Date of birth" not in partial_reply
    assert "Payment method" not in partial_reply
    assert "Full name as shown on NRIC: Test Student" in partial_reply
    assert SYNTHETIC_NRIC not in partial_reply

    completed = send(
        client,
        conversation_id,
        f"DOB: 15 May 2000; mobile: {SYNTHETIC_MOBILE}; PayNow amount: S$600",
    ).get_json()
    assert completed["enrollment"]["status"] == "awaiting_confirmation"
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.full_name == SYNTHETIC_NAME
        assert draft.nric == SYNTHETIC_NRIC
        assert draft.email == SYNTHETIC_EMAIL


def test_selected_date_saves_and_confirms_enrollment(
    app, client, conversation_id
):
    summary = complete_draft(client, conversation_id).get_json()
    assert summary["enrollment"]["status"] == "awaiting_confirmation"
    assert "18–19 November 2026" in summary["message"]["content"]

    confirmation = send(client, conversation_id, "Yes, confirm").get_json()
    assert confirmation["enrollment"]["status"] == "confirmed"
    reply = confirmation["message"]["content"]
    assert "confirmed successfully ✅" in reply
    assert "18–19 November 2026" in reply
    assert "July 2026" not in reply
    assert "Clementi Loop" not in reply
    with app.app_context():
        saved = EnrollmentDraft.query.one()
        assert saved.preferred_intake_date.isoformat() == "2026-11-18"
        assert saved.confirmed_at is not None


def test_all_required_enrollment_fields_are_stored_and_confirmed(
    tmp_path, fake_ai, dated_catalogue
):
    from backend.app import create_app

    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / 'dated.db').as_posix()}",
        },
        ai_service=fake_ai,
        catalogue=dated_catalogue,
    )
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "17 October 2027")
    response = send(
        client,
        conversation_id,
        f"Full name: {SYNTHETIC_NAME}; NRIC: {SYNTHETIC_NRIC}; "
        f"DOB: 1990-01-02; email: {SYNTHETIC_EMAIL}; "
        f"mobile: {SYNTHETIC_MOBILE}; SkillsFuture S$400; PayNow S$200",
    ).get_json()
    assert response["enrollment"] == {
        "status": "awaiting_confirmation",
        "missing_fields": [],
        "demo_intakes_enabled": False,
        "course_id": "dental-assisting-basic",
        "course_name": "Basic Certificate in Dental Assisting",
        "course_fee": "600.00",
        "friendly_reference": None,
        "selected_intake": {
            "id": "approved-2027-10-17-1",
            "start_date": "2027-10-17",
            "end_date": "2027-10-18",
            "display_date": "17–18 October 2027",
            "is_demo": False,
        },
    }
    assert "SkillsFuture amount" not in response["message"]["content"]
    assert "PayNow amount" not in response["message"]["content"]

    confirmed = send(client, conversation_id, "confirm").get_json()
    assert confirmed["enrollment"]["status"] == "confirmed"
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        values = (
            draft.preferred_intake_date,
            draft.full_name,
            draft.nric,
            draft.date_of_birth,
            draft.email,
            draft.mobile_number,
        )
        assert all(value is not None for value in values)
        assert draft.payment_method is None
        assert draft.skillsfuture_amount is None
        assert draft.paynow_amount is None


def test_repeated_confirmation_does_not_create_duplicate(app, client, conversation_id):
    complete_draft(client, conversation_id)
    send(client, conversation_id, "confirm")
    repeated = send(client, conversation_id, "confirm").get_json()
    assert "already confirmed" in repeated["message"]["content"]
    with app.app_context():
        assert db.session.query(EnrollmentDraft).count() == 1


def test_user_can_correct_a_field_before_confirmation(client, conversation_id):
    complete_draft(client, conversation_id)
    corrected = send(
        client, conversation_id, "Change my email to corrected@example.com"
    ).get_json()
    assert corrected["enrollment"]["status"] == "awaiting_confirmation"
    assert "Thanks, Test. I’ve updated your email address." in corrected["message"]["content"]
    assert "c***@example.com" in corrected["message"]["content"]
    assert "NRIC/FIN: S*******D" in corrected["message"]["content"]
    assert "Course\n" in corrected["message"]["content"]


def test_faq_interruption_preserves_and_resumes_enrollment(client, conversation_id):
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "1")
    send(client, conversation_id, f"My name is {SYNTHETIC_NAME}")
    faq = send(client, conversation_id, "What is the course fee?").get_json()
    assert faq["routing"]["intent"] == "faq"
    assert (
        "continue where you left off"
        in faq["message"]["content"].casefold()
    )
    resumed = send(
        client, conversation_id, f"My mobile number is {SYNTHETIC_MOBILE}"
    ).get_json()
    assert resumed["routing"]["intent"] == "enrollment"
    assert "email address" in resumed["message"]["content"].casefold()


@pytest.mark.parametrize(
    "payment_text",
    (
        "Payment method: PayNow; PayNow amount: S$600",
        "SkillsFuture S$400 and PayNow S$200",
    ),
)
def test_payment_details_are_ignored_and_never_stored(
    app, client, conversation_id, payment_text
):
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "1")
    response = send(
        client,
        conversation_id,
        f"Full name: {SYNTHETIC_NAME}; NRIC: {SYNTHETIC_NRIC}; "
        f"DOB: 15 May 2000; email: {SYNTHETIC_EMAIL}; "
        f"mobile: {SYNTHETIC_MOBILE}; {payment_text}",
    ).get_json()
    assert response["enrollment"]["status"] == "awaiting_confirmation"
    assert "Payment method" not in response["message"]["content"]
    assert "Payment allocation" not in response["message"]["content"]
    assert "PayNow amount" not in response["message"]["content"]
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.full_name == SYNTHETIC_NAME
        assert draft.nric == SYNTHETIC_NRIC
        assert draft.payment_method is None
        assert draft.skillsfuture_amount is None
        assert draft.paynow_amount is None


def test_invalid_email_and_mobile_are_corrected_without_losing_valid_details(
    app, client, conversation_id
):
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "1")
    invalid = send(
        client,
        conversation_id,
        f"Full name: {SYNTHETIC_NAME}; NRIC: {SYNTHETIC_NRIC}; "
        "DOB: 15 May 2000; email: invalid-email; mobile: 1234; PayNow amount: S$600",
    ).get_json()
    reply = invalid["message"]["content"]
    assert "Needs correction" in reply
    assert "Email address: Please enter a valid email address." in reply
    assert "Mobile number: Please enter a valid 8-digit Singapore mobile number" in reply
    assert not any(emoji in reply for emoji in ("👋", "😊", "📚", "📅", "✅", "💳"))

    corrected = send(
        client,
        conversation_id,
        f"Email: {SYNTHETIC_EMAIL}; mobile: {SYNTHETIC_MOBILE}",
    ).get_json()
    assert corrected["enrollment"]["status"] == "awaiting_confirmation"
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.full_name == SYNTHETIC_NAME
        assert draft.nric == SYNTHETIC_NRIC
        assert draft.date_of_birth.isoformat() == "2000-05-15"
        assert draft.email == SYNTHETIC_EMAIL
        assert draft.mobile_number == SYNTHETIC_MOBILE


def test_retired_payment_wording_does_not_block_other_valid_fields(
    app, client, conversation_id
):
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "1")
    conflict = send(
        client,
        conversation_id,
        f"Full name: {SYNTHETIC_NAME}; NRIC: {SYNTHETIC_NRIC}; "
        f"DOB: 15 May 2000; email: {SYNTHETIC_EMAIL}; "
        f"mobile: {SYNTHETIC_MOBILE}; I want UTAP and SkillsFuture",
    ).get_json()
    reply = conflict["message"]["content"]
    assert conflict["enrollment"]["status"] == "awaiting_confirmation"
    assert "SkillsFuture Credits and UTAP cannot be combined" not in reply
    assert "Payment method" not in reply
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.full_name == SYNTHETIC_NAME
        assert draft.nric == SYNTHETIC_NRIC
        assert draft.email == SYNTHETIC_EMAIL
        assert draft.payment_method is None


def test_payment_only_message_does_not_advance_enrollment(client, conversation_id):
    send(client, conversation_id, "I want to enroll")
    response = send(
        client,
        conversation_id,
        "SkillsFuture S$600",
    ).get_json()
    assert response["enrollment"]["status"] is None
    assert response["enrollment"]["course_id"] is None
    assert "Please choose a course" in response["message"]["content"]
    assert "SkillsFuture amount" not in response["message"]["content"]


def test_later_irrelevant_extraction_cannot_revalidate_or_replace_saved_identity(
    tmp_path,
):
    class EchoingDraftFieldsAI(FakeAIService):
        def extract_enrollment(self, message, context, draft_state, tools=None):
            result = super().extract_enrollment(message, context, draft_state, tools)
            if "remaining" in message.casefold():
                result.extracted_fields.nric = draft_state["nric"]
                result.extracted_fields.full_name = "Unrelated Person"
                result.extracted_fields.date_of_birth = "[DATE REDACTED]"
                result.extracted_fields.email = ""
                result.extracted_fields.mobile_number = "80000000"
            return result

    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / 'echoed.db').as_posix()}",
        },
        ai_service=EchoingDraftFieldsAI(),
    )
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    for message in (
        "I want to enroll in Dental Assisting",
        "1",
        f"My name is {SYNTHETIC_NAME}",
        f"My NRIC is {SYNTHETIC_NRIC}",
        "My DOB is 15 May 2000",
        f"My email is {SYNTHETIC_EMAIL}",
        f"My mobile number is {SYNTHETIC_MOBILE}",
    ):
        send(client, conversation_id, message)

    response = send(
        client,
        conversation_id,
        "I want to use $400 SkillsFuture and pay the remaining $200 by PayNow.",
    ).get_json()

    assert response["enrollment"] == {
        "status": "awaiting_confirmation",
        "course_id": "dental-assisting-basic",
        "course_name": "Basic Certificate in Dental Assisting",
        "course_fee": "600.00",
        "friendly_reference": None,
        "missing_fields": [],
        "demo_intakes_enabled": False,
        "selected_intake": EXPECTED_INTAKE,
    }
    assert "valid Singapore NRIC" not in response["message"]["content"]
    assert "S1234567D" not in response["message"]["content"]
    assert "S*******D" in response["message"]["content"]
    with app.app_context():
        draft = EnrollmentDraft.query.filter_by(
            conversation_id=conversation_id
        ).one()
        assert draft.full_name == SYNTHETIC_NAME
        assert draft.nric == SYNTHETIC_NRIC
        assert draft.date_of_birth.isoformat() == "2000-05-15"
        assert draft.email == SYNTHETIC_EMAIL
        assert draft.mobile_number == SYNTHETIC_MOBILE
        assert draft.payment_method is None
        assert draft.skillsfuture_amount is None
        assert draft.paynow_amount is None


def test_extraction_cannot_fill_a_field_absent_from_the_current_message(tmp_path):
    class InventingAI(FakeAIService):
        def extract_enrollment(self, message, context, draft_state, tools=None):
            result = super().extract_enrollment(message, context, draft_state, tools)
            if "nric" in message.casefold():
                result.extracted_fields.email = "invented@example.com"
                result.extracted_fields.mobile_number = "89999999"
            return result

    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / 'invented.db').as_posix()}",
        },
        ai_service=InventingAI(),
    )
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "1")
    send(client, conversation_id, f"My name is {SYNTHETIC_NAME}")
    send(client, conversation_id, f"My NRIC is {SYNTHETIC_NRIC}")

    with app.app_context():
        draft = EnrollmentDraft.query.filter_by(
            conversation_id=conversation_id
        ).one()
        assert draft.nric == SYNTHETIC_NRIC
        assert draft.email is None
        assert draft.mobile_number is None


def test_valid_nric_checksum_is_accepted_and_wrong_checksum_is_rejected():
    assert has_valid_identity_checksum(SYNTHETIC_NRIC) is True
    valid, valid_error = validate_field("nric", SYNTHETIC_NRIC, CourseCatalogue())
    assert valid == SYNTHETIC_NRIC
    assert valid_error is None

    invalid, invalid_error = validate_field("nric", "S1234567A", CourseCatalogue())
    assert invalid is None
    assert "checksum" in invalid_error


def test_enrollment_drafts_do_not_leak_between_conversations(app, client):
    first_id = client.post("/api/conversations").get_json()["conversation_id"]
    second_id = client.post("/api/conversations").get_json()["conversation_id"]
    send(client, first_id, "I want to enroll in Dental Assisting")
    send(client, first_id, "1")
    send(client, first_id, f"My name is {SYNTHETIC_NAME}")
    send(client, first_id, f"My NRIC is {SYNTHETIC_NRIC}")
    second_reply = send(client, second_id, "I want to enroll").get_json()

    assert second_reply["enrollment"]["status"] is None
    assert second_reply["enrollment"]["course_id"] is None
    assert "Please choose a course" in second_reply["message"]["content"]
    with app.app_context():
        first = EnrollmentDraft.query.filter_by(conversation_id=first_id).one()
        second = EnrollmentDraft.query.filter_by(conversation_id=second_id).one_or_none()
        assert first.nric == SYNTHETIC_NRIC
        assert second is None


@pytest.mark.parametrize(
    "field,value",
    (
        ("payment_method", "PayNow"),
        ("skillsfuture_amount", "400"),
        ("paynow_amount", "200"),
    ),
)
def test_retired_payment_fields_are_not_supported_by_enrollment_validator(field, value):
    with pytest.raises(ValueError, match="Unsupported enrollment field"):
        validate_field(field, value, CourseCatalogue())


@pytest.mark.parametrize("value", ["12345678", "71234567", "+65 8123"])
def test_invalid_mobile_number_is_rejected(value):
    normalized, error = validate_field("mobile_number", value, CourseCatalogue())
    assert normalized is None
    assert "valid 8-digit Singapore mobile number" in error


def test_mobile_number_is_normalized():
    normalized, error = validate_field(
        "mobile_number", "+65 9123-4567", CourseCatalogue()
    )
    assert error is None
    assert normalized == SYNTHETIC_MOBILE


def test_invalid_nric_is_rejected(client, conversation_id):
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "1")
    response = send(client, conversation_id, "My NRIC is 1234").get_json()
    assert "valid Singapore NRIC" in response["message"]["content"]


def test_invalid_email_is_rejected(client, conversation_id):
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "1")
    response = send(client, conversation_id, "My email is invalid-email").get_json()
    assert "valid email" in response["message"]["content"]


@pytest.mark.parametrize(
    "setup_messages,error_message",
    [
        (("My NRIC is 1234",), "valid Singapore NRIC"),
        (("My email is invalid-email",), "valid email"),
    ],
)
def test_sensitive_and_error_replies_do_not_contain_emojis(
    client, conversation_id, setup_messages, error_message
):
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "1")
    response = None
    for message in setup_messages:
        response = send(client, conversation_id, message).get_json()
    reply = response["message"]["content"]
    assert error_message in reply
    assert not any(emoji in reply for emoji in ("👋", "😊", "📚", "📅", "✅", "💳"))


def test_invalid_dob_is_rejected(client, conversation_id):
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "1")
    response = send(client, conversation_id, "My DOB is 2099-01-01").get_json()
    assert "valid past date" in response["message"]["content"]


def test_unconfigured_intake_is_not_accepted(client, conversation_id):
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    response = send(client, conversation_id, "My intake is 1 January 2030").get_json()
    assert "not one of the available intakes" in response["message"]["content"]


def test_full_name_must_contain_letters():
    value, error = validate_field("full_name", "12345", CourseCatalogue())
    assert value is None
    assert "using letters" in error


def test_invalid_course_is_rejected():
    value, error = validate_field("course", "Advanced Orthodontics", CourseCatalogue())
    assert value is None
    assert "currently available" in error
