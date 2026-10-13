"""Focused regressions for post-confirmation grouped enrollment management."""

import pytest

from backend.extensions import db
from backend.models import Conversation, EnrollmentCourse, EnrollmentDraft
from backend.models.finance import MasterInvoiceExport
from backend.models.operations import CourseDate
from backend.tests.conftest import authenticated_client, send
from backend.tests.enrollment_tests.test_multi_course_enrollment import (
    _app,
    _complete_both,
    _start_both,
)


def _confirm_both(client):
    conversation_id, _ = _start_both(client)
    _complete_both(client, conversation_id)
    confirmed = send(client, conversation_id, "Confirm").get_json()
    assert confirmed["enrollment"]["status"] == "confirmed"
    return conversation_id


def _confirm_one(client):
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    send(client, conversation_id, "I want to enrol")
    send(client, conversation_id, "1")
    send(client, conversation_id, "1")
    send(
        client,
        conversation_id,
        "Full name: Test Student; NRIC/FIN: S1234567D; "
        "Date of birth: 15 May 2000; Email: test.student@example.com; "
        "Mobile number: 91234567",
    )
    confirmed = send(client, conversation_id, "Confirm").get_json()
    assert confirmed["enrollment"]["status"] == "confirmed"
    return conversation_id


def test_selective_cancellation_requires_confirmation_and_releases_only_one_place(
    tmp_path, fake_ai
):
    app = _app(tmp_path, fake_ai)
    client = authenticated_client(app, "selective-cancel@example.com")
    conversation_id = _confirm_both(client)

    with app.app_context():
        rows = EnrollmentCourse.query.order_by(EnrollmentCourse.position).all()
        original_counts = {
            row.intake_id: db.session.get(CourseDate, row.intake_id).participant_count
            for row in rows
        }

    staged = send(client, conversation_id, "Cancel the second course").get_json()
    assert "cancel only this course" in staged["message"]["content"]
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.status == "confirmed"
        assert all(row.status == "confirmed" for row in draft.course_enrollments)

    result = send(client, conversation_id, "Confirm cancellation").get_json()
    assert "remains confirmed" in result["message"]["content"]
    assert result["enrollment"]["status"] == "confirmed"
    assert [row["status"] for row in result["enrollment"]["courses"]] == [
        "confirmed",
        "cancelled",
    ]
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        rows = EnrollmentCourse.query.order_by(EnrollmentCourse.position).all()
        assert draft.status == "confirmed"
        assert draft.confirmed_at is not None
        assert rows[0].status == "confirmed"
        assert rows[0].allocated_intake_id == rows[0].intake_id
        assert rows[1].status == "cancelled"
        assert rows[1].cancelled_at is not None
        assert rows[1].allocated_intake_id is None
        assert db.session.get(CourseDate, rows[0].intake_id).participant_count == original_counts[rows[0].intake_id]
        assert db.session.get(CourseDate, rows[1].intake_id).participant_count == original_counts[rows[1].intake_id] - 1
        assert draft.course_fee == rows[0].course_fee

    repeated = send(client, conversation_id, "Confirm cancellation").get_json()
    assert "no cancellation" in repeated["message"]["content"].casefold()


def test_ambiguous_group_cancellation_asks_for_course_and_keep_changes_nothing(
    tmp_path, fake_ai
):
    app = _app(tmp_path, fake_ai)
    client = authenticated_client(app, "ambiguous-cancel@example.com")
    conversation_id = _confirm_both(client)

    prompt = send(client, conversation_id, "Cancel my course").get_json()
    assert "Which course would you like to cancel?" in prompt["message"]["content"]
    kept = send(client, conversation_id, "Keep enrollment").get_json()
    assert "both course enrollments remain unchanged" in kept["message"]["content"]
    with app.app_context():
        assert all(row.status == "confirmed" for row in EnrollmentCourse.query.all())


