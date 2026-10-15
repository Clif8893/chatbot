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
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "1")
    send(client, conversation_id, IDENTITY)
    return conversation_id


def test_participant_details_cannot_bypass_required_intake_selection(tmp_path, fake_ai):
    app = regression_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    response = send(
        client,
        conversation_id,
        "Full name: Alex Lim; NRIC: S1234567D; DOB: 03/09/2026; "
        "Email: alex.manual@example.com; Mobile: 91234567",
    ).get_json()
    content = response["message"]["content"]
    assert "Available Intake Dates" in content
    assert "18–19 November 2026" in content
    assert "reply with the intake number or the dates you prefer" in content
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.full_name is None
        assert draft.date_of_birth is None


def test_approved_intakes_remain_available_when_demo_flag_is_disabled(tmp_path, fake_ai):
    app = regression_app(tmp_path, fake_ai, demos=False)
    client = authenticated_client(app)
    response = send(
        client,
        client.post("/api/conversations").get_json()["conversation_id"],
        "What intake dates are available?",
    ).get_json()
    assert "Available Intake Dates" in response["message"]["content"]
    assert "18–19 November 2026" in response["message"]["content"]
    assert "2 December 2026" in response["message"]["content"]
    assert fake_ai.classify_calls == []
    assert fake_ai.faq_calls == []


def test_database_backed_intake_query_lists_all_approved_choices(tmp_path, fake_ai):
    app = regression_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    response = send(
        client,
        client.post("/api/conversations").get_json()["conversation_id"],
        "Which weekend dates are available?",
    ).get_json()
    content = response["message"]["content"]
    assert "Available Intake Dates" in content
    assert "18–19 November 2026 — Weekdays" in content
    assert "28–29 November 2026 — Weekends" in content
    assert "16–17 December 2026 — Weekdays" in content
    assert "2 December 2026 — Weekday" in content
    assert "9 January 2027 — Weekend" in content
    assert "3 February 2027 — Weekday" in content
    assert "— Open" not in content
    assert "dates are being updated" not in content
    assert fake_ai.classify_calls == []
    assert fake_ai.faq_calls == []
    assert fake_ai.enrollment_calls == []


def test_payment_group_is_ignored_and_not_saved(tmp_path, fake_ai):
    app = regression_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = start_with_identity(client)
    response = send(
        client,
        conversation_id,
        "I will use S$500 SkillsFuture and S$300 PayNow.",
    ).get_json()
    content = response["message"]["content"]
    assert "Needs correction" not in content
    assert "Payment allocation" not in content
    assert "Saved successfully" not in content
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.payment_method is None
        assert draft.skillsfuture_amount is None
        assert draft.paynow_amount is None
        assert "payment_allocation" not in draft.validation_errors


def test_payment_correction_text_does_not_populate_retired_fields(tmp_path, fake_ai):
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
        assert draft.skillsfuture_amount is None
        assert draft.paynow_amount is None
        assert draft.payment_method is None
        assert draft.validation_errors == {}


def test_historical_payment_fields_are_not_changed_by_enrollment_chat(tmp_path, fake_ai):
    app = regression_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = start_with_identity(client)
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        draft.payment_method = "skillsfuture_paynow"
        draft.skillsfuture_amount = 400
        draft.paynow_amount = 200
        db.session.commit()
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
        assert "payment_allocation" not in draft.validation_errors
        assert draft.payment_method == "skillsfuture_paynow"


def prepare_progress_draft(client):
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "1")
    send(
        client,
        conversation_id,
        "Full name: Alex Lim; NRIC: S1234567D; DOB: 03/09/2026; "
        "Email: alex.manual@example.com; Mobile: 91234567",
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
    assert "Payment allocation" not in content
    assert "Still Needed" not in content
    assert "Preferred course intake/date" not in content
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

    restarted = regression_app(tmp_path, fake_ai, name=database_name)
    resumed = restarted.test_client()
    assert resumed.post(
        "/api/auth/login",
        json={"email": client.test_email, "password": client.test_password},
    ).status_code == 200
    history = resumed.get(
        f"/api/conversations/{conversation_id}/messages"
    ).get_json()
    assert history["enrollment"]["selected_intake"]["id"] == "dental-assisting-2026-11-18"
    with restarted.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.full_name == "Alex Lim"
        assert draft.nric == "S1234567D"
        assert draft.skillsfuture_amount is None
        assert draft.paynow_amount is None
