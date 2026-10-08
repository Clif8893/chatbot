"""Regressions for draft lifecycle and context-aware enrollment routing."""

from datetime import date
from decimal import Decimal

import pytest

from backend.app import create_app
from backend.extensions import db
from backend.models import Conversation, EnrollmentDraft
from backend.tests.conftest import authenticated_client, send


COURSE = "2-Day Basic Certificate in Dental Assisting"
OLD_NRIC = "S1234567D"


def context_app(tmp_path, fake_ai, name="context.db"):
    return create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "context-state-test-secret",
            "OPENAI_API_KEY": "",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / name).as_posix()}",
            "ENABLE_DEMO_INTAKES": True,
            "ENROLLMENT_CURRENT_DATE": date(2026, 9, 23),
        },
        ai_service=fake_ai,
    )


def add_active_draft(app, conversation_id, **overrides):
    values = {
        "course": COURSE,
        "course_fee": Decimal("600"),
        "email": "test.student@example.com",
        "intake_id": "demo-2026-11-21",
        "preferred_intake_date": date(2026, 11, 21),
        "intake_start_date": date(2026, 11, 21),
        "intake_end_date": date(2026, 11, 22),
        "intake_is_demo": True,
        "status": "collecting",
    }
    values.update(overrides)
    with app.app_context():
        conversation = db.session.get(Conversation, conversation_id)
        conversation.active_intent = "enrollment"
        draft = EnrollmentDraft(conversation=conversation, **values)
        db.session.add(draft)
        db.session.commit()
        return draft.id


def test_discard_archives_draft_clears_state_and_restart_is_fresh(tmp_path, fake_ai):
    app = context_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    old_draft_id = add_active_draft(
        app,
        conversation_id,
        full_name="Old Student",
        nric=OLD_NRIC,
        date_of_birth=date(2000, 5, 15),
        mobile_number="91234567",
        payment_method="skillsfuture_paynow",
        skillsfuture_amount=Decimal("400"),
        paynow_amount=Decimal("200"),
        pending_corrections=["email"],
        validation_errors={"email": "Invalid email"},
    )
    with app.app_context():
        conversation = db.session.get(Conversation, conversation_id)
        conversation.pending_followup = "intake_selection"
        conversation.pending_intake_options = [
            "demo-2026-10-10",
            "demo-2026-11-04",
            "demo-2026-11-21",
        ]
        db.session.commit()

    send(client, conversation_id, "I changed my mind")
    discarded = send(client, conversation_id, "Discard draft").get_json()
    assert discarded["enrollment"]["status"] is None

    with app.app_context():
        current = db.session.get(Conversation, conversation_id)
        assert current.enrollment_draft is None
        assert current.active_intent is None
        assert current.pending_followup is None
        assert current.pending_action is None
        assert current.pending_enrollment_id is None
        assert current.pending_target_intake_id is None
        assert current.pending_source_intake_id is None
        assert current.pending_intake_options is None
        archived = db.session.get(EnrollmentDraft, old_draft_id)
        assert archived.status == "discarded"
        assert archived.conversation.status == "archived"
        assert archived.pending_corrections == []
        assert archived.validation_errors == {}

    listed_ids = {
        item["conversation_id"]
        for item in client.get("/api/conversations").get_json()["conversations"]
    }
    assert conversation_id in listed_ids
    assert len(listed_ids) == 1

    restarted = send(client, conversation_id, "I want to enrol").get_json()
    assert "Old Student" not in restarted["message"]["content"]
    assert OLD_NRIC not in restarted["message"]["content"]
    assert "Old Student" not in fake_ai.classify_calls[-1]["context"]
    assert "test.student@example.com" not in fake_ai.classify_calls[-1]["context"]
    assert "Old Student" not in fake_ai.enrollment_calls[-1]["context"]
    assert "test.student@example.com" not in fake_ai.enrollment_calls[-1]["context"]
    with app.app_context():
        fresh = db.session.get(Conversation, conversation_id).enrollment_draft
        assert fresh is not None and fresh.id != old_draft_id
        for field in (
            "full_name",
            "nric",
            "date_of_birth",
            "email",
            "mobile_number",
            "preferred_intake_date",
            "payment_method",
            "skillsfuture_amount",
            "paynow_amount",
        ):
            assert getattr(fresh, field) is None


def test_repeated_discard_is_idempotent_and_keep_preserves_draft(tmp_path, fake_ai):
    app = context_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    kept_id = client.post("/api/conversations").get_json()["conversation_id"]
    draft_id = add_active_draft(app, kept_id, full_name="Keep Student")

    send(client, kept_id, "I changed my mind")
    kept = send(client, kept_id, "Keep draft").get_json()
    assert "still available" in kept["message"]["content"]
    with app.app_context():
        assert db.session.get(Conversation, kept_id).enrollment_draft.id == draft_id
        assert db.session.get(EnrollmentDraft, draft_id).full_name == "Keep Student"

    send(client, kept_id, "I changed my mind")
    send(client, kept_id, "Discard draft")
    repeated = send(client, kept_id, "Discard draft").get_json()
    assert "No active enrollment draft" in repeated["message"]["content"]
    with app.app_context():
        assert db.session.get(EnrollmentDraft, draft_id).status == "discarded"
        assert EnrollmentDraft.query.count() == 1


def test_confirmed_enrollment_cannot_be_discarded(tmp_path, fake_ai):
    app = context_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    draft_id = add_active_draft(
        app,
        conversation_id,
        full_name="Confirmed Student",
        status="confirmed",
    )
    with app.app_context():
        draft = db.session.get(EnrollmentDraft, draft_id)
        draft.confirmed_at = draft.updated_at
        db.session.commit()

    response = send(client, conversation_id, "Discard draft").get_json()
    assert "confirmed enrollment cannot be removed" in response["message"]["content"]
    with app.app_context():
        assert db.session.get(EnrollmentDraft, draft_id).status == "confirmed"
        assert db.session.get(Conversation, conversation_id).enrollment_draft.id == draft_id