def test_add_second_course_is_staged_without_unconfirming_first_and_updates_same_registration(
    tmp_path, fake_ai
):
    app = _app(tmp_path, fake_ai)
    client = authenticated_client(app, "add-course@example.com")
    conversation_id = _confirm_one(client)

    offered = send(client, conversation_id, "I also want to enrol in the other course").get_json()
    assert "remains confirmed" in offered["message"]["content"]
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        original_confirmed_at = draft.confirmed_at
        assert draft.status == "confirmed"
        assert len(draft.course_enrollments) == 2
        original_row_id = draft.course_enrollments[0].id
        assert [row.status for row in draft.course_enrollments] == [
            "confirmed",
            "pending_addition",
        ]

    summary = send(client, conversation_id, "1").get_json()
    assert "updated two-course registration" in summary["message"]["content"]
    assert "Combined total: S$950.00" in summary["message"]["content"]
    assert [row["status"] for row in summary["enrollment"]["courses"]] == [
        "confirmed",
        "pending_addition",
    ]
    panel = client.get(f"/api/conversations/{conversation_id}/status").get_json()
    assert [row["status"] for row in panel["enrollment"]["courses"]] == [
        "confirmed",
        "pending_addition",
    ]
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.status == "confirmed"
        assert len(draft.course_enrollments) == 2
        assert [row.status for row in draft.course_enrollments] == [
            "confirmed",
            "pending_addition",
        ]

    added = send(client, conversation_id, "Confirm change").get_json()
    assert "Both courses are now under the same registration and invoice" in added["message"]["content"]
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        rows = EnrollmentCourse.query.order_by(EnrollmentCourse.position).all()
        assert draft.status == "confirmed"
        assert draft.confirmed_at == original_confirmed_at
        assert len(rows) == 2
        assert rows[0].id == original_row_id
        assert all(row.status == "confirmed" for row in rows)
        assert len({row.course_id for row in rows}) == 2
        assert MasterInvoiceExport.query.filter_by(enrollment_id=draft.id).count() == 1


def test_new_chat_does_not_duplicate_already_held_course_and_can_add_other(
    tmp_path, fake_ai
):
    app = _app(tmp_path, fake_ai)
    client = authenticated_client(app, "new-chat-add@example.com")
    _confirm_one(client)
    new_conversation = client.post("/api/conversations").get_json()["conversation_id"]

    duplicate = send(
        client, new_conversation, "I want to enrol in Dental Assisting"
    ).get_json()
    assert "already registered" in duplicate["message"]["content"]
    with app.app_context():
        assert EnrollmentDraft.query.count() == 1
        assert EnrollmentCourse.query.count() == 1

    addition = send(
        client, new_conversation, "I want to enrol in Infection Control"
    ).get_json()
    assert "Choose an intake" in addition["message"]["content"]
    with app.app_context():
        assert EnrollmentDraft.query.count() == 1
        pending = db.session.get(Conversation, new_conversation)
        assert pending.pending_enrollment_id == EnrollmentDraft.query.one().id


def test_group_status_shows_active_and_cancelled_courses_with_current_invoice(
    tmp_path, fake_ai
):
    app = _app(tmp_path, fake_ai)
    client = authenticated_client(app, "group-status@example.com")
    conversation_id = _confirm_both(client)
    send(client, conversation_id, "Cancel course 2")
    send(client, conversation_id, "Confirm cancellation")

    calls_before = (
        len(fake_ai.classify_calls),
        len(fake_ai.faq_calls),
        len(fake_ai.enrollment_calls),
    )
    status = send(client, conversation_id, "Which courses am I registered for?").get_json()
    content = status["message"]["content"]
    assert "Course 1:" in content and "Status: Confirmed" in content
    assert "Course 2:" in content and "Status: Cancelled" in content
    assert "Invoice number:" in content
    assert "Current invoice total: S$600.00" in content
    assert "S1234567D" not in content
    assert "private" not in content.casefold()
    assert calls_before == (
        len(fake_ai.classify_calls),
        len(fake_ai.faq_calls),
        len(fake_ai.enrollment_calls),
    )


