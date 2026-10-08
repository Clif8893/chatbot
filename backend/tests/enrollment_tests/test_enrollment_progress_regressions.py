"""Manual-test regressions for corrections, payment atomicity and progress replies."""

from datetime import date

import pytest

from backend.app import create_app
from backend.extensions import db
from backend.models import Conversation, EnrollmentDraft
from backend.services.enrollment.enrollment_service import EnrollmentService
from backend.tools.enrollment import build_enrollment_tools
from backend.tests.conftest import authenticated_client, send


TODAY = date(2026, 9, 21)
IDENTITY = (
    "Full name: Alex Lim; NRIC/FIN: S1234567D; "
    "Date of birth: 15 May 2000; Email: alex.manual@example.com; "
    "Mobile number: 91234567"
)


def regression_app(tmp_path, fake_ai, *, demos=True, name="regressions.db"):
    return create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "progress-regression-secret",
            "OPENAI_API_KEY": "",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / name).as_posix()}",
            "ENABLE_DEMO_INTAKES": demos,
            "ENROLLMENT_CURRENT_DATE": TODAY,
        },
        ai_service=fake_ai,
    )


def start_with_identity(client):
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    send(client, conversation_id, "I want to enroll")
    send(client, conversation_id, "Yes, this course")
    send(client, conversation_id, "1")
    send(client, conversation_id, IDENTITY)
    return conversation_id


def test_invalid_dob_is_only_a_correction_and_missing_intake_is_separate(tmp_path, fake_ai):
    app = regression_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    send(client, conversation_id, "I want to enroll")
    response = send(
        client,
        conversation_id,
        "Full name: Alex Lim; NRIC: S1234567D; DOB: 03/09/2026; "
        "Email: alex.manual@example.com; Mobile: 91234567",
    ).get_json()
    content = response["message"]["content"]
    correction = content.split("Still needed", 1)[0]
    assert "Needs correction" in correction
    assert "Date of birth" in correction
    assert "Preferred course intake/date" not in correction
    assert "Still needed" in content
    assert "Preferred course intake/date" in content
    assert "Available Intake Dates" in content
    assert "10–11 October 2026" in content
    assert "dates are being updated" not in content


def test_no_intakes_fallback_only_when_database_has_no_selectable_rows(tmp_path, fake_ai):
    app = regression_app(tmp_path, fake_ai, demos=False)
    client = authenticated_client(app)
    response = send(
        client,
        client.post("/api/conversations").get_json()["conversation_id"],
        "What intake dates are available?",
    ).get_json()
    assert "dates are being updated" in response["message"]["content"]
    assert "Available Intake Dates" not in response["message"]["content"]
    assert fake_ai.classify_calls == []
    assert fake_ai.faq_calls == []


def test_database_backed_intake_query_lists_all_demo_choices(tmp_path, fake_ai):
    app = regression_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    response = send(
        client,
        client.post("/api/conversations").get_json()["conversation_id"],
        "Which weekend dates are available?",
    ).get_json()
    content = response["message"]["content"]
    assert "Available Intake Dates" in content
    assert "10–11 October 2026 — Weekend — Open" in content
    assert "4–5 November 2026 — Weekday — Open" in content
    assert "21–22 November 2026 — Weekend — Open" in content
    assert "dates are being updated" not in content
    assert fake_ai.classify_calls == []
    assert fake_ai.faq_calls == []
    assert fake_ai.enrollment_calls == []


def test_invalid_payment_group_is_not_saved_or_reported_as_saved(tmp_path, fake_ai):
    app = regression_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = start_with_identity(client)
    response = send(
        client,
        conversation_id,
        "I will use S$500 SkillsFuture and S$300 PayNow.",
    ).get_json()
    content = response["message"]["content"]
    assert "Needs correction" in content
    assert "total S$800" in content
    assert "S$200 too much" in content
    assert "Saved successfully" not in content
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.payment_method is None
        assert draft.skillsfuture_amount is None
        assert draft.paynow_amount is None
        assert "payment_allocation" in draft.validation_errors


def test_corrected_payment_group_is_saved_atomically(tmp_path, fake_ai):
    app = regression_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = start_with_identity(client)
    send(client, conversation_id, "S$500 SkillsFuture and S$300 PayNow")
    corrected = send(
        client,
        conversation_id,
        "Change it to S$400 SkillsFuture and S$200 PayNow.",
    ).get_json()
    assert corrected["enrollment"]["status"] == "awaiting_confirmation"
    assert "Please review your enrolment" in corrected["message"]["content"]
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert str(draft.skillsfuture_amount) == "400.00"
        assert str(draft.paynow_amount) == "200.00"
        assert draft.payment_method == "skillsfuture_paynow"
        assert draft.validation_errors == {}


def test_previous_valid_payment_survives_rejected_replacement(tmp_path, fake_ai):
    app = regression_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = start_with_identity(client)
    send(client, conversation_id, "S$400 SkillsFuture and S$200 PayNow")
    rejected = send(
        client,
        conversation_id,
        "Change it to S$500 SkillsFuture and S$300 PayNow",
    ).get_json()
    assert "Saved successfully" not in rejected["message"]["content"]
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert str(draft.skillsfuture_amount) == "400.00"
        assert str(draft.paynow_amount) == "200.00"
        assert "payment_allocation" in draft.validation_errors
    send(client, conversation_id, "Change it to S$400 SkillsFuture and S$200 PayNow")
    with app.app_context():
        assert EnrollmentDraft.query.one().validation_errors == {}


