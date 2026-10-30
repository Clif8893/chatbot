"""Regressions for the staged, deterministic enrollment conversation."""

import json
from decimal import Decimal

import pytest

from backend.app import create_app
from backend.extensions import db
from backend.models import EnrollmentDraft
from backend.models.finance import MasterInvoiceExport, Payment
from backend.services.course_catalogue import CATALOGUE_PATH, CourseCatalogue
from backend.tests.conftest import FakeAIService, authenticated_client, send


def test_greeting_is_semantic_small_talk_and_does_not_start_enrollment(
    client, conversation_id, fake_ai
):
    before = len(fake_ai.classify_calls)

    payload = send(client, conversation_id, "Good morning!").get_json()

    assert payload["routing"]["intent"] == "small_talk"
    assert payload["enrollment"]["status"] is None
    assert payload["message"]["content"].startswith("Good morning!")
    assert len(fake_ai.classify_calls) == before + 1


@pytest.mark.parametrize(
    ("message", "opening"),
    [
        ("HI!!!", "Hi!"),
        ("hello.", "Hello!"),
        ("Good Afternoon", "Good afternoon!"),
        ("good evening", "Good evening!"),
        ("gud morning", "Good morning!"),
    ],
)
def test_common_greeting_variants_use_semantic_small_talk(
    client, conversation_id, fake_ai, message, opening
):
    before = len(fake_ai.classify_calls)
    payload = send(client, conversation_id, message).get_json()
    assert payload["message"]["content"].startswith(opening)
    assert payload["enrollment"]["status"] is None
    assert len(fake_ai.classify_calls) == before + 1


def test_greeting_during_enrollment_preserves_draft_and_resumes_next_step(
    app, client, conversation_id, fake_ai
):
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "1")
    send(client, conversation_id, "My full name is Test Student")
    before = len(fake_ai.classify_calls)

    reply = send(client, conversation_id, "Morning!").get_json()["message"][
        "content"
    ]

    assert reply.startswith("Good morning, Test!")
    assert "valid nric/fin" not in reply.casefold()
    assert "8-digit singapore mobile number" in reply.casefold()
    assert "nric/fin" not in reply.casefold()
    assert len(fake_ai.classify_calls) == before + 1
    with app.app_context():
        assert EnrollmentDraft.query.one().full_name == "Test Student"


def test_enrollment_starts_with_course_choices_before_personal_data(
    client, conversation_id
):
    reply = send(client, conversation_id, "I want to enroll").get_json()["message"][
        "content"
    ]

    assert "Basic Certificate in Dental Assisting" in reply
    assert "S$600 nett" in reply
    assert "Certificate in Dental Clinic Front Desk Operations" in reply
    assert "Please choose a course" in reply
    assert "Enrollment Started" not in reply
    assert "NRIC/FIN:" not in reply
    assert "Payment method:" not in reply
    assert "PayNow" not in reply
    assert "SkillsFuture" not in reply
    assert "UTAP" not in reply


@pytest.mark.parametrize(
    "affirmative",
    [
        "yes",
        "yes please",
        "sure",
        "okay",
        "I would like to",
        "sign me up",
        "can enrol",
        "yup",
    ],
)
def test_contextual_affirmative_after_course_description_requires_explicit_course(
    client, conversation_id, affirmative
):
    offered = send(client, conversation_id, "Tell me about the course").get_json()
    assert "Basic Certificate in Dental Assisting" in offered["message"]["content"]
    assert "Certificate in Dental Clinic Front Desk Operations" in offered["message"]["content"]

    response = send(client, conversation_id, affirmative).get_json()

    assert response["enrollment"]["status"] is None
    assert response["enrollment"]["course_id"] is None


def test_contextual_no_after_course_offer_stays_in_enquiry_mode(
    client, conversation_id
):
    send(client, conversation_id, "Tell me about the course")

    response = send(client, conversation_id, "No thanks").get_json()

    assert response["routing"]["intent"] == "unclear"
    assert response["enrollment"]["status"] is None


def test_no_after_direct_enrollment_start_keeps_resumable_draft(
    app, client, conversation_id
):
    send(client, conversation_id, "I want to enroll")
    send(client, conversation_id, "1")

    response = send(client, conversation_id, "No thanks").get_json()

    assert response["routing"]["intent"] == "enrollment"
    assert response["enrollment"]["status"] == "collecting"
    with app.app_context():
        assert EnrollmentDraft.query.count() == 1
        assert EnrollmentDraft.query.one().course is not None


def test_contextual_yes_does_not_create_duplicate_enrollment_drafts(
    app, client, conversation_id
):
    send(client, conversation_id, "Tell me about the course")
    send(client, conversation_id, "yes")
    send(client, conversation_id, "yes")

    with app.app_context():
        assert EnrollmentDraft.query.count() == 0


def test_joined_numbered_fields_are_split_and_invalid_nric_is_explained(
    app, client, conversation_id
):
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "1")

    payload = send(
        client,
        conversation_id,
        "1. Full name: Test Student 2. NRIC/FIN: S1234567A "
        "3. Date of birth: 3 September 2007 "
        "4. Email: test.student@example.com5. Mobile number: 91234567",
    ).get_json()

    reply = payload["message"]["content"]
    assert "Saved successfully" in reply
    assert "Needs correction" in reply
    assert "checksum" in reply.casefold()
    assert "Please send only your corrected NRIC/FIN" in reply
    assert "test.student@example.com5" not in reply

    with app.app_context():
        draft = db.session.execute(
            db.select(EnrollmentDraft).where(
                EnrollmentDraft.conversation_id == conversation_id
            )
        ).scalar_one()
        assert draft.full_name == "Test Student"
        assert draft.nric is None
        assert draft.date_of_birth.isoformat() == "2007-09-03"
        assert draft.email == "test.student@example.com"
        assert draft.mobile_number == "91234567"