def test_whole_group_cancellation_still_cancels_both_courses_safely(
    tmp_path, fake_ai
):
    app = _app(tmp_path, fake_ai)
    client = authenticated_client(app, "cancel-both@example.com")
    conversation_id = _confirm_both(client)

    staged = send(client, conversation_id, "Cancel both courses").get_json()
    assert "Confirm cancellation" in staged["message"]["content"]
    send(client, conversation_id, "Confirm cancellation")
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.status == "cancelled"
        assert all(row.status == "cancelled" for row in draft.course_enrollments)
        assert all(row.allocated_intake_id is None for row in draft.course_enrollments)


def test_unavailable_second_course_intake_leaves_first_course_unchanged(
    tmp_path, fake_ai
):
    app = _app(tmp_path, fake_ai)
    client = authenticated_client(app, "unavailable-add@example.com")
    conversation_id = _confirm_one(client)
    send(client, conversation_id, "Add the Infection Control course")
    send(client, conversation_id, "1")
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        first_id = draft.course_enrollments[0].id
        original_allocation = draft.course_enrollments[0].allocated_intake_id
        target = db.session.get(CourseDate, "infection-control-2026-10-18")
        target.participant_count = target.capacity
        db.session.commit()

    response = send(client, conversation_id, "Confirm change").get_json()
    assert "no longer available" in response["message"]["content"]
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.status == "confirmed"
        assert len(draft.course_enrollments) == 2
        assert draft.course_enrollments[0].id == first_id
        assert draft.course_enrollments[0].allocated_intake_id == original_allocation
        assert draft.course_enrollments[1].status == "pending_addition"
        assert draft.course_enrollments[1].allocated_intake_id is None


