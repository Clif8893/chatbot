"""Deterministic age, intake, capacity and duplicate-enrollment regressions."""

from datetime import date

import pytest

from backend.app import create_app
from backend.extensions import db
from backend.models import Conversation, EnrollmentDraft
from backend.models.operations import CourseDate
from backend.services.course_catalogue import CourseCatalogue
from backend.services.enrollment.validation import validate_field
from backend.tests.conftest import authenticated_client, send


TODAY = date(2026, 9, 21)
VALID_DETAILS = (
    "Full name: Test Student; NRIC/FIN: S1234567D; "
    "Date of birth: 15 May 2000; Email: test.student@example.com; "
    "Mobile number: +65 9123 4567; PayNow S$600"
)


def business_app(tmp_path, fake_ai, name="business.db"):
    return create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "business-rule-test-secret",
            "OPENAI_API_KEY": "",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / name).as_posix()}",
            "ENABLE_DEMO_INTAKES": True,
            "ENROLLMENT_CURRENT_DATE": TODAY,
            "MINIMUM_ENROLLMENT_AGE": 16,
            "MAXIMUM_ENROLLMENT_AGE": 100,
        },
        ai_service=fake_ai,
    )


@pytest.mark.parametrize(
    ("value", "accepted"),
    [
        ("2026-09-22", False),
        ("2026-09-21", False),
        ("2026-09-20", False),
        ("22/09/2010", False),
        ("21/09/2010", True),
        ("29/02/2004", True),
        ("31/02/2000", False),
        ("20/09/1925", False),
        ("15 May 2000", True),
        ("2000-05-15", True),
        ("02092007", True),
        ("020927", True),
        ("32092007", False),
    ],
)
def test_dob_age_boundaries_use_frozen_singapore_date(value, accepted):
    normalized, error = validate_field(
        "date_of_birth",
        value,
        CourseCatalogue(),
        as_of_date=TODAY,
        minimum_age=16,
        maximum_age=100,
    )
    assert (error is None) is accepted
    assert (normalized is not None) is accepted


@pytest.mark.parametrize(
    ("value", "expected"),
    [("02092007", date(2007, 9, 2)), ("020927", date(1927, 9, 2))],
)
def test_compact_dob_uses_valid_age_range_for_century(value, expected):
    normalized, error = validate_field(
        "date_of_birth", value, CourseCatalogue(), as_of_date=TODAY,
        minimum_age=16, maximum_age=100,
    )
    assert error is None
    assert normalized == expected


def test_invalid_dob_is_not_saved_or_allowed_to_progress(tmp_path, fake_ai):
    app = business_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "1")
    response = send(
        client,
        conversation_id,
        VALID_DETAILS.replace("15 May 2000", "03/09/2026"),
    ).get_json()
    assert response["enrollment"]["status"] == "collecting"
    assert "minimum enrollment age" in response["message"]["content"]
    assert "Confirm, Correct" not in response["message"]["content"]
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.date_of_birth is None
        assert draft.full_name == "Test Student"
        assert draft.nric == "S1234567D"
        assert draft.email == "test.student@example.com"
        assert draft.mobile_number == "91234567"
        assert draft.validation_status == "needs_correction"
        assert draft.pending_corrections == ["date_of_birth"]


def test_invalid_dob_correction_never_overwrites_valid_dob(tmp_path, fake_ai):
    app = business_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "1")
    send(client, conversation_id, VALID_DETAILS)
    response = send(client, conversation_id, "My DOB should be 03/09/2026").get_json()
    assert "minimum enrollment age" in response["message"]["content"]
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.date_of_birth == date(2000, 5, 15)
        assert draft.nric == "S1234567D"


@pytest.mark.parametrize("value", ["", "test", "abc", "N/A", "unknown", "123456"])
def test_placeholder_or_numbers_only_names_are_rejected(value):
    _, error = validate_field("full_name", value, CourseCatalogue())
    assert error


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("email", "student"),
        ("email", "unknown"),
        ("mobile_number", "61234567"),
        ("mobile_number", "9123"),
        ("nric", "S1234567A"),
        ("nric", "S*******D"),
    ],
)
def test_logically_invalid_field_values_are_rejected(field, value):
    _, error = validate_field(field, value, CourseCatalogue())
    assert error