def test_joined_numbered_fields_extract_all_valid_participant_values(
    app, client, conversation_id
):
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "1")
    payload = send(
        client,
        conversation_id,
        "1. Full name: Test Student 2. NRIC/FIN: S1234567D "
        "3. Date of birth: 3rd September 2007 "
        "4. Email: test.student@example.com5. Mobile number: 91234567",
    ).get_json()
    assert payload["enrollment"]["missing_fields"] == []
    assert payload["enrollment"]["status"] == "awaiting_confirmation"
    assert "Payment summary" not in payload["message"]["content"]
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.date_of_birth.isoformat() == "2007-09-03"
        assert draft.email == "test.student@example.com"


def test_selected_course_saves_approved_fee_snapshot(app, client, conversation_id):
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.course == "Basic Certificate in Dental Assisting"
        assert draft.course_fee == Decimal("600.00")


def test_missing_approved_price_is_not_fabricated(tmp_path):
    data = json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))
    data["course"]["fee"]["amount"] = None
    data["course"]["fee"]["display"] = ""
    path = tmp_path / "course-without-price.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / 'no-price.db').as_posix()}",
        },
        ai_service=FakeAIService(),
        catalogue=CourseCatalogue(path),
    )
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    offer = send(
        client, conversation_id, "I want to enroll in Dental Assisting"
    ).get_json()["message"]["content"]
    assert "Awaiting staff confirmation" in offer
    assert "18–19 November 2026" in offer
    assert "S$600" not in offer


def test_intake_selection_is_limited_to_approved_dates(tmp_path):
    data = json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))
    data["course"]["intakes"] = [{
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
    }]
    path = tmp_path / "dated-course.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / 'dated-flow.db').as_posix()}",
        },
        ai_service=FakeAIService(),
        catalogue=CourseCatalogue(path),
    )
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    selection = send(
        client, conversation_id, "I want to enroll in Dental Assisting"
    ).get_json()
    assert "17–18 October 2027" in selection["message"]["content"]
    invalid = send(client, conversation_id, "Intake: 19 October 2027").get_json()
    assert "That date is not one of the available intakes" in invalid["message"]["content"]
    assert "1. 17–18 October 2027" in invalid["message"]["content"]
    valid = send(client, conversation_id, "Intake: 17 October 2027").get_json()
    assert valid["enrollment"]["missing_fields"][0] == "full_name"


def _complete_required_details(client, conversation_id):
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "1")
    return send(
        client,
        conversation_id,
        "Full name: Test Student; NRIC/FIN: S1234567D; "
        "Date of birth: 15 May 2000; Email: test.student@example.com; "
        "Mobile number: 91234567",
    ).get_json()


@pytest.mark.parametrize(
    "message",
    (
        "PayNow only",
        "PayNow amount: S$550",
        "Use S$400 SkillsFuture and S$200 PayNow",
        "I want UTAP",
    ),
)
def test_payment_inputs_are_not_collected_or_validated(
    app, client, conversation_id, message
):
    identity = _complete_required_details(client, conversation_id)
    assert identity["enrollment"]["status"] == "awaiting_confirmation"
    response = send(client, conversation_id, message).get_json()
    assert "Payment method" not in response["message"]["content"]
    assert "Payment allocation" not in response["message"]["content"]
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.payment_method is None
        assert draft.skillsfuture_amount is None
        assert draft.paynow_amount is None


def test_contextual_yes_and_no_apply_to_confirmation_summary(
    client, conversation_id
):
    summary = _complete_required_details(client, conversation_id)
    assert summary["enrollment"]["status"] == "awaiting_confirmation"

    rejected = send(client, conversation_id, "no").get_json()
    assert rejected["enrollment"]["status"] == "collecting"
    assert "Which detail would you like to correct?" in rejected["message"]["content"]

    send(client, conversation_id, "Correct email: corrected@example.com")
    confirmed = send(client, conversation_id, "yes please").get_json()
    assert confirmed["enrollment"]["status"] == "confirmed"
    assert "Reference:" in confirmed["message"]["content"]


def test_confirmation_is_masked_idempotent_and_exported_without_payment_fields(
    app, client, conversation_id
):
    summary = _complete_required_details(client, conversation_id)
    content = summary["message"]["content"]
    assert summary["enrollment"]["status"] == "awaiting_confirmation"
    assert "Course fee: S$600 nett" in content
    assert "S*******D" in content
    assert "t***@example.com" in content
    assert "****4567" in content
    assert "Remaining amount" not in content
    assert "Payment status" not in content
    assert "S1234567D" not in content

    first = send(client, conversation_id, "Confirm").get_json()
    second = send(client, conversation_id, "Confirm").get_json()
    assert first["enrollment"]["status"] == "confirmed"
    assert "Reference:" in first["message"]["content"]
    assert "already confirmed" in second["message"]["content"]
    with app.app_context():
        assert EnrollmentDraft.query.count() == 1
        export = MasterInvoiceExport.query.one()
        assert export.status == "exported"
        assert export.attempts == 1
        assert Payment.query.count() == 0


def test_single_field_correction_preserves_other_identity_values(
    app, client, conversation_id
):
    _complete_required_details(client, conversation_id)
    send(client, conversation_id, "Correct email: corrected@example.com")
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.email == "corrected@example.com"
        assert draft.full_name == "Test Student"
        assert draft.nric == "S1234567D"
        assert draft.mobile_number == "91234567"
