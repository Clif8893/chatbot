"""Regression coverage for the staged single- and multi-course selection gate."""

from datetime import date

import pytest

from backend.agents.schemas import EnrollmentExtraction, EnrollmentFields
from backend.app import create_app
from backend.extensions import db
from backend.models import Conversation, EnrollmentCourse, EnrollmentDraft
from backend.services.enrollment.enrollment_service import (
    COURSE_OPTIONS_KEY,
    COURSE_SELECTION_FOLLOWUP,
    INTAKE_SELECTION_FOLLOWUP,
)
from backend.tests.conftest import FakeAIService, authenticated_client, send


def _app(tmp_path, ai_service, name="course-gate.db"):
    return create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "course-gate-test",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / name).as_posix()}",
            "ENABLE_DEMO_INTAKES": True,
            "ENROLLMENT_CURRENT_DATE": date(2026, 9, 24),
            "INVOICE_GENERATION_ENABLED": False,
            "STORAGE_DIR": str(tmp_path / "private"),
            "MASTER_INVOICE_PATH": str(tmp_path / "private" / "master.xlsx"),
        },
        ai_service=ai_service,
    )


def _new_chat(client):
    return client.post("/api/conversations").get_json()["conversation_id"]


def _open_gate(client, conversation_id):
    return send(client, conversation_id, "I want to enrol").get_json()


def test_screenshot_request_only_displays_the_course_gate(tmp_path, fake_ai):
    app = _app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = _new_chat(client)

    payload = _open_gate(client, conversation_id)
    content = payload["message"]["content"]

    assert content.count("Please choose a course") == 1
    assert content.count("2-Day Basic Certificate in Dental Assisting") == 1
    assert content.count("1-Day Dental Infection Control & Sterilisation Workshop") == 1
    assert "S$600 nett" in content
    assert "S$350 nett" in content
    assert "Enrollment Started" not in content
    assert "Awaiting staff confirmation" not in content
    assert not any(
        label in content
        for label in (
            "Full name as shown on NRIC",
            "NRIC/FIN",
            "Date of birth",
            "Email address",
            "Mobile number",
            "Payment method",
            "PayNow amount",
            "SkillsFuture amount",
        )
    )
    assert payload["enrollment"]["course_id"] is None
    assert payload["enrollment"]["selected_intake"] is None
    with app.app_context():
        assert EnrollmentDraft.query.count() == 0
        conversation = db.session.get(Conversation, conversation_id)
        assert conversation.pending_followup == COURSE_SELECTION_FOLLOWUP
        assert conversation.pending_change_data[COURSE_OPTIONS_KEY] == [
            "dental-assisting-basic",
            "dental-infection-control",
        ]


@pytest.mark.parametrize(
    ("choice", "course_id", "present", "absent"),
    [
        ("1", "dental-assisting-basic", "10–11 October 2026", "18 October 2026"),
        ("2", "dental-infection-control", "18 October 2026", "10–11 October 2026"),
        (
            "1-Day Dental Infection Control & Sterilisation Workshop",
            "dental-infection-control",
            "18 October 2026",
            "10–11 October 2026",
        ),
    ],
)
def test_one_course_selection_only_shows_that_courses_intakes(
    tmp_path, fake_ai, choice, course_id, present, absent
):
    app = _app(tmp_path, fake_ai, name=f"one-{course_id}-{len(choice)}.db")
    client = authenticated_client(app)
    conversation_id = _new_chat(client)
    _open_gate(client, conversation_id)

    payload = send(client, conversation_id, choice).get_json()
    content = payload["message"]["content"]

    assert payload["enrollment"]["course_id"] == course_id
    assert present in content
    assert absent not in content
    assert "Enrollment Started" not in content
    assert "Full name as shown on NRIC" not in content
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.intake_id is None
        conversation = db.session.get(Conversation, conversation_id)
        assert conversation.pending_followup == INTAKE_SELECTION_FOLLOWUP


def test_invalid_and_duplicate_course_choices_stay_at_the_gate(tmp_path, fake_ai):
    app = _app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = _new_chat(client)
    _open_gate(client, conversation_id)

    invalid = send(client, conversation_id, "3").get_json()["message"]["content"]
    duplicate = send(client, conversation_id, "1 and 1").get_json()["message"]["content"]

    assert "displayed course numbers" in invalid
    assert "cannot select the same course twice" in duplicate.casefold()
    assert invalid.count("Please choose a course") == 1
    assert duplicate.count("Please choose a course") == 1
    with app.app_context():
        assert EnrollmentDraft.query.count() == 0
        assert EnrollmentCourse.query.count() == 0


