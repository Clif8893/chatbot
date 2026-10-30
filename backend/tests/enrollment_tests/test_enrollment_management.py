"""Participant cancellation, rescheduling and deterministic status regressions."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal

import pytest
from types import SimpleNamespace
from openpyxl import load_workbook

from backend.app import create_app
from backend.extensions import db
from backend.models import Conversation, EnrollmentDraft, utc_now
from backend.models.finance import CreditNoteRequest, FinanceCase, MasterInvoiceExport
from backend.models.operations import AuditEvent, CourseDate, StaffEscalation
from backend.services.enrollment.enrollment_management_service import (
    AWAITING_CANCELLATION,
    AWAITING_CHANGE_SELECTION,
    AWAITING_COURSE_CHANGE_INTAKE,
    AWAITING_COURSE_CHANGE_SELECTION,
    AWAITING_RESCHEDULE_CONFIRMATION,
    AWAITING_RESCHEDULE_SELECTION,
)
from backend.services.master_invoice_service import SHEET_NAME
from backend.tests.conftest import authenticated_client, send


TODAY = date(2026, 9, 21)
COURSE = "Basic Certificate in Dental Assisting"


def management_app(tmp_path, fake_ai, database_name="management.db"):
    return create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "management-test-secret",
            "OPENAI_API_KEY": "",
            "SQLALCHEMY_DATABASE_URI": (
                f"sqlite:///{(tmp_path / database_name).as_posix()}"
            ),
            "ENABLE_DEMO_INTAKES": True,
            "ENROLLMENT_CURRENT_DATE": TODAY,
        },
        ai_service=fake_ai,
    )


def add_confirmed(app, client, intake_id="dental-assisting-2026-11-18", *, name="Test Student"):
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    with app.app_context():
        conversation = db.session.get(Conversation, conversation_id)
        intake = db.session.get(CourseDate, intake_id)
        draft = EnrollmentDraft(
            conversation=conversation,
            course=COURSE,
            course_fee=Decimal("600"),
            full_name=name,
            nric="S1234567D",
            date_of_birth=date(2000, 5, 15),
            email="test.student@example.com",
            mobile_number="91234567",
            payment_method="skillsfuture_paynow",
            skillsfuture_amount=Decimal("400"),
            paynow_amount=Decimal("200"),
            intake_id=intake.id,
            preferred_intake_date=intake.date,
            intake_start_date=intake.date,
            intake_end_date=intake.end_date,
            intake_is_demo=intake.is_demo,
            status="confirmed",
            validation_status="valid",
            confirmed_at=utc_now(),
        )
        db.session.add(draft)
        db.session.flush()
        app.extensions["intakes"].allocate(draft)
        db.session.commit()
        return conversation_id, draft.id


def test_cancellation_requires_explicit_confirmation_and_can_be_kept(tmp_path, fake_ai):
    app = management_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id, draft_id = add_confirmed(app, client)
    before = len(fake_ai.classify_calls)

    request = send(client, conversation_id, "Cancel my enrollment").get_json()
    assert "Confirm cancellation" in request["message"]["content"]
    assert "S1234567D" not in request["message"]["content"]
    assert len(fake_ai.classify_calls) == before + 1
    assert request["routing"]["handler"] == "deterministic"
    assert request["routing"]["rule"] == "cancellation_confirmation_required"
    with app.app_context():
        draft = db.session.get(EnrollmentDraft, draft_id)
        conversation = db.session.get(Conversation, conversation_id)
        assert draft.status == "confirmed"
        assert conversation.pending_action == AWAITING_CANCELLATION
        assert db.session.get(CourseDate, draft.intake_id).participant_count == 1

    kept = send(client, conversation_id, "Keep enrollment").get_json()
    assert "kept unchanged" in kept["message"]["content"]
    with app.app_context():
        assert db.session.get(EnrollmentDraft, draft_id).status == "confirmed"
        assert db.session.get(Conversation, conversation_id).pending_action is None


def test_confirmed_cancellation_is_idempotent_releases_once_and_updates_excel(
    tmp_path, fake_ai
):
    app = management_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id, draft_id = add_confirmed(app, client)
    with app.app_context():
        service = app.extensions["master_invoice"]
        assert service.export_confirmed_enrollment(draft_id)
        invoice_number = MasterInvoiceExport.query.one().invoice_number

    send(client, conversation_id, "I want to withdraw")
    confirmed = send(client, conversation_id, "Confirm cancellation").get_json()
    assert "has been cancelled" in confirmed["message"]["content"]
    repeated = send(client, conversation_id, "Confirm cancellation").get_json()
    assert "no cancellation" in repeated["message"]["content"].casefold()
    already = send(client, conversation_id, "Cancel my enrollment").get_json()
    assert "already cancelled" in already["message"]["content"].casefold()

    with app.app_context():
        draft = db.session.get(EnrollmentDraft, draft_id)
        intake = db.session.get(CourseDate, "dental-assisting-2026-11-18")
        record = MasterInvoiceExport.query.one()
        assert draft.status == "cancelled"
        assert draft.cancelled_at is not None
        assert draft.allocated_intake_id is None
        assert intake.participant_count == 0
        assert record.invoice_number == invoice_number
        assert record.status == "removed_after_cancellation"
        assert MasterInvoiceExport.query.count() == 1
        assert AuditEvent.query.filter_by(action="enrollment_cancelled").count() == 1
        workbook_path = app.extensions["master_invoice"].workbook_path

    workbook = load_workbook(workbook_path)
    try:
        sheet = workbook[SHEET_NAME]
        assert sheet.max_row == 1
        assert sheet.cell(2, 23).value is None
    finally:
        workbook.close()


def test_yes_only_cancels_when_cancellation_is_pending(tmp_path, fake_ai):
    app = management_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id, draft_id = add_confirmed(app, client)
    send(client, conversation_id, "yes")
    with app.app_context():
        assert db.session.get(EnrollmentDraft, draft_id).status == "confirmed"
    send(client, conversation_id, "Please remove me from the course")
    send(client, conversation_id, "yes")
    with app.app_context():
        assert db.session.get(EnrollmentDraft, draft_id).status == "cancelled"


def test_chat_invoice_resend_requires_exact_confirmation(tmp_path, fake_ai):
    app = management_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id, _ = add_confirmed(app, client)
    calls = []

    class FakeInvoiceEmail:
        def participant_resend(self, draft):
            calls.append(draft.email)
            return SimpleNamespace(id="queued-test-delivery")

    app.extensions["enrollment_management"].invoice_email = FakeInvoiceEmail()
    requested = send(client, conversation_id, "Resend my invoice").get_json()
    assert "Confirm resend" in requested["message"]["content"]
    assert "t***@example.com" in requested["message"]["content"]
    kept = send(client, conversation_id, "Keep invoice").get_json()
    assert "will not be resent" in kept["message"]["content"]
    assert calls == []

    send(client, conversation_id, "Email my invoice again")
    confirmed = send(client, conversation_id, "Confirm resend").get_json()
    assert "queued for delivery" in confirmed["message"]["content"]
    assert calls == ["test.student@example.com"]


def test_natural_invoice_resend_pronoun_still_requires_confirmation(tmp_path, fake_ai):
    app = management_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id, _ = add_confirmed(app, client)
    calls = []

    class FakeInvoiceEmail:
        def participant_resend(self, draft):
            calls.append(draft.email)
            return SimpleNamespace(id="queued-test-delivery")

    app.extensions["enrollment_management"].invoice_email = FakeInvoiceEmail()
    requested = send(client, conversation_id, "Can you resend it?").get_json()

    assert "Confirm resend" in requested["message"]["content"]
    assert calls == []


def test_orphaned_change_and_resend_confirmations_do_not_reach_ai(tmp_path, fake_ai):
    app = management_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id, _ = add_confirmed(app, client)

    for command in ("Confirm resend", "Confirm change"):
        response = send(client, conversation_id, command).get_json()
        assert "no cancellation" in response["message"]["content"]


def test_confirmed_email_change_requires_confirmation_and_preserves_other_fields(
    tmp_path, fake_ai
):
    app = management_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id, draft_id = add_confirmed(app, client)
    requested = send(
        client, conversation_id, "Change my email to updated.student@example.com"
    ).get_json()
    assert "Confirm change" in requested["message"]["content"]
    with app.app_context():
        draft = db.session.get(EnrollmentDraft, draft_id)
        assert draft.email == "test.student@example.com"
        original_nric = draft.nric
        original_intake = draft.intake_id
    confirmed = send(client, conversation_id, "Confirm change").get_json()
    assert "updated successfully" in confirmed["message"]["content"]
    with app.app_context():
        draft = db.session.get(EnrollmentDraft, draft_id)
        assert draft.email == "updated.student@example.com"
        assert draft.nric == original_nric
        assert draft.intake_id == original_intake
        assert draft.status == "confirmed"


def test_confirmed_course_change_revalidates_intake_fee_and_capacity(tmp_path, fake_ai):
    app = management_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id, draft_id = add_confirmed(app, client)

    requested = send(
        client, conversation_id, "Change my course to front desk operations"
    ).get_json()
    assert "Available Intake Dates" in requested["message"]["content"]
    confirmation = send(client, conversation_id, "1").get_json()
    assert "Confirm change" in confirmation["message"]["content"]
    assert "PayNow" not in confirmation["message"]["content"]

    with app.app_context():
        draft = db.session.get(EnrollmentDraft, draft_id)
        assert draft.course == COURSE
        assert draft.allocated_intake_id == "dental-assisting-2026-11-18"

    completed = send(client, conversation_id, "Confirm change").get_json()
    assert "Dental Clinic Front Desk Operations" in completed["message"]["content"]
    with app.app_context():
        draft = db.session.get(EnrollmentDraft, draft_id)
        assert draft.course == "Certificate in Dental Clinic Front Desk Operations"
        assert draft.course_fee == Decimal("450")
        assert draft.intake_id == "front-desk-operations-2026-12-02"
        assert draft.allocated_intake_id == "front-desk-operations-2026-12-02"
        assert draft.payment_method == "skillsfuture_paynow"
        assert draft.skillsfuture_amount == Decimal("400")
        assert draft.paynow_amount == Decimal("200")
        assert db.session.get(CourseDate, "dental-assisting-2026-11-18").participant_count == 0
        assert db.session.get(CourseDate, "front-desk-operations-2026-12-02").participant_count == 1


def test_unconfirmed_draft_is_discarded_only_after_separate_confirmation(tmp_path, fake_ai):
    app = management_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    with app.app_context():
        conversation = db.session.get(Conversation, conversation_id)
        db.session.add(EnrollmentDraft(conversation=conversation, full_name="Draft Student"))
        db.session.commit()
    request = send(client, conversation_id, "I changed my mind").get_json()
    assert "still a draft" in request["message"]["content"]
    with app.app_context():
        assert EnrollmentDraft.query.one().status == "collecting"
    send(client, conversation_id, "Discard draft")
    with app.app_context():
        assert EnrollmentDraft.query.one().status == "discarded"


def test_past_course_cancellation_escalates_without_changing_enrollment(tmp_path, fake_ai):
    app = management_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id, draft_id = add_confirmed(app, client)
    with app.app_context():
        intake = db.session.get(CourseDate, "dental-assisting-2026-11-18")
        intake.date = date(2026, 9, 20)
        intake.end_date = date(2026, 9, 21)
        draft = db.session.get(EnrollmentDraft, draft_id)
        draft.preferred_intake_date = intake.date
        draft.intake_start_date = intake.date
        draft.intake_end_date = intake.end_date
        db.session.commit()
    response = send(client, conversation_id, "Cancel my enrollment").get_json()
    assert "staff" in response["message"]["content"].casefold()
    with app.app_context():
        assert db.session.get(EnrollmentDraft, draft_id).status == "confirmed"
        assert StaffEscalation.query.filter_by(category="cancellation_review").count() == 1


def test_paid_cancellation_requires_refund_review_and_issues_no_credit_note(tmp_path, fake_ai):
    app = management_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id, draft_id = add_confirmed(app, client)
    with app.app_context():
        db.session.add(FinanceCase(enrollment_id=draft_id, status="Paid"))
        db.session.commit()
    send(client, conversation_id, "Cancel my October intake")
    response = send(client, conversation_id, "Confirm cancellation").get_json()
    content = response["message"]["content"].casefold()
    assert "staff confirmation" in content
    assert "no refund" in content
    with app.app_context():
        draft = db.session.get(EnrollmentDraft, draft_id)
        assert draft.refund_review_required is True
        assert CreditNoteRequest.query.count() == 0
        assert StaffEscalation.query.filter_by(category="payment_refund_review").count() == 1


def test_cross_user_cannot_cancel_or_view_another_enrollment(tmp_path, fake_ai):
    app = management_app(tmp_path, fake_ai)
    owner = authenticated_client(app, email="owner-management@example.com")
    other = authenticated_client(app, email="other-management@example.com")
    conversation_id, draft_id = add_confirmed(app, owner)
    rejected = send(other, conversation_id, "Cancel my enrollment")
    assert rejected.status_code == 404
    other_conversation = other.post("/api/conversations").get_json()["conversation_id"]
    status = send(other, other_conversation, "What's my status ah?").get_json()
    assert "No saved enrollment" in status["message"]["content"]
    with app.app_context():
        assert db.session.get(EnrollmentDraft, draft_id).status == "confirmed"


def test_exact_change_menu_and_intake_flow_survives_course_change(
    tmp_path, fake_ai
):
    app = management_app(tmp_path, fake_ai, "exact-change-flow.db")
    client = authenticated_client(app)
    conversation_id, draft_id = add_confirmed(app, client)
    with app.app_context():
        draft = db.session.get(EnrollmentDraft, draft_id)
        draft.friendly_reference = "ENR-2026-000777"
        app.extensions["master_invoice"].export_confirmed_enrollment(draft_id)
        db.session.commit()

    alias = send(client, conversation_id, "Reschedule enrollment.").get_json()
    assert "That date is not one of" not in alias["message"]["content"]
    assert "28–29 November 2026" in alias["message"]["content"]
    assert "18–19 November 2026" not in alias["message"]["content"]
    send(client, conversation_id, "Keep enrollment")

    with app.app_context():
        conversation = db.session.get(Conversation, conversation_id)
        conversation.pending_intake_options = ["dental-assisting-2026-11-18"]
        db.session.commit()

    menu = send(client, conversation_id, "Change enrollment.").get_json()
    assert "1. Participant details" in menu["message"]["content"]
    with app.app_context():
        conversation = db.session.get(Conversation, conversation_id)
        assert conversation.pending_action == AWAITING_CHANGE_SELECTION
        assert conversation.pending_intake_options is None

    alternatives = send(client, conversation_id, "3").get_json()
    assert "28–29 November 2026" in alternatives["message"]["content"]
    assert "18–19 November 2026" not in alternatives["message"]["content"]
    summary = send(client, conversation_id, "1").get_json()
    content = summary["message"]["content"]
    assert f"- Course: {COURSE}" in content
    assert "- Current intake: 18–19 November 2026" in content
    assert "- New intake: 28–29 November 2026" in content
    assert "Confirm change" in content and "Keep enrollment" in content

    changed = send(client, conversation_id, "Confirm change").get_json()
    assert "28–29 November 2026" in changed["message"]["content"]
    with app.app_context():
        draft = db.session.get(EnrollmentDraft, draft_id)
        conversation = db.session.get(Conversation, conversation_id)
        assert draft.friendly_reference == "ENR-2026-000777"
        assert draft.intake_id == "dental-assisting-2026-11-28"
        assert db.session.get(CourseDate, "dental-assisting-2026-11-18").participant_count == 0
        assert db.session.get(CourseDate, "dental-assisting-2026-11-28").participant_count == 1
        assert MasterInvoiceExport.query.filter_by(enrollment_id=draft_id).count() == 1
        assert conversation.pending_action is None
        assert conversation.pending_intake_options is None
        assert conversation.pending_change_data is None

    send(client, conversation_id, "Change enrollment")
    course_menu = send(client, conversation_id, "2").get_json()
    assert "Please choose the new course" in course_menu["message"]["content"]
    course_intakes = send(client, conversation_id, "2").get_json()
    assert "Front Desk Operations" in course_intakes["message"]["content"]
    send(client, conversation_id, "1")
    send(client, conversation_id, "Confirm change")

    send(client, conversation_id, "Change enrollment")
    front_desk_alternatives = send(client, conversation_id, "3").get_json()
    front_desk_content = front_desk_alternatives["message"]["content"]
    assert "9 January 2027" in front_desk_content
    assert "3 February 2027" in front_desk_content
    assert "2 December 2026" not in front_desk_content
    with app.app_context():
        assert db.session.get(Conversation, conversation_id).pending_intake_options == [
            "front-desk-operations-2027-01-09",
            "front-desk-operations-2027-02-03",
        ]


def test_legacy_reschedule_alias_and_confirmation_phrase_remain_accepted(
    tmp_path, fake_ai
):
    app = management_app(tmp_path, fake_ai, "legacy-reschedule-alias.db")
    client = authenticated_client(app)
    conversation_id, draft_id = add_confirmed(app, client)

    choices = send(client, conversation_id, "Reschedule enrollment").get_json()
    assert "28–29 November 2026" in choices["message"]["content"]
    summary = send(client, conversation_id, "1").get_json()
    assert "Confirm change" in summary["message"]["content"]
    assert "Confirm reschedule" not in summary["message"]["content"]
    result = send(client, conversation_id, "Confirm reschedule").get_json()

    assert "has been changed" in result["message"]["content"]
    with app.app_context():
        assert db.session.get(EnrollmentDraft, draft_id).intake_id == "dental-assisting-2026-11-28"
        assert db.session.get(Conversation, conversation_id).pending_action is None


@pytest.mark.parametrize(
    ("choice", "expected_state", "expected_copy"),
    [
        ("1", AWAITING_CHANGE_SELECTION, "Change my email"),
        ("2", AWAITING_COURSE_CHANGE_SELECTION, "Please choose the new course"),
        ("3", AWAITING_RESCHEDULE_SELECTION, "Available Intakes"),
        ("4", AWAITING_CANCELLATION, "Confirm cancellation"),
    ],
)
def test_change_menu_numbers_are_locked_to_the_displayed_menu(
    tmp_path, fake_ai, choice, expected_state, expected_copy
):
    app = management_app(tmp_path, fake_ai, f"menu-choice-{choice}.db")
    client = authenticated_client(app)
    conversation_id, _ = add_confirmed(app, client)
    send(client, conversation_id, "Change enrollment")

    response = send(client, conversation_id, choice).get_json()

    assert expected_copy in response["message"]["content"]
    with app.app_context():
        assert db.session.get(Conversation, conversation_id).pending_action == expected_state


def test_keep_enrollment_clears_each_change_stage_without_mutation(tmp_path, fake_ai):
    app = management_app(tmp_path, fake_ai, "keep-every-stage.db")
    client = authenticated_client(app)
    conversation_id, draft_id = add_confirmed(app, client)

    stage_commands = [
        ("Change enrollment", None),
        ("Change enrollment", "2"),
        ("Change enrollment", ("2", "2")),
        ("Change enrollment", "3"),
        ("Change enrollment", ("3", "1")),
    ]
    for opener, selections in stage_commands:
        send(client, conversation_id, opener)
        for selection in (() if selections is None else selections if isinstance(selections, tuple) else (selections,)):
            send(client, conversation_id, selection)
        kept = send(client, conversation_id, "Keep enrollment").get_json()
        assert "unchanged" in kept["message"]["content"]
        with app.app_context():
            assert db.session.get(Conversation, conversation_id).pending_action is None
            assert db.session.get(EnrollmentDraft, draft_id).intake_id == "dental-assisting-2026-11-18"


def test_change_menu_and_intake_stage_survive_faq_and_backend_restart(
    tmp_path, fake_ai
):
    database_name = "change-menu-restart.db"
    app = management_app(tmp_path, fake_ai, database_name)
    client = authenticated_client(app, email="change-menu-restart@example.com")
    conversation_id, _ = add_confirmed(app, client)
    send(client, conversation_id, "Change enrollment")
    faq = send(client, conversation_id, "Can I use SkillsFuture Credits?").get_json()
    assert "SkillsFuture" in faq["message"]["content"]
    with app.app_context():
        assert db.session.get(Conversation, conversation_id).pending_action == AWAITING_CHANGE_SELECTION
    refreshed = client.get(f"/api/conversations/{conversation_id}/messages")
    assert refreshed.status_code == 200
    assert client.post("/api/auth/logout").status_code == 200

    restarted = management_app(tmp_path, fake_ai, database_name)
    resumed = restarted.test_client()
    login = resumed.post(
        "/api/auth/login",
        json={"email": client.test_email, "password": client.test_password},
    ).get_json()
    response = resumed.post(
        "/api/chat",
        json={"conversation_id": conversation_id, "message": "3"},
        headers={"X-CSRF-Token": login["csrf_token"]},
    ).get_json()
    assert "28–29 November 2026" in response["message"]["content"]
    faq = resumed.post(
        "/api/chat",
        json={"conversation_id": conversation_id, "message": "What is the course fee?"},
        headers={"X-CSRF-Token": login["csrf_token"]},
    ).get_json()
    assert "$600 nett" in faq["message"]["content"]
    with restarted.app_context():
        conversation = db.session.get(Conversation, conversation_id)
        assert conversation.pending_action == AWAITING_RESCHEDULE_SELECTION
        assert conversation.pending_intake_options == [
            "dental-assisting-2026-11-28",
            "dental-assisting-2026-12-16",
        ]


def test_unique_month_reschedule_resolves_to_matching_intake(
    tmp_path, fake_ai
):
    app = management_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id, draft_id = add_confirmed(app, client)
    response = send(client, conversation_id, "Change me to the November intake").get_json()
    content = response["message"]["content"]
    assert "Available November Intakes" in content
    assert "28–29 November 2026" in content
    assert "18–19 November 2026" not in content
    assert "10–11 October" not in content
    selected = send(client, conversation_id, "1").get_json()
    assert "28–29 November 2026" in selected["message"]["content"]
    with app.app_context():
        conversation = db.session.get(Conversation, conversation_id)
        draft = db.session.get(EnrollmentDraft, draft_id)
        assert conversation.pending_action == AWAITING_RESCHEDULE_CONFIRMATION
        assert conversation.pending_target_intake_id == "dental-assisting-2026-11-28"
        assert draft.intake_id == "dental-assisting-2026-11-18"


@pytest.mark.parametrize(
    ("message", "target_id"),
    [
        ("Change my course to 28 November", "dental-assisting-2026-11-28"),
        ("Move me to the second option", "dental-assisting-2026-12-16"),
    ],
)
def test_reschedule_selection_by_date_or_option_requires_confirmation(
    tmp_path, fake_ai, message, target_id
):
    app = management_app(tmp_path, fake_ai, f"selection-{target_id[-2:]}.db")
    client = authenticated_client(app)
    conversation_id, draft_id = add_confirmed(app, client)
    response = send(client, conversation_id, message).get_json()
    assert "Confirm change" in response["message"]["content"]
    with app.app_context():
        assert db.session.get(EnrollmentDraft, draft_id).intake_id == "dental-assisting-2026-11-18"
        assert db.session.get(Conversation, conversation_id).pending_target_intake_id == target_id


def test_keep_current_and_same_intake_make_no_change(tmp_path, fake_ai):
    app = management_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id, draft_id = add_confirmed(app, client)
    same = send(client, conversation_id, "Change my intake to 18 November").get_json()
    assert "already enrolled" in same["message"]["content"]
    send(client, conversation_id, "Change my intake to 28 November")
    kept = send(client, conversation_id, "Keep current intake").get_json()
    assert "kept unchanged" in kept["message"]["content"]
    with app.app_context():
        draft = db.session.get(EnrollmentDraft, draft_id)
        assert draft.intake_id == "dental-assisting-2026-11-18"
        assert db.session.get(CourseDate, "dental-assisting-2026-11-18").participant_count == 1
        assert db.session.get(CourseDate, "dental-assisting-2026-11-28").participant_count == 0


@pytest.mark.parametrize("unavailable", ["full", "closed", "cancelled", "historical"])
def test_unavailable_reschedule_target_is_rejected(tmp_path, fake_ai, unavailable):
    app = management_app(tmp_path, fake_ai, f"unavailable-{unavailable}.db")
    client = authenticated_client(app)
    conversation_id, draft_id = add_confirmed(app, client)
    with app.app_context():
        target = db.session.get(CourseDate, "dental-assisting-2026-11-28")
        if unavailable == "full":
            target.participant_count = target.capacity
        elif unavailable == "historical":
            target.date = date(2026, 9, 20)
            target.end_date = date(2026, 9, 21)
        else:
            target.status = unavailable
        db.session.commit()
    response = send(client, conversation_id, "Change my intake to 28 November").get_json()
    assert unavailable in response["message"]["content"].casefold() or "not one of" in response["message"]["content"].casefold()
    with app.app_context():
        assert db.session.get(EnrollmentDraft, draft_id).intake_id == "dental-assisting-2026-11-18"


def test_target_becoming_full_preserves_original_intake(tmp_path, fake_ai):
    app = management_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id, draft_id = add_confirmed(app, client)
    send(client, conversation_id, "Change my intake to 28 November")
    with app.app_context():
        target = db.session.get(CourseDate, "dental-assisting-2026-11-28")
        target.participant_count = target.capacity
        db.session.commit()
    response = send(client, conversation_id, "Confirm change").get_json()
    assert "full" in response["message"]["content"].casefold()
    with app.app_context():
        draft = db.session.get(EnrollmentDraft, draft_id)
        assert draft.intake_id == "dental-assisting-2026-11-18"
        assert draft.allocated_intake_id == "dental-assisting-2026-11-18"
        assert db.session.get(CourseDate, "dental-assisting-2026-11-18").participant_count == 1


def test_confirmed_reschedule_moves_one_place_and_updates_same_excel_row(tmp_path, fake_ai):
    app = management_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id, draft_id = add_confirmed(app, client)
    with app.app_context():
        service = app.extensions["master_invoice"]
        assert service.export_confirmed_enrollment(draft_id)
        invoice_number = MasterInvoiceExport.query.one().invoice_number
    send(client, conversation_id, "Reschedule my enrollment to 16 December")
    response = send(client, conversation_id, "Confirm change").get_json()
    assert "has been changed" in response["message"]["content"].casefold()
    send(client, conversation_id, "Confirm change")
    with app.app_context():
        draft = db.session.get(EnrollmentDraft, draft_id)
        assert draft.intake_id == "dental-assisting-2026-12-16"
        assert draft.previous_intake_id == "dental-assisting-2026-11-18"
        assert draft.rescheduled_at is not None
        assert db.session.get(CourseDate, "dental-assisting-2026-11-18").participant_count == 0
        assert db.session.get(CourseDate, "dental-assisting-2026-12-16").participant_count == 1
        assert MasterInvoiceExport.query.count() == 1
        assert MasterInvoiceExport.query.one().invoice_number == invoice_number
        workbook_path = app.extensions["master_invoice"].workbook_path
    workbook = load_workbook(workbook_path)
    try:
        sheet = workbook[SHEET_NAME]
        assert sheet.max_row == 2
        assert sheet.cell(2, 5).value.date() == date(2026, 12, 16)
        assert sheet.cell(2, 23).value == draft_id
    finally:
        workbook.close()


def test_pending_reschedule_survives_restart_logout_and_login(tmp_path, fake_ai):
    database_name = "restart-management.db"
    app = management_app(tmp_path, fake_ai, database_name)
    client = authenticated_client(app, email="management-restart@example.com")
    conversation_id, draft_id = add_confirmed(app, client)
    send(client, conversation_id, "Change my intake to 28 November")

    restarted = management_app(tmp_path, fake_ai, database_name)
    resumed = restarted.test_client()
    login = resumed.post(
        "/api/auth/login",
        json={"email": client.test_email, "password": client.test_password},
    )
    token = login.get_json()["csrf_token"]
    response = resumed.post(
        "/api/chat",
        json={"conversation_id": conversation_id, "message": "Confirm change"},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 200
    status = resumed.post(
        "/api/chat",
        json={"conversation_id": conversation_id, "message": "Which intake did I select?"},
        headers={"X-CSRF-Token": token},
    )
    assert "28–29 November 2026" in status.get_json()["message"]["content"]
    with restarted.app_context():
        draft = db.session.get(EnrollmentDraft, draft_id)
        assert draft.intake_id == "dental-assisting-2026-11-28"
        assert db.session.get(Conversation, conversation_id).pending_action is None


def test_faq_interruption_preserves_pending_action_and_new_chat_does_not_inherit_it(
    tmp_path, fake_ai
):
    app = management_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id, draft_id = add_confirmed(app, client)
    send(client, conversation_id, "Cancel my enrollment")
    faq = send(
        client,
        conversation_id,
        "Can I use Mid-Career SkillsFuture Credits?",
    ).get_json()
    assert "You cannot use Midcareer Skillsfuture Credit" in faq["message"]["content"]
    assert "Front Desk Operations" not in faq["message"]["content"]
    with app.app_context():
        assert db.session.get(Conversation, conversation_id).pending_action == AWAITING_CANCELLATION

    new_chat = client.post("/api/conversations").get_json()["conversation_id"]
    with app.app_context():
        assert db.session.get(Conversation, new_chat).pending_action is None
    send(client, new_chat, "yes")
    with app.app_context():
        assert db.session.get(EnrollmentDraft, draft_id).status == "confirmed"
    send(client, conversation_id, "Confirm cancellation")
    with app.app_context():
        assert db.session.get(EnrollmentDraft, draft_id).status == "cancelled"


def test_excel_failure_after_reschedule_keeps_database_change_and_retry_state(
    tmp_path, fake_ai, monkeypatch
):
    app = management_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id, draft_id = add_confirmed(app, client)
    service = app.extensions["master_invoice"]
    with app.app_context():
        assert service.export_confirmed_enrollment(draft_id)
    send(client, conversation_id, "Change my intake to 16 December")
    monkeypatch.setattr(
        service,
        "_atomic_save",
        lambda workbook: (_ for _ in ()).throw(PermissionError("locked")),
    )
    response = send(client, conversation_id, "Confirm change").get_json()
    assert "has been changed" in response["message"]["content"].casefold()
    with app.app_context():
        draft = db.session.get(EnrollmentDraft, draft_id)
        record = MasterInvoiceExport.query.one()
        assert draft.intake_id == "dental-assisting-2026-12-16"
        assert record.status == "failed"
        assert record.last_error and "Retry" in record.last_error


def test_staff_serialization_shows_management_history_without_full_nric(tmp_path, fake_ai):
    app = management_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id, draft_id = add_confirmed(app, client)
    send(client, conversation_id, "Change my intake to 28 November")
    send(client, conversation_id, "Confirm change")
    with app.app_context():
        draft = db.session.get(EnrollmentDraft, draft_id)
        row = app.extensions["finance"].serialize_enrollment(draft)
        assert row["previous_intake_display"] == "18–19 November 2026"
        assert row["reschedule_requested_at"]
        assert row["rescheduled_at"]
        assert row["nric_masked"] != "S1234567D"
        assert "S1234567D" not in str(row)


def test_status_queries_are_deterministic_accurate_and_private(tmp_path, fake_ai):
    app = management_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id, draft_id = add_confirmed(app, client)
    before = (len(fake_ai.classify_calls), len(fake_ai.faq_calls), len(fake_ai.enrollment_calls))
    response = send(client, conversation_id, "What's my status ah?").get_json()
    content = response["message"]["content"]
    after = (len(fake_ai.classify_calls), len(fake_ai.faq_calls), len(fake_ai.enrollment_calls))
    assert after == before
    assert "Enrollment Status" in content
    assert "Confirmed" in content
    assert "18–19 November 2026" in content
    assert "S$400.00" not in content and "S$200.00" not in content
    assert "Payment status" not in content
    assert "S1234567D" not in content
    assert response["routing"]["intent"] == "enrollment"

    send(client, conversation_id, "Change my intake to 16 December")
    send(client, conversation_id, "Confirm change")
    updated = send(client, conversation_id, "Did my reschedule work?").get_json()
    assert "16–17 December 2026" in updated["message"]["content"]


def test_draft_and_cancelled_status_are_accurate(tmp_path, fake_ai):
    app = management_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    draft_conversation = client.post("/api/conversations").get_json()["conversation_id"]
    with app.app_context():
        conversation = db.session.get(Conversation, draft_conversation)
        db.session.add(
            EnrollmentDraft(
                conversation=conversation,
                course=COURSE,
                course_fee=Decimal("600"),
                full_name="Draft Student",
                validation_status="needs_correction",
                validation_errors={"email": "Enter a valid email address."},
            )
        )
        db.session.commit()
    draft_status = send(client, draft_conversation, "What is left for me to complete?").get_json()
    assert "Draft" in draft_status["message"]["content"]
    assert "Still needed" in draft_status["message"]["content"]
    assert "Needs correction" in draft_status["message"]["content"]

    confirmed_conversation, _ = add_confirmed(app, client)
    send(client, confirmed_conversation, "Cancel my enrollment")
    send(client, confirmed_conversation, "Confirm cancellation")
    cancelled = send(client, confirmed_conversation, "Is my enrollment cancelled?").get_json()
    assert "No saved enrollment was found" in cancelled["message"]["content"]
    assert "Cancelled" not in cancelled["message"]["content"]


@pytest.mark.parametrize(
    "question",
    [
        "Am I enrolled?",
        "I forgot which intake I chose.",
        "Check my course registration.",
        "What course date am I attending?",
    ],
)
def test_natural_status_variants_bypass_ai(tmp_path, fake_ai, question):
    app = management_app(tmp_path, fake_ai, f"status-{abs(hash(question))}.db")
    client = authenticated_client(app)
    conversation_id, _ = add_confirmed(app, client)
    before = len(fake_ai.classify_calls)
    response = send(client, conversation_id, question).get_json()
    assert response["routing"]["intent"] == "enrollment"
    assert len(fake_ai.classify_calls) == before


def test_multiple_current_confirmed_enrollments_display_without_legacy_dump(
    tmp_path, fake_ai
):
    app = management_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    add_confirmed(app, client, "dental-assisting-2026-11-18", name="First Student")
    add_confirmed(app, client, "dental-assisting-2026-11-28", name="Second Student")
    new_conversation = client.post("/api/conversations").get_json()["conversation_id"]
    response = send(client, new_conversation, "Show my registration details").get_json()
    content = response["message"]["content"]
    assert "Confirmed enrollments" in content
    assert "18–19 November 2026" in content
    assert "28–29 November 2026" in content
    assert "Please open the relevant previous chat" not in content
    assert "S1234567D" not in content


def test_status_excludes_unrelated_incomplete_obsolete_cancelled_and_duplicates(
    tmp_path, fake_ai
):
    app = management_app(tmp_path, fake_ai, "filtered-status.db")
    client = authenticated_client(app)
    first_conversation, first_id = add_confirmed(
        app, client, "dental-assisting-2026-11-18", name="Private First"
    )
    add_confirmed(app, client, "dental-assisting-2026-11-28", name="Private Second")
    current_conversation = client.post("/api/conversations").get_json()["conversation_id"]
    unrelated_conversation = client.post("/api/conversations").get_json()["conversation_id"]
    obsolete_conversation = client.post("/api/conversations").get_json()["conversation_id"]
    cancelled_conversation = client.post("/api/conversations").get_json()["conversation_id"]
    discarded_conversation = client.post("/api/conversations").get_json()["conversation_id"]
    duplicate_conversation = client.post("/api/conversations").get_json()["conversation_id"]

    with app.app_context():
        current = db.session.get(Conversation, current_conversation)
        unrelated = db.session.get(Conversation, unrelated_conversation)
        obsolete = db.session.get(Conversation, obsolete_conversation)
        cancelled = db.session.get(Conversation, cancelled_conversation)
        discarded = db.session.get(Conversation, discarded_conversation)
        duplicate = db.session.get(Conversation, duplicate_conversation)
        first = db.session.get(EnrollmentDraft, first_id)
        db.session.add_all(
            [
                EnrollmentDraft(
                    conversation=current,
                    course_id="dental-assisting-basic",
                    course=COURSE,
                    full_name="Current Private Name",
                    nric="S1234567D",
                    email="current.private@example.com",
                    status="collecting",
                ),
                EnrollmentDraft(
                    conversation=unrelated,
                    course_id="dental-assisting-basic",
                    course=COURSE,
                    full_name="Unrelated Private Name",
                    status="collecting",
                ),
                EnrollmentDraft(
                    conversation=obsolete,
                    course_id="dental-infection-control",
                    course="1-Day Dental Infection Control & Sterilisation Workshop",
                    status="confirmed",
                    confirmed_at=utc_now(),
                ),
                EnrollmentDraft(
                    conversation=cancelled,
                    course_id="dental-assisting-basic",
                    course=COURSE,
                    status="cancelled",
                    confirmed_at=utc_now(),
                ),
                EnrollmentDraft(
                    conversation=discarded,
                    course_id="dental-assisting-basic",
                    course=COURSE,
                    status="discarded",
                    friendly_reference="DISCARDED-REF",
                ),
                EnrollmentDraft(
                    conversation=duplicate,
                    course_id="dental-assisting-basic",
                    course=COURSE,
                    course_fee=Decimal("600"),
                    intake_id=first.intake_id,
                    preferred_intake_date=first.preferred_intake_date,
                    intake_start_date=first.intake_start_date,
                    intake_end_date=first.intake_end_date,
                    status="confirmed",
                    confirmed_at=utc_now(),
                ),
            ]
        )
        db.session.commit()

    payload = send(client, current_conversation, "What is my enrollment status?").get_json()
    content = payload["message"]["content"]

    assert content.count("Basic Certificate in Dental Assisting") == 3
    assert "18–19 November 2026" in content
    assert "28–29 November 2026" in content
    assert "Current draft" in content
    assert "Dental Infection Control" not in content
    assert "Cancelled" not in content
    assert "DISCARDED-REF" not in content
    assert "Unrelated Private Name" not in content
    assert "Current Private Name" not in content
    assert "S1234567D" not in content
    assert "current.private@example.com" not in content
    assert "Reference pending" not in content


def test_concurrent_allocations_cannot_overbook_target(tmp_path, fake_ai):
    app = management_app(tmp_path, fake_ai)
    first_client = authenticated_client(app, email="capacity-one@example.com")
    second_client = authenticated_client(app, email="capacity-two@example.com")
    _, first_id = add_confirmed(app, first_client, "dental-assisting-2026-11-18", name="First Student")
    _, second_id = add_confirmed(app, second_client, "dental-assisting-2026-11-28", name="Second Student")
    with app.app_context():
        target = db.session.get(CourseDate, "dental-assisting-2026-12-16")
        target.capacity = 1
        db.session.commit()

    def attempt(draft_id):
        with app.app_context():
            draft = db.session.get(EnrollmentDraft, draft_id)
            target = db.session.get(CourseDate, "dental-assisting-2026-12-16")
            draft.intake_id = target.id
            draft.preferred_intake_date = target.date
            draft.intake_start_date = target.date
            draft.intake_end_date = target.end_date
            try:
                app.extensions["intakes"].allocate(draft)
                db.session.commit()
                return True
            except Exception:
                db.session.rollback()
                return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, (first_id, second_id)))
    assert sorted(results) == [False, True]
    with app.app_context():
        target = db.session.get(CourseDate, "dental-assisting-2026-12-16")
        all_intakes = CourseDate.query.all()
        assert target.participant_count == 1
        assert sum(row.participant_count for row in all_intakes) == 2