def test_seeded_intakes_are_database_backed_complete_and_idempotent(tmp_path, fake_ai):
    database_name = "seeded.db"
    app = business_app(tmp_path, fake_ai, database_name)
    with app.app_context():
        rows = CourseDate.query.order_by(CourseDate.date).all()
        assert [row.id for row in rows] == [
            "dental-assisting-2026-11-18",
            "dental-assisting-2026-11-28",
            "front-desk-operations-2026-12-02",
            "dental-assisting-2026-12-16",
            "front-desk-operations-2027-01-09",
            "front-desk-operations-2027-02-03",
        ]
        assert {row.course_id for row in rows} == {
            "dental-assisting-basic", "dental-clinic-front-desk-operations"
        }
        dental_rows = [row for row in rows if row.course_id == "dental-assisting-basic"]
        front_desk_rows = [row for row in rows if row.course_id == "dental-clinic-front-desk-operations"]
        assert all(row.start_time.strftime("%H:%M") == "09:30" for row in dental_rows)
        assert all(row.end_time.strftime("%H:%M") == "17:30" for row in dental_rows)
        assert all(row.start_time.strftime("%H:%M") == "00:00" for row in front_desk_rows)
        assert all(row.end_time.strftime("%H:%M") == "00:00" for row in front_desk_rows)
        assert all(row.date == row.end_date for row in front_desk_rows)
        assert all(row.capacity == 20 and row.participant_count == 0 for row in rows)
        assert all(row.status == "open" and not row.is_demo for row in rows)
    restarted = business_app(tmp_path, fake_ai, database_name)
    with restarted.app_context():
        assert CourseDate.query.count() == 6


@pytest.mark.parametrize(
    ("selection", "expected_id"),
    [
        ("1", "dental-assisting-2026-11-18"),
        ("first intake", "dental-assisting-2026-11-18"),
        ("18 November", "dental-assisting-2026-11-18"),
        ("18–19 November", "dental-assisting-2026-11-18"),
        ("weekend intake on 28 November", "dental-assisting-2026-11-28"),
    ],
)
def test_natural_intake_selection_uses_database_records(tmp_path, fake_ai, selection, expected_id):
    app = business_app(tmp_path, fake_ai, f"selection-{expected_id}-{len(selection)}.db")
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    offered = send(
        client, conversation_id, "I want to enroll in Dental Assisting"
    ).get_json()
    assert "18–19 November 2026 — Weekdays" in offered["message"]["content"]
    response = send(client, conversation_id, selection).get_json()
    assert response["enrollment"]["selected_intake"]["id"] == expected_id
    assert "Selected Intake" in response["message"]["content"]


def test_unique_weekend_intake_resolves_without_clarification(tmp_path, fake_ai):
    app = business_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    response = send(client, conversation_id, "weekend intake").get_json()
    assert response["enrollment"]["selected_intake"]["id"] == "dental-assisting-2026-11-28"


@pytest.mark.parametrize(
    ("status", "expected"),
    [("full", "full"), ("closed", "closed"), ("cancelled", "cancelled")],
)
def test_unavailable_intakes_cannot_be_selected(tmp_path, fake_ai, status, expected):
    app = business_app(tmp_path, fake_ai, f"{status}.db")
    with app.app_context():
        row = db.session.get(CourseDate, "dental-assisting-2026-11-18")
        if status == "full":
            row.participant_count = row.capacity
        else:
            row.status = status
        db.session.commit()
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    response = send(client, conversation_id, "18 November 2026").get_json()
    assert expected in response["message"]["content"].casefold()
    assert response["enrollment"]["selected_intake"] is None


def test_historical_intake_is_rejected_by_singapore_date(tmp_path, fake_ai):
    app = business_app(tmp_path, fake_ai)
    with app.app_context():
        db.session.add(CourseDate(id="past-demo", date=date(2026, 9, 1), end_date=date(2026, 9, 2), capacity=20, is_demo=True))
        db.session.commit()
        selected, error = app.extensions["catalogue"].match_intake_selection("1 September 2026")
        assert selected is None
        assert "already started" in error