def test_valid_intake_starts_participant_details_without_requesting_intake_again(
    tmp_path, fake_ai
):
    app = _app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = _new_chat(client)
    _open_gate(client, conversation_id)
    selected = send(client, conversation_id, "1").get_json()
    assert "Enrollment Started" not in selected["message"]["content"]

    payload = send(client, conversation_id, "1").get_json()
    content = payload["message"]["content"]

    assert content.count("Enrollment Started") == 1
    assert "Full name as shown on NRIC" in content
    assert "NRIC/FIN" in content
    assert "Date of birth" in content
    assert "Email address" in content
    assert "Mobile number" in content
    assert "Preferred intake/date" not in content
    assert not any(
        field in content
        for field in ("Payment method", "PayNow amount", "SkillsFuture amount", "UTAP")
    )
    assert payload["enrollment"]["selected_intake"] is not None


@pytest.mark.parametrize(
    "choice",
    ("both", "1 and 2", "Dental Assisting and Infection Control"),
)
def test_two_courses_require_each_stage_before_participant_details(
    tmp_path, fake_ai, choice
):
    app = _app(tmp_path, fake_ai, name=f"both-{abs(hash(choice))}.db")
    client = authenticated_client(app)
    conversation_id = _new_chat(client)
    _open_gate(client, conversation_id)

    selected = send(client, conversation_id, choice).get_json()
    assert "10–11 October 2026" in selected["message"]["content"]
    assert "18 October 2026" not in selected["message"]["content"]
    assert "Full name as shown on NRIC" not in selected["message"]["content"]

    first = send(client, conversation_id, "1").get_json()
    assert "Now choose an intake for Course 2" in first["message"]["content"]
    assert "18 October 2026" in first["message"]["content"]
    assert "Full name as shown on NRIC" not in first["message"]["content"]
    assert first["enrollment"]["courses"][0]["selected_intake"] is not None
    assert first["enrollment"]["courses"][1]["selected_intake"] is None

    second = send(client, conversation_id, "1").get_json()
    assert "Enrollment Started" in second["message"]["content"]
    assert "Full name as shown on NRIC" in second["message"]["content"]
    assert all(row["selected_intake"] for row in second["enrollment"]["courses"])


def test_faq_and_backend_restart_preserve_course_selection_stage(tmp_path, fake_ai):
    app = _app(tmp_path, fake_ai, name="restart.db")
    owner = authenticated_client(app, email="course-stage@example.com")
    conversation_id = _new_chat(owner)
    _open_gate(owner, conversation_id)

    faq = send(owner, conversation_id, "How much does Dental Assisting cost?").get_json()
    assert faq["routing"]["intent"] == "faq"
    with app.app_context():
        conversation = db.session.get(Conversation, conversation_id)
        assert conversation.pending_followup == COURSE_SELECTION_FOLLOWUP

    restarted = _app(tmp_path, fake_ai, name="restart.db")
    resumed = restarted.test_client()
    login = resumed.post(
        "/api/auth/login",
        json={"email": owner.test_email, "password": owner.test_password},
    ).get_json()
    selected = resumed.post(
        "/api/chat",
        json={"conversation_id": conversation_id, "message": "2"},
        headers={"X-CSRF-Token": login["csrf_token"]},
    ).get_json()
    assert selected["enrollment"]["course_id"] == "dental-infection-control"


def test_new_chat_and_discarded_draft_do_not_inherit_course_state(tmp_path, fake_ai):
    app = _app(tmp_path, fake_ai)
    client = authenticated_client(app)
    first_id = _new_chat(client)
    _open_gate(client, first_id)
    send(client, first_id, "1")

    second_id = _new_chat(client)
    unrelated = send(client, second_id, "2").get_json()
    assert unrelated["enrollment"]["course_id"] is None

    send(client, first_id, "Discard draft")
    send(client, first_id, "Discard draft")
    restarted = _open_gate(client, first_id)
    assert restarted["enrollment"]["course_id"] is None
    assert "Enrollment Started" not in restarted["message"]["content"]
    replacement = send(client, first_id, "2").get_json()
    assert replacement["enrollment"]["course_id"] == "dental-infection-control"


class HallucinatingCourseAI(FakeAIService):
    def extract_enrollment(self, message, context, draft_state, tools=None):
        return EnrollmentExtraction(
            extracted_fields=EnrollmentFields(
                course="2-Day Basic Certificate in Dental Assisting"
            )
        )


def test_model_course_without_current_message_evidence_is_ignored(tmp_path):
    app = _app(tmp_path, HallucinatingCourseAI(), name="hallucination.db")
    client = authenticated_client(app)
    conversation_id = _new_chat(client)

    payload = _open_gate(client, conversation_id)

    assert payload["enrollment"]["course_id"] is None
    with app.app_context():
        assert EnrollmentDraft.query.count() == 0