def prepare_progress_draft(client):
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    send(client, conversation_id, "I want to enroll")
    send(
        client,
        conversation_id,
        "Full name: Alex Lim; NRIC: S1234567D; DOB: 03/09/2026; "
        "Email: alex.manual@example.com; Mobile: 91234567; "
        "S$500 SkillsFuture and S$300 PayNow",
    )
    return conversation_id


def test_progress_reply_uses_database_state_and_masks_nric_without_ai(tmp_path, fake_ai):
    app = regression_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = prepare_progress_draft(client)
    calls_before = (
        len(fake_ai.classify_calls), len(fake_ai.faq_calls), len(fake_ai.enrollment_calls)
    )
    response = send(
        client, conversation_id, "What information have I already provided?"
    ).get_json()
    content = response["message"]["content"]
    assert "Hi Alex" in content
    assert "Saved Information" in content
    assert "Alex Lim" in content
    assert "S*******D" in content
    assert "S1234567D" not in content
    assert "alex.manual@example.com" in content
    assert "Needs Correction" in content
    assert "Date of birth" in content
    assert "Payment allocation" in content
    assert "Still Needed" in content
    assert "Preferred course intake/date" in content
    assert "requires the corrections shown above" in content
    assert "already recorded" not in content
    assert "confirmed information" not in content
    assert calls_before == (
        len(fake_ai.classify_calls), len(fake_ai.faq_calls), len(fake_ai.enrollment_calls)
    )


@pytest.mark.parametrize(
    "question",
    [
        "What information do you still need from me?",
        "What details are missing?",
        "Show my enrollment progress.",
        "What is left?",
        "Which fields have I completed?",
        "What do I need to correct?",
        "Have I completed registration?",
        "What else you need?",
        "What details I give already?",
    ],
)
def test_progress_paraphrases_and_singlish_are_deterministic(tmp_path, fake_ai, question):
    app = regression_app(tmp_path, fake_ai, name=f"progress-{abs(hash(question))}.db")
    client = authenticated_client(app)
    conversation_id = prepare_progress_draft(client)
    before = len(fake_ai.classify_calls) + len(fake_ai.faq_calls) + len(fake_ai.enrollment_calls)
    response = send(client, conversation_id, question).get_json()
    after = len(fake_ai.classify_calls) + len(fake_ai.faq_calls) + len(fake_ai.enrollment_calls)
    assert "current enrollment progress" in response["message"]["content"]
    assert after == before


def test_confirmed_progress_uses_confirmed_status_wording(tmp_path, fake_ai):
    app = regression_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = start_with_identity(client)
    send(client, conversation_id, "S$400 SkillsFuture and S$200 PayNow")
    send(client, conversation_id, "Confirm")
    response = send(client, conversation_id, "Show my enrollment progress").get_json()
    assert "Your enrollment has been confirmed." in response["message"]["content"]
    assert "still required" not in response["message"]["content"].casefold()


def test_progress_is_owner_scoped_and_faq_keeps_draft(tmp_path, fake_ai):
    app = regression_app(tmp_path, fake_ai)
    owner = authenticated_client(app, email="progress-owner@example.com")
    conversation_id = prepare_progress_draft(owner)
    faq = send(owner, conversation_id, "What is the course fee?")
    assert faq.status_code == 200
    other = authenticated_client(app, email="progress-other@example.com")
    assert send(other, conversation_id, "What details are missing?").status_code == 404
    resumed = send(owner, conversation_id, "What details are missing?").get_json()
    assert "S*******D" in resumed["message"]["content"]
    assert "Date of birth" in resumed["message"]["content"]


def test_progress_history_and_draft_tool_never_return_full_nric(tmp_path, fake_ai):
    app = regression_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = prepare_progress_draft(client)
    send(client, conversation_id, "What information have I already provided?")
    history = client.get(
        f"/api/conversations/{conversation_id}/messages"
    ).get_json()
    assert "S1234567D" not in str(history)
    assert "S*******D" in str(history)

    with app.app_context():
        conversation = db.session.get(Conversation, conversation_id)
        tools = build_enrollment_tools(
            app.extensions["repository"],
            app.extensions["catalogue"],
            conversation,
            20,
            EnrollmentService.missing_fields,
        )
        result = tools[2].run()
        assert "S1234567D" not in result.model_dump_json()
        assert result.safe_values["nric"] == "S*******D"


def test_valid_fields_and_selected_intake_survive_backend_restart(tmp_path, fake_ai):
    database_name = "progress-restart.db"
    app = regression_app(tmp_path, fake_ai, name=database_name)
    client = authenticated_client(app, email="progress-restart@example.com")
    conversation_id = start_with_identity(client)
    send(client, conversation_id, "S$400 SkillsFuture and S$200 PayNow")

    restarted = regression_app(tmp_path, fake_ai, name=database_name)
    resumed = restarted.test_client()
    assert resumed.post(
        "/api/auth/login",
        json={"email": client.test_email, "password": client.test_password},
    ).status_code == 200
    history = resumed.get(
        f"/api/conversations/{conversation_id}/messages"
    ).get_json()
    assert history["enrollment"]["selected_intake"]["id"] == "demo-2026-10-10"
    with restarted.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.full_name == "Alex Lim"
        assert draft.nric == "S1234567D"
        assert str(draft.skillsfuture_amount) == "400.00"
        assert str(draft.paynow_amount) == "200.00"
