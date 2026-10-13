"""Grouped multi-course enrollment regression coverage."""

from datetime import date
from decimal import Decimal

import pytest

from backend.app import create_app
from backend.extensions import db
from backend.models import EnrollmentCourse, EnrollmentDraft
from backend.models.operations import CourseDate
from backend.tests.conftest import authenticated_client, send


def _app(tmp_path, fake_ai, **overrides):
    config = {
        "TESTING": True,
        "SECRET_KEY": "multi-course-test",
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        "STORAGE_DIR": str(tmp_path / "private"),
        "MASTER_INVOICE_PATH": str(tmp_path / "private" / "Master_Invoice_List.xlsx"),
        "ENABLE_DEMO_INTAKES": True,
        "ENROLLMENT_CURRENT_DATE": date(2026, 9, 24),
        "INVOICE_GENERATION_ENABLED": False,
    }
    config.update(overrides)
    return create_app(config, ai_service=fake_ai)


def _start_both(client):
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    opening = send(client, conversation_id, "I want to enrol").get_json()
    assert opening["enrollment"]["course_id"] is None
    assert "1. Basic Certificate in Dental Assisting" in opening["message"]["content"]
    selected = send(client, conversation_id, "both").get_json()
    assert [row["course_id"] for row in selected["enrollment"]["courses"]] == [
        "dental-assisting-basic",
        "dental-clinic-front-desk-operations",
    ]
    return conversation_id, selected


def _complete_both(client, conversation_id):
    first = send(client, conversation_id, "1").get_json()
    assert "Now choose an intake for Course 2" in first["message"]["content"]
    second = send(client, conversation_id, "1").get_json()
    assert "Full name as shown on NRIC" in second["message"]["content"]
    ready = send(
        client,
        conversation_id,
        "Full name: Test Student; NRIC/FIN: S1234567D; "
        "Date of birth: 15 May 2000; Email: test.student@example.com; "
        "Mobile number: 91234567",
    ).get_json()
    return ready


@pytest.mark.parametrize(
    "choice",
    (
        "both",
        "1 and 2",
        "Dental Assisting and Front Desk Operations",
    ),
)
def test_two_course_phrases_store_two_unique_canonical_ids_in_order(
    tmp_path, fake_ai, choice
):
    app = _app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    send(client, conversation_id, "I want to enrol")

    payload = send(client, conversation_id, choice).get_json()

    assert [row["course_id"] for row in payload["enrollment"]["courses"]] == [
        "dental-assisting-basic",
        "dental-clinic-front-desk-operations",
    ]
    with app.app_context():
        rows = EnrollmentCourse.query.order_by(EnrollmentCourse.position).all()
        assert len(rows) == 2
        assert len({row.course_id for row in rows}) == 2


@pytest.mark.parametrize("choice", ("1 and 1", "Course 1 twice", "1 and Dental Assisting"))
def test_duplicate_course_wording_is_rejected(tmp_path, fake_ai, choice):
    app = _app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    send(client, conversation_id, "I want to enrol")

    reply = send(client, conversation_id, choice).get_json()["message"]["content"]

    assert "cannot select the same course twice" in reply.casefold()
    with app.app_context():
        assert EnrollmentCourse.query.count() == 0


def test_two_course_intakes_summary_and_confirmation_are_grouped_and_idempotent(
    tmp_path, fake_ai
):
    app = _app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id, _ = _start_both(client)
    ready = _complete_both(client, conversation_id)

    assert ready["enrollment"]["status"] == "awaiting_confirmation"
    content = ready["message"]["content"]
    assert "review both course enrollments" in content.casefold()
    assert "Basic Certificate in Dental Assisting" in content
    assert "Certificate in Dental Clinic Front Desk Operations" in content
    assert "Total amount due: S$1,050 nett" in content
    assert not any(
        phrase in content
        for phrase in ("Payment method", "PayNow amount", "SkillsFuture amount")
    )

    confirmed = send(client, conversation_id, "Confirm").get_json()
    repeated = send(client, conversation_id, "Confirm").get_json()
    assert confirmed["enrollment"]["status"] == "confirmed"
    assert repeated["enrollment"]["status"] == "confirmed"
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        rows = EnrollmentCourse.query.order_by(EnrollmentCourse.position).all()
        assert draft.course_fee == Decimal("1050.00")
        assert len(rows) == 2
        assert all(row.status == "confirmed" for row in rows)
        assert len({row.id for row in rows}) == 2
        counts = {
            row.id: row.participant_count
            for row in CourseDate.query.filter(
                CourseDate.id.in_([course.intake_id for course in rows])
            )
        }
        assert counts == {row.intake_id: 1 for row in rows}


def test_each_course_only_shows_and_accepts_its_own_intakes(tmp_path, fake_ai):
    app = _app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id, selected = _start_both(client)
    first_content = selected["message"]["content"]
    assert "18–19 November 2026" in first_content
    assert "2 December 2026" not in first_content

    next_course = send(client, conversation_id, "1").get_json()["message"]["content"]
    assert "2 December 2026" in next_course
    available_intakes = next_course.split("Available Intake Dates", 1)[1]
    assert "18–19 November 2026" not in available_intakes


def test_group_state_persists_in_previous_chat_history(tmp_path, fake_ai):
    app = _app(tmp_path, fake_ai)
    client = authenticated_client(app, "group-history@example.com")
    conversation_id, _ = _start_both(client)
    send(client, conversation_id, "1")

    reloaded = client.get(f"/api/conversations/{conversation_id}/messages").get_json()

    assert [row["course_id"] for row in reloaded["enrollment"]["courses"]] == [
        "dental-assisting-basic",
        "dental-clinic-front-desk-operations",
    ]
    assert reloaded["enrollment"]["courses"][0]["selected_intake"] is not None
    assert reloaded["enrollment"]["courses"][1]["selected_intake"] is None