def test_pending_course_cancellation_survives_restart_and_new_chat_does_not_inherit(
    tmp_path, fake_ai
):
    email = "group-restart@example.com"
    password = "test-password-123"
    app = _app(tmp_path, fake_ai)
    client = authenticated_client(app, email, password)
    conversation_id = _confirm_both(client)
    send(client, conversation_id, "Cancel one course")

    restarted = _app(tmp_path, fake_ai)
    resumed = restarted.test_client()
    login = resumed.post("/api/auth/login", json={"email": email, "password": password})
    token = login.get_json()["csrf_token"]
    new_chat = resumed.post(
        "/api/conversations", headers={"X-CSRF-Token": token}
    ).get_json()["conversation_id"]
    with restarted.app_context():
        pending = db.session.get(Conversation, conversation_id)
        assert pending.pending_action == "cancel_course_selection"
        assert len(pending.pending_change_data["cancellable_course_ids"]) == 2
        assert db.session.get(Conversation, new_chat).pending_action is None
    selection = resumed.post(
        "/api/chat",
        json={"conversation_id": conversation_id, "message": "2"},
        headers={"X-CSRF-Token": token},
    )
    assert "Confirm course cancellation" in selection.get_json()["message"]["content"]
    response = resumed.post(
        "/api/chat",
        json={"conversation_id": conversation_id, "message": "Confirm course cancellation"},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 200
    with restarted.app_context():
        rows = EnrollmentCourse.query.order_by(EnrollmentCourse.position).all()
        assert [row.status for row in rows] == ["confirmed", "cancelled"]


def test_pending_second_course_survives_refresh_login_and_restart(
    tmp_path, fake_ai
):
    email = "addition-restart@example.com"
    password = "test-password-123"
    app = _app(tmp_path, fake_ai)
    client = authenticated_client(app, email, password)
    conversation_id = _confirm_one(client)
    send(client, conversation_id, "I want both courses now")
    send(client, conversation_id, "1")

    restarted = _app(tmp_path, fake_ai)
    resumed = restarted.test_client()
    login = resumed.post("/api/auth/login", json={"email": email, "password": password})
    token = login.get_json()["csrf_token"]
    refreshed = resumed.get(f"/api/conversations/{conversation_id}/messages").get_json()
    assert [row["status"] for row in refreshed["enrollment"]["courses"]] == [
        "confirmed",
        "pending_addition",
    ]
    result = resumed.post(
        "/api/chat",
        json={"conversation_id": conversation_id, "message": "Confirm course addition"},
        headers={"X-CSRF-Token": token},
    )
    assert result.status_code == 200
    with restarted.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.status == "confirmed"
        assert len(draft.course_enrollments) == 2
        assert all(row.status == "confirmed" for row in draft.course_enrollments)


@pytest.mark.parametrize(
    ("message", "expected_course_id"),
    (
        (
            "Cancel 2-Day Basic Certificate in Dental Assisting",
            "dental-assisting-basic",
        ),
        (
            "Cancel 1-Day Dental Infection Control & Sterilisation Workshop",
            "dental-infection-control",
        ),
        ("Cancel Dental Assisting", "dental-assisting-basic"),
        ("Deregister me from Infection Control", "dental-infection-control"),
        ("Remove course 1", "dental-assisting-basic"),
        ("Drop the second course", "dental-infection-control"),
    ),
)
def test_cancellation_course_names_route_before_course_selection_and_ignore_duration_digits(
    tmp_path, fake_ai, message, expected_course_id
):
    app = _app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = _confirm_both(client)

    response = send(client, conversation_id, message).get_json()

    assert "cancel only this course" in response["message"]["content"]
    assert "already selected" not in response["message"]["content"].casefold()
    with app.app_context():
        conversation = db.session.get(Conversation, conversation_id)
        selected = db.session.get(
            EnrollmentCourse,
            conversation.pending_change_data["course_enrollment_id"],
        )
        assert selected.course_id == expected_course_id
        assert conversation.pending_action == "awaiting_course_cancellation_confirmation"
        assert EnrollmentDraft.query.one().status == "confirmed"
        assert all(row.status == "confirmed" for row in EnrollmentCourse.query.all())


@pytest.mark.parametrize(
    ("choice", "expected_course_id"),
    (("1", "dental-assisting-basic"), ("2", "dental-infection-control")),
)
def test_pending_cancellation_number_is_locked_to_displayed_child_list(
    tmp_path, fake_ai, choice, expected_course_id
):
    app = _app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = _confirm_both(client)
    prompt = send(client, conversation_id, "I want to cancel one course").get_json()
    assert "Which course would you like to cancel?" in prompt["message"]["content"]
    with app.app_context():
        conversation = db.session.get(Conversation, conversation_id)
        displayed = list(conversation.pending_change_data["cancellable_course_ids"])
        assert conversation.pending_action == "cancel_course_selection"
        assert len(displayed) == 2

    calls_before = (
        len(fake_ai.classify_calls),
        len(fake_ai.faq_calls),
        len(fake_ai.enrollment_calls),
    )
    status = send(client, conversation_id, "Which courses am I registered for?").get_json()
    assert "Cancellation request: Awaiting your confirmation" in status["message"]["content"]
    assert "Draft" not in status["message"]["content"]
    assert calls_before == (
        len(fake_ai.classify_calls),
        len(fake_ai.faq_calls),
        len(fake_ai.enrollment_calls),
    )

    selected_reply = send(client, conversation_id, choice).get_json()
    assert "Confirm course cancellation" in selected_reply["message"]["content"]
    with app.app_context():
        conversation = db.session.get(Conversation, conversation_id)
        selected = db.session.get(
            EnrollmentCourse,
            conversation.pending_change_data["course_enrollment_id"],
        )
        assert selected.course_id == expected_course_id
        draft = EnrollmentDraft.query.one()
        assert draft.status == "confirmed"
        assert all(row.status == "confirmed" for row in draft.course_enrollments)
        assert all(row.allocated_intake_id for row in draft.course_enrollments)


def test_cancellation_selection_survives_faq_and_keep_both_abandons_safely(
    tmp_path, fake_ai
):
    app = _app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = _confirm_both(client)
    send(client, conversation_id, "Cancel one course")

    faq = send(client, conversation_id, "Can I use SkillsFuture?").get_json()
    assert "SkillsFuture" in faq["message"]["content"]
    with app.app_context():
        assert db.session.get(Conversation, conversation_id).pending_action == "cancel_course_selection"

    kept = send(client, conversation_id, "Keep both courses").get_json()
    assert "unchanged" in kept["message"]["content"]
    with app.app_context():
        conversation = db.session.get(Conversation, conversation_id)
        assert conversation.pending_action is None
        assert all(row.status == "confirmed" for row in EnrollmentCourse.query.all())


def test_single_course_confirmation_offers_deterministic_continuation_and_decline(
    tmp_path, fake_ai
):
    app = _app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = _confirm_one(client)
    messages = client.get(f"/api/conversations/{conversation_id}/messages").get_json()
    confirmation = messages["messages"][-1]["content"]
    assert "Is that all, or would you like to enrol in another course?" in confirmation
    assert "Add another course" in confirmation
    assert not any(
        phrase in confirmation
        for phrase in ("Payment method", "PayNow amount", "SkillsFuture amount")
    )
    with app.app_context():
        assert db.session.get(Conversation, conversation_id).pending_followup == "post_confirmation_add_course"

    declined = send(client, conversation_id, "That’s all").get_json()
    assert "confirmed enrollment is complete" in declined["message"]["content"]
    with app.app_context():
        conversation = db.session.get(Conversation, conversation_id)
        assert conversation.pending_followup is None
        assert EnrollmentCourse.query.count() == 1

    send(client, conversation_id, "yes")
    with app.app_context():
        assert EnrollmentCourse.query.count() == 1


def test_generic_yes_adds_other_course_only_for_post_confirmation_followup(
    tmp_path, fake_ai
):
    app = _app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = _confirm_one(client)

    offered = send(client, conversation_id, "yes").get_json()
    assert "Choose an intake" in offered["message"]["content"]
    assert "Infection Control" in offered["message"]["content"]
    with app.app_context():
        rows = EnrollmentCourse.query.order_by(EnrollmentCourse.position).all()
        assert [row.status for row in rows] == ["confirmed", "pending_addition"]


def test_two_course_confirmation_says_participant_is_enrolled_in_both(
    tmp_path, fake_ai
):
    app = _app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id, _ = _start_both(client)
    _complete_both(client, conversation_id)

    confirmed = send(client, conversation_id, "Confirm").get_json()

    assert "enrolled in both available courses" in confirmed["message"]["content"]
    with app.app_context():
        assert db.session.get(Conversation, conversation_id).pending_followup is None


def test_cancelled_course_is_reactivated_in_place_and_allocated_once(
    tmp_path, fake_ai
):
    app = _app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = _confirm_both(client)
    with app.app_context():
        rows = EnrollmentCourse.query.order_by(EnrollmentCourse.position).all()
        cancelled_id = rows[1].id
        old_intake_id = rows[1].intake_id
        original_first_allocation = rows[0].allocated_intake_id

    send(client, conversation_id, "Cancel course 2")
    send(client, conversation_id, "Confirm course cancellation")
    offered = send(client, conversation_id, "Add the Infection Control course").get_json()
    assert "Choose an intake" in offered["message"]["content"]
    with app.app_context():
        rows = EnrollmentCourse.query.order_by(EnrollmentCourse.position).all()
        assert len(rows) == 2
        assert rows[1].id == cancelled_id
        assert rows[1].status == "pending_addition"
        assert rows[1].intake_id is None
        assert rows[1].allocated_intake_id is None
        assert rows[1].cancelled_at is not None
        assert rows[0].status == "confirmed"
        assert rows[0].allocated_intake_id == original_first_allocation

    send(client, conversation_id, "1")
    send(client, conversation_id, "Confirm course addition")
    with app.app_context():
        rows = EnrollmentCourse.query.order_by(EnrollmentCourse.position).all()
        assert len(rows) == 2
        assert rows[1].id == cancelled_id
        assert rows[1].status == "confirmed"
        assert rows[1].allocated_intake_id == rows[1].intake_id
        assert rows[1].intake_id == old_intake_id
        count_after = db.session.get(CourseDate, rows[1].intake_id).participant_count
        assert count_after == 1

    send(client, conversation_id, "Confirm course addition")
    with app.app_context():
        rows = EnrollmentCourse.query.order_by(EnrollmentCourse.position).all()
        assert EnrollmentCourse.query.count() == 2
        assert db.session.get(CourseDate, rows[1].intake_id).participant_count == 1