@pytest.mark.parametrize(
    "question",
    [
        "What other details do you need?",
        "What information have I not provided?",
        "Anything else?",
        "Continue my enrollment.",
        "Can we continue?",
        "What's missing ah?",
        "So now need what?",
        "Continue from where we stopped.",
    ],
)
def test_progress_variants_are_deterministic_and_draft_grounded(
    tmp_path, fake_ai, question
):
    app = context_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    add_active_draft(app, conversation_id)
    before = len(fake_ai.classify_calls)

    response = send(client, conversation_id, question).get_json()
    content = response["message"]["content"]
    assert "Saved Information" in content
    assert "test.student@example.com" in content
    assert "Still Needed" in content
    assert "Full name" in content
    assert "Payment method" not in content
    assert "SkillsFuture amount" not in content
    assert "PayNow amount" not in content
    assert "Course Fee" not in content
    assert "Mid-Career SkillsFuture" not in content
    assert "image_url" not in response["message"]
    assert len(fake_ai.classify_calls) == before
    assert len(fake_ai.faq_calls) == 0


def test_repeated_progress_is_consistent_and_masks_nric(tmp_path, fake_ai):
    app = context_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    add_active_draft(app, conversation_id, full_name="Test Student", nric=OLD_NRIC)

    first = send(client, conversation_id, "What else do you need?").get_json()
    second = send(client, conversation_id, "What else do you need?").get_json()
    assert first["message"]["content"] == second["message"]["content"]
    assert OLD_NRIC not in first["message"]["content"]
    assert "S*******D" in first["message"]["content"]
    assert len(fake_ai.classify_calls) == 0


@pytest.mark.parametrize("payment_method", [None, "paynow", "skillsfuture_paynow"])
def test_progress_never_surfaces_historical_payment_fields(
    tmp_path, fake_ai, payment_method
):
    app = context_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    add_active_draft(app, conversation_id, payment_method=payment_method)

    content = send(client, conversation_id, "What's left?").get_json()["message"][
        "content"
    ]
    for label in ("Payment method", "SkillsFuture amount", "PayNow amount"):
        assert label not in content


def test_fee_interruption_preserves_draft_and_continue_resumes_without_ai(
    tmp_path, fake_ai
):
    app = context_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    draft_id = add_active_draft(app, conversation_id, full_name="Test Student")

    faq = send(client, conversation_id, "Wait, how much is the course?").get_json()
    assert "S$600 nett" in faq["message"]["content"]
    assert "continue with your remaining enrollment details" in faq["message"]["content"]
    classify_after_faq = len(fake_ai.classify_calls)

    resumed = send(client, conversation_id, "Okay, continue.").get_json()
    assert "current enrollment progress" in resumed["message"]["content"]
    assert "course fee" not in resumed["message"]["content"].casefold()
    assert len(fake_ai.classify_calls) == classify_after_faq
    with app.app_context():
        draft = db.session.get(EnrollmentDraft, draft_id)
        assert draft.full_name == "Test Student"
        assert draft.email == "test.student@example.com"


def test_plain_name_uses_latest_assistant_question(tmp_path, fake_ai):
    app = context_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    send(client, conversation_id, "I want to enrol")
    send(client, conversation_id, "1")
    send(client, conversation_id, "The last weekend one")
    reply = send(client, conversation_id, "Test Student").get_json()

    assert reply["routing"]["intent"] == "enrollment"
    with app.app_context():
        assert (
            db.session.get(Conversation, conversation_id).enrollment_draft.full_name
            == "Test Student"
        )


def test_intake_change_uses_persisted_displayed_choices(tmp_path, fake_ai):
    app = context_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    send(client, conversation_id, "I want to enrol")
    send(client, conversation_id, "1")
    send(client, conversation_id, "The last weekend one")

    with app.app_context():
        conversation = db.session.get(Conversation, conversation_id)
        assert conversation.enrollment_draft.intake_id == "demo-2026-11-21"
        assert conversation.pending_intake_options == [
            "demo-2026-10-10",
            "demo-2026-11-04",
            "demo-2026-11-21",
        ]

    send(client, conversation_id, "Actually use the second one")
    with app.app_context():
        draft = db.session.get(Conversation, conversation_id).enrollment_draft
        assert draft.intake_id == "demo-2026-11-04"
        assert draft.preferred_intake_date == date(2026, 11, 4)


def test_ambiguous_fallback_receives_masked_scoped_context(tmp_path, fake_ai):
    app = context_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    add_active_draft(
        app,
        conversation_id,
        full_name="Test Student",
        nric=OLD_NRIC,
        date_of_birth=date(2000, 5, 15),
        mobile_number="91234567",
    )
    send(
        client,
        conversation_id,
        "My name is Test Student; NRIC is S1234567D; "
        "email is test.student@example.com; mobile is 91234567; DOB is 15 May 2000",
    )
    fake_ai.classify_calls.clear()

    send(client, conversation_id, "I am undecided")
    call = fake_ai.classify_calls[-1]
    assert OLD_NRIC not in call["context"]
    assert OLD_NRIC not in call["message"]
    for sensitive_value in (
        "Test Student",
        "test.student@example.com",
        "91234567",
        "15 May 2000",
    ):
        assert sensitive_value not in call["context"]
    assert call["active_intent"] == "enrollment"
    assert call["draft_summary"]["status"] == "awaiting_confirmation"
    assert "nric" in call["draft_summary"]["saved_fields"]
    assert OLD_NRIC not in str(call["draft_summary"])