def _ready_for_confirmation(client, conversation_id, intake="1"):
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, intake)
    return send(client, conversation_id, VALID_DETAILS).get_json()


def test_confirmation_allocates_once_and_cancellation_releases_place(tmp_path, fake_ai):
    app = business_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    assert _ready_for_confirmation(client, conversation_id)["enrollment"]["status"] == "awaiting_confirmation"
    send(client, conversation_id, "Confirm")
    send(client, conversation_id, "Confirm")
    with app.app_context():
        row = db.session.get(CourseDate, "dental-assisting-2026-11-18")
        draft = EnrollmentDraft.query.one()
        assert row.participant_count == 1
        assert draft.allocated_intake_id == row.id
    requested = send(client, conversation_id, "Cancel").get_json()
    assert "Confirm cancellation" in requested["message"]["content"]
    with app.app_context():
        assert db.session.get(CourseDate, "dental-assisting-2026-11-18").participant_count == 1
    send(client, conversation_id, "Confirm cancellation")
    with app.app_context():
        row = db.session.get(CourseDate, "dental-assisting-2026-11-18")
        assert row.participant_count == 0


def test_confirmed_intake_change_moves_exactly_one_place(tmp_path, fake_ai):
    app = business_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    _ready_for_confirmation(client, conversation_id)
    send(client, conversation_id, "Confirm")
    changed = send(client, conversation_id, "Change my intake to the second option").get_json()
    assert changed["enrollment"]["selected_intake"]["id"] == "dental-assisting-2026-11-18"
    assert "Confirm change" in changed["message"]["content"]
    send(client, conversation_id, "Confirm change")
    with app.app_context():
        first = db.session.get(CourseDate, "dental-assisting-2026-11-18")
        third = db.session.get(CourseDate, "dental-assisting-2026-12-16")
        draft = EnrollmentDraft.query.one()
        assert first.participant_count == 0
        assert third.participant_count == 1
        assert draft.allocated_intake_id == third.id
        assert draft.full_name == "Test Student"
        assert draft.nric == "S1234567D"


def test_selected_intake_and_validation_state_survive_restart_and_login(tmp_path, fake_ai):
    database_name = "restart.db"
    app = business_app(tmp_path, fake_ai, database_name)
    client = authenticated_client(app, email="restart-owner@example.com")
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "2")
    restarted = business_app(tmp_path, fake_ai, database_name)
    resumed = restarted.test_client()
    assert resumed.post(
        "/api/auth/login",
        json={"email": client.test_email, "password": client.test_password},
    ).status_code == 200
    history = resumed.get(f"/api/conversations/{conversation_id}/messages").get_json()
    assert history["enrollment"]["selected_intake"]["id"] == "dental-assisting-2026-11-28"
    with restarted.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.validation_status == "incomplete"
        assert draft.pending_corrections == []


def test_atomic_capacity_guard_prevents_second_allocation(tmp_path, fake_ai):
    app = business_app(tmp_path, fake_ai)
    with app.app_context():
        intake = db.session.get(CourseDate, "dental-assisting-2026-11-18")
        intake.capacity = 1
        first_conversation = Conversation()
        second_conversation = Conversation()
        db.session.add_all([first_conversation, second_conversation])
        db.session.flush()
        first = EnrollmentDraft(conversation=first_conversation, intake_id=intake.id)
        second = EnrollmentDraft(conversation=second_conversation, intake_id=intake.id)
        db.session.add_all([first, second])
        db.session.flush()
        service = app.extensions["intakes"]
        service.allocate(first)
        with pytest.raises(ValueError, match="full"):
            service.allocate(second)
        assert intake.participant_count == 1


def test_duplicate_active_enrollment_is_sent_for_staff_review(tmp_path, fake_ai):
    app = business_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    first_id = client.post("/api/conversations").get_json()["conversation_id"]
    _ready_for_confirmation(client, first_id)
    second_id = client.post("/api/conversations").get_json()["conversation_id"]
    response = _ready_for_confirmation(client, second_id)
    assert response["enrollment"]["status"] == "duplicate_review"
    assert "active enrolment with the same NRIC/FIN, mobile number" in response["message"]["content"]
    assert "test.student@example.com" not in response["message"]["content"]
