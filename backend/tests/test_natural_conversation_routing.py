"""Focused contract tests for semantic-first, transaction-safe chat routing."""

import pytest

from backend.extensions import db
from backend.models import Conversation, EnrollmentDraft
from backend.models.operations import Lead, StaffEscalation
from backend.tests.conftest import send


def _reach_intake_menu(client, conversation_id):
    send(client, conversation_id, "I want to enrol")
    selected = send(client, conversation_id, "1").get_json()
    assert selected["routing"]["handler"] == "deterministic"
    assert selected["routing"]["rule"] == "displayed_course_selection"
    return selected


def test_natural_faq_reaches_crewai_before_grounded_answer(
    client, conversation_id, fake_ai
):
    payload = send(client, conversation_id, "Why is the course expensive?").get_json()

    assert fake_ai.classify_calls[-1]["message"] == "Why is the course expensive?"
    assert fake_ai.enquiry_calls[-1]["message"] == "Why is the course expensive?"
    assert payload["routing"] == {
        "intent": "faq",
        "confidence": 0.96,
        "handler": "crewai",
        "agent": "enquiry",
        "rule": "enquiry_grounded_answer",
        "response_type": "grounded_answer",
    }
    assert "course fee" in payload["message"]["content"].casefold()


def test_hesitation_does_not_select_an_intake_and_preserves_options(
    app, client, conversation_id, fake_ai
):
    _reach_intake_menu(client, conversation_id)
    with app.app_context():
        before = list(
            db.session.get(Conversation, conversation_id).pending_intake_options
        )

    payload = send(
        client, conversation_id, "I'm not sure which intake works."
    ).get_json()

    assert payload["routing"]["intent"] == "pause_workflow"
    assert payload["routing"]["handler"] == "crewai"
    assert payload["routing"]["rule"] == "pause_preserve_pending_state"
    assert fake_ai.classify_calls[-1]["message"] == "I'm not sure which intake works."
    with app.app_context():
        conversation = db.session.get(Conversation, conversation_id)
        assert conversation.pending_intake_options == before
        assert conversation.pending_followup == "intake_selection"


def test_faq_interruption_preserves_pending_enrollment(
    app, client, conversation_id, fake_ai
):
    _reach_intake_menu(client, conversation_id)
    with app.app_context():
        before = list(
            db.session.get(Conversation, conversation_id).pending_intake_options
        )

    payload = send(client, conversation_id, "Why is the course expensive?").get_json()

    assert payload["routing"]["intent"] == "faq"
    assert fake_ai.enquiry_calls
    with app.app_context():
        conversation = db.session.get(Conversation, conversation_id)
        assert conversation.pending_intake_options == before
        assert conversation.pending_followup == "intake_selection"
        assert conversation.enrollment_draft is not None


def test_pause_and_resume_are_natural_and_keep_the_saved_draft(
    app, client, conversation_id
):
    _reach_intake_menu(client, conversation_id)
    paused = send(client, conversation_id, "I want to continue later.").get_json()
    resumed = send(client, conversation_id, "Continue my enrollment.").get_json()

    assert paused["routing"]["intent"] == "pause_workflow"
    assert "pause" in paused["message"]["content"].casefold()
    assert resumed["routing"]["intent"] == "resume_workflow"
    assert resumed["routing"]["rule"] == "resume_saved_enrollment"
    with app.app_context():
        conversation = db.session.get(Conversation, conversation_id)
        assert conversation.enrollment_draft is not None
        assert conversation.pending_intake_options


def test_workflow_question_uses_semantic_routing_without_losing_state(
    app, client, conversation_id, fake_ai
):
    _reach_intake_menu(client, conversation_id)
    payload = send(client, conversation_id, "What information do you need?").get_json()

    assert fake_ai.classify_calls[-1]["message"] == "What information do you need?"
    assert payload["routing"]["intent"] == "workflow_question"
    assert payload["routing"]["agent"] == "classifier"
    with app.app_context():
        assert db.session.get(Conversation, conversation_id).pending_intake_options


def test_course_comparison_uses_only_approved_facts(
    client, conversation_id, fake_ai
):
    payload = send(
        client, conversation_id, "Which course is suitable for beginners?"
    ).get_json()

    assert fake_ai.classify_calls
    assert payload["routing"]["intent"] == "course_comparison"
    assert payload["routing"]["rule"] == "enquiry_grounded_answer"
    assert "GCE 'N' Levels" in payload["message"]["content"]
    assert "best for beginners" not in payload["message"]["content"].casefold()


def test_question_labelled_interest_is_answered_before_offering_enrollment(
    client, conversation_id, fake_ai
):
    original = fake_ai.classify

    def over_eager(**kwargs):
        result = original(**kwargs)
        if "top up" in kwargs["message"]:
            result.intent = "enrollment_interest"
        return result

    fake_ai.classify = over_eager
    payload = send(
        client, conversation_id, "How much would I need to top up for dental assisting?"
    ).get_json()
    assert payload["routing"]["agent"] == "enquiry"
    assert "Would you like me to begin your enrollment" not in payload["message"]["content"]


def test_greeting_labelled_interest_and_pause_without_a_draft_go_to_module_a(
    client, conversation_id, fake_ai
):
    original = fake_ai.classify

    def confused(**kwargs):
        result = original(**kwargs)
        if kwargs["message"] == "Hi again!":
            result.intent = "enrollment_interest"
        if kwargs["message"] == "hmm":
            result.intent = "pause_workflow"
        return result

    fake_ai.classify = confused
    greeting = send(client, conversation_id, "Hi again!").get_json()
    assert greeting["routing"]["agent"] == "enquiry"
    hesitation = send(client, conversation_id, "hmm").get_json()
    assert hesitation["routing"]["agent"] == "enquiry"


def test_frustration_labelled_pause_during_enrollment_gets_a_module_a_reply(
    client, conversation_id, fake_ai
):
    _reach_intake_menu(client, conversation_id)
    original = fake_ai.classify

    def as_pause(**kwargs):
        result = original(**kwargs)
        if "annoying" in kwargs["message"]:
            result.intent = "pause_workflow"
        return result

    fake_ai.classify = as_pause
    payload = send(client, conversation_id, "ugh why so many questions, so annoying").get_json()
    assert payload["routing"]["agent"] == "enquiry"
    assert "continue where you left off" in payload["message"]["content"]
    assert payload["enrollment"]["status"] == "collecting"

    # A real request to pause still gets Module B's pause reply.
    paused = send(client, conversation_id, "I want to continue later.").get_json()
    assert paused["routing"]["rule"] == "pause_preserve_pending_state"


def test_stated_credit_is_answered_and_remembered_whichever_module_replies(
    app, client, conversation_id, fake_ai
):
    from backend.models import Conversation
    from backend.services.enquiry.memory_service import MemoryService

    original = fake_ai.classify

    def as_interest(**kwargs):
        result = original(**kwargs)
        if "credits" in kwargs["message"]:
            result.intent = "enrollment_interest"
        return result

    fake_ai.classify = as_interest
    payload = send(client, conversation_id, "I have $500 in SkillsFuture credits").get_json()
    assert payload["routing"]["agent"] == "enquiry"
    assert "Would you like me to begin your enrollment" not in payload["message"]["content"]

    # Even a Module B turn (an enrollment answer) remembers a stated balance.
    other = client.post("/api/conversations").get_json()["conversation_id"]
    send(client, other, "I want to enrol in Dental Assisting.")
    send(client, other, "1")
    send(client, other, "My full name is Test Student and I have $350 SkillsFuture credits")
    with app.app_context():
        conversation = db.session.get(Conversation, other)
        profile = MemoryService().build_context(conversation, [], "").profile
        assert profile["sfc_balance_stated"][0]["value"] == "S$350"


def test_registration_question_naming_a_course_is_not_a_course_selection(
    client, conversation_id
):
    payload = send(
        client,
        conversation_id,
        "What details will you need from me to register for Dental Assisting?",
    ).get_json()
    assert payload["routing"]["rule"] != "canonical_course_selection"
    assert payload["enrollment"]["status"] is None


def test_casual_and_general_questions_do_not_create_staff_escalations(
    app, client, conversation_id
):
    casual = send(client, conversation_id, "How are you?").get_json()
    general = send(client, conversation_id, "Tell me a joke.").get_json()

    assert casual["routing"]["intent"] == "small_talk"
    assert general["routing"]["intent"] == "unsupported_general_question"
    assert general["routing"]["rule"] == "enquiry_general_knowledge"
    with app.app_context():
        assert StaffEscalation.query.count() == 0


def test_unsupported_company_question_uses_grounded_staff_fallback(
    app, client, conversation_id
):
    payload = send(
        client, conversation_id, "Is parking included with the course?"
    ).get_json()

    assert payload["routing"]["intent"] == "faq"
    assert "don't have confirmed information" in payload["message"]["content"].casefold()
    with app.app_context():
        assert StaffEscalation.query.filter_by(
            conversation_id=conversation_id, category="unanswered_question"
        ).count() == 1


def test_displayed_numbers_remain_deterministic_and_conversation_scoped(
    client, conversation_id, fake_ai
):
    send(client, conversation_id, "I want to enrol")
    calls_before = len(fake_ai.classify_calls)
    selected = send(client, conversation_id, "2").get_json()

    assert len(fake_ai.classify_calls) == calls_before
    assert selected["routing"]["handler"] == "deterministic"
    assert selected["routing"]["agent"] == "none"
    assert selected["routing"]["rule"] == "displayed_course_selection"
    assert selected["enrollment"]["course_id"] == "dental-clinic-front-desk-operations"


def test_exact_pending_confirmation_never_calls_the_llm(
    client, conversation_id, fake_ai
):
    _reach_intake_menu(client, conversation_id)
    send(client, conversation_id, "1")
    ready = send(
        client,
        conversation_id,
        "Full name: Alex Lim; NRIC: S1234567D; DOB: 03/09/2000; "
        "Email: alex.confirm@example.com; Mobile: 91234567",
    ).get_json()
    assert ready["enrollment"]["status"] == "awaiting_confirmation"
    calls_before = (
        len(fake_ai.classify_calls),
        len(fake_ai.enquiry_calls),
        len(fake_ai.enrollment_calls),
    )

    confirmed = send(client, conversation_id, "Confirm").get_json()

    assert (
        len(fake_ai.classify_calls),
        len(fake_ai.enquiry_calls),
        len(fake_ai.enrollment_calls),
    ) == calls_before
    assert confirmed["enrollment"]["status"] == "confirmed"
    assert confirmed["routing"]["handler"] == "deterministic"
    assert confirmed["routing"]["agent"] == "none"
    assert confirmed["routing"]["rule"] == "pending_enrollment_confirmation"


def test_natural_date_interest_asks_permission_before_collecting_identity(
    app, client, conversation_id
):
    dates = send(
        client,
        conversation_id,
        "What dates are available for Dental Assisting?",
    ).get_json()
    assert "18–19 November 2026" in dates["message"]["content"]

    interested = send(
        client, conversation_id, "18 November works for me"
    ).get_json()

    assert interested["routing"]["intent"] == "enrollment_interest"
    assert interested["routing"]["rule"] == "qualified_interest_confirmation"
    assert "Would you like me to begin your enrollment" in interested["message"]["content"]
    assert "name and mobile number first" in interested["message"]["content"]
    with app.app_context():
        assert EnrollmentDraft.query.count() == 0
        assert Lead.query.filter_by(conversation_id=conversation_id).one().status == "qualified"


def test_confirmed_date_interest_starts_at_name_then_mobile(
    client, conversation_id
):
    send(client, conversation_id, "Show me Dental Assisting course dates")
    send(client, conversation_id, "The 18 November date suits me")

    started = send(client, conversation_id, "Yes please").get_json()
    assert started["enrollment"]["status"] == "collecting"
    assert started["enrollment"]["selected_intake"]["start_date"] == "2026-11-18"
    assert "full name" in started["message"]["content"].casefold()
    assert "NRIC/FIN" not in started["message"]["content"]

    named = send(client, conversation_id, "My name is Alex Lim").get_json()
    assert "mobile number" in named["message"]["content"].casefold()
    assert "NRIC/FIN" not in named["message"]["content"]


def test_bare_displayed_date_is_understood_but_date_question_is_not_consent(
    client, conversation_id
):
    send(client, conversation_id, "Dental Assisting dates please")

    question = send(client, conversation_id, "Is 18 November still available?").get_json()
    # Answered as an enquiry, not taken as consent to enrol.
    assert question["routing"]["agent"] == "enquiry"
    assert "Would you like me to begin your enrollment" not in question["message"]["content"]

    selected = send(client, conversation_id, "18 November").get_json()
    assert selected["routing"] == {
        "intent": "enrollment_interest",
        "confidence": 1.0,
        "handler": "deterministic",
        "agent": "none",
        "rule": "displayed_intake_interest",
    }
    assert "Would you like me to begin your enrollment" in selected["message"]["content"]


def test_details_supplied_to_enrollment_offer_are_saved_without_restarting_course(
    app, client, conversation_id
):
    send(client, conversation_id, "How much is the dental assisting course?")
    offered = send(
        client, conversation_id, "I'm interested in joining the November intake."
    ).get_json()
    assert "2 matching intakes" in offered["message"]["content"]
    assert "18–19 November 2026" in offered["message"]["content"]
    assert "28–29 November 2026" in offered["message"]["content"]

    continued = send(
        client,
        conversation_id,
        "My name is Test User and my number is 91234567.",
    ).get_json()

    assert continued["routing"]["rule"] == "implicit_enrollment_acceptance"
    assert "Please choose a course" not in continued["message"]["content"]
    assert "Please choose the exact intake" in continued["message"]["content"]
    assert "18–19 November 2026" in continued["message"]["content"]
    assert "28–29 November 2026" in continued["message"]["content"]
    assert "16–17 December 2026" not in continued["message"]["content"]
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.course_id == "dental-assisting-basic"
        assert draft.full_name == "Test User"
        assert draft.mobile_number == "91234567"


def test_draft_mobile_change_prompts_then_repairs_without_staff_escalation(
    app, client, conversation_id
):
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "2")
    ready = send(
        client,
        conversation_id,
        "Full name: Test User; mobile: 89284713; email: test.user@example.com; "
        "DOB: 03/09/2004; NRIC: T0000000G",
    ).get_json()
    assert ready["enrollment"]["status"] == "awaiting_confirmation"

    change = send(
        client, conversation_id, "I want to change the mobile number please?"
    ).get_json()
    assert "What is your new" not in change["message"]["content"]
    assert "send the new 8-digit" in change["message"]["content"]
    assert "Needs correction" not in change["message"]["content"]
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.mobile_number == "89284713"
        assert draft.validation_errors == {}

    confused = send(client, conversation_id, "what?").get_json()
    assert confused["routing"]["rule"] == "pending_correction_clarification"
    assert "You asked to change your mobile number" in confused["message"]["content"]
    with app.app_context():
        assert StaffEscalation.query.count() == 0

    status = send(
        client, conversation_id, "is my enrollment confirmed or what else do you need?"
    ).get_json()["message"]["content"]
    assert "Current conversation" in status
    assert "Not confirmed yet" in status
    assert "pending change: mobile number" in status.casefold()
    assert "Next step: Send the new 8-digit mobile number" in status

    updated = send(client, conversation_id, "92345678").get_json()
    assert updated["enrollment"]["status"] == "awaiting_confirmation"
    assert updated["routing"]["rule"] == "pending_correction_value"
    assert "****5678" in updated["message"]["content"]
    with app.app_context():
        assert EnrollmentDraft.query.one().mobile_number == "92345678"


def test_nevermind_cancels_pending_phone_change_and_restores_confirmation(
    app, client, conversation_id
):
    send(client, conversation_id, "I want to enroll in Front Desk Operations")
    send(client, conversation_id, "1")
    send(
        client,
        conversation_id,
        "Full name: Joberts Stones; mobile: 89284713; email: joberts@example.com; "
        "DOB: 20/04/2000; NRIC: T0000000G",
    )
    send(client, conversation_id, "I want to change my phone number")

    kept = send(client, conversation_id, "wait nevermind").get_json()

    assert kept["routing"]["rule"] == "pending_correction_kept"
    assert kept["enrollment"]["status"] == "awaiting_confirmation"
    assert "kept your current details" in kept["message"]["content"]
    assert "Confirm enrollment" in kept["message"]["content"]
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.mobile_number == "89284713"
        assert draft.pending_corrections == []
        assert draft.validation_errors == {}


def test_paused_phone_change_resumes_with_exact_action_and_never_escalates(
    app, client, conversation_id
):
    send(client, conversation_id, "I want to enroll in Front Desk Operations")
    send(client, conversation_id, "1")
    send(
        client,
        conversation_id,
        "Full name: Joberts Stones; mobile: 89284713; email: joberts@example.com; "
        "DOB: 20/04/2000; NRIC: T0000000G",
    )
    send(client, conversation_id, "I want to change my phone number")
    send(client, conversation_id, "I want to continue later")

    resumed = send(client, conversation_id, "lets continue").get_json()
    assert "only pending action" in resumed["message"]["content"]
    assert "send the new 8-digit" in resumed["message"]["content"]

    required = send(client, conversation_id, "yes what is required?").get_json()
    assert required["routing"]["rule"] == "pending_correction_clarification"
    assert "You asked to change your mobile number" in required["message"]["content"]

    confused = send(client, conversation_id, "i dont understand?").get_json()
    assert confused["routing"]["rule"] == "pending_correction_clarification"
    with app.app_context():
        assert StaffEscalation.query.count() == 0


def test_saved_field_question_answers_from_draft_and_preserves_next_step(
    app, client, conversation_id, fake_ai
):
    send(client, conversation_id, "I want to enroll in Front Desk Operations")
    send(client, conversation_id, "1")
    send(client, conversation_id, "My name is Avery Tan")
    send(client, conversation_id, "You can reach me at 9123 4567")
    calls_before = len(fake_ai.classify_calls)

    recalled = send(
        client, conversation_id, "what phone number did i give you?"
    ).get_json()

    assert recalled["routing"]["handler"] == "deterministic"
    assert recalled["routing"]["rule"] == "saved_enrollment_detail_lookup"
    assert "ends in 4567" in recalled["message"]["content"]
    assert "91234567" not in recalled["message"]["content"]
    assert "email address" in recalled["message"]["content"].casefold()
    assert len(fake_ai.classify_calls) == calls_before
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.mobile_number == "91234567"
        assert draft.email is None


def test_saved_field_presence_and_all_details_questions_are_state_aware(
    client, conversation_id
):
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "1")
    send(client, conversation_id, "My name is Avery Tan")

    nric = send(client, conversation_id, "have i already given my nric?").get_json()
    assert "haven't provided your NRIC/FIN yet" in nric["message"]["content"]
    assert "mobile number" in nric["message"]["content"].casefold()

    details = send(client, conversation_id, "show me what details you saved").get_json()
    assert details["routing"]["rule"] == "saved_enrollment_details_lookup"
    assert "Avery Tan" in details["message"]["content"]
    assert "Still Needed" in details["message"]["content"]


def test_field_guidance_answers_format_question_without_losing_collection_state(
    app, client, conversation_id, fake_ai
):
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "1")
    send(client, conversation_id, "Full name: Avery Tan; mobile: 91234567; email: avery@example.com")
    calls_before = len(fake_ai.classify_calls)

    guidance = send(client, conversation_id, "what birthday format can i use?").get_json()

    assert guidance["routing"]["rule"] == "enrollment_field_guidance"
    assert "20/04/2000" in guidance["message"]["content"]
    assert "20 April 2000" in guidance["message"]["content"]
    assert "date of birth" in guidance["message"]["content"].casefold()
    assert len(fake_ai.classify_calls) == calls_before
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.date_of_birth is None
        assert draft.mobile_number == "91234567"


def test_dob_formats_and_out_of_order_particulars_are_normalized(
    app, client, conversation_id
):
    send(client, conversation_id, "I want to enroll in Front Desk Operations")
    send(client, conversation_id, "1")
    send(client, conversation_id, "My name is Avery Tan")

    dob = send(client, conversation_id, "20.04.2000").get_json()
    assert "Saved successfully" in dob["message"]["content"]
    assert "mobile number" in dob["message"]["content"].casefold()

    send(client, conversation_id, "9123-4567")
    send(client, conversation_id, "avery@example.com")
    ready = send(client, conversation_id, "T0000000G").get_json()

    assert ready["enrollment"]["status"] == "awaiting_confirmation"
    assert "20 April 2000" in ready["message"]["content"]
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.date_of_birth.isoformat() == "2000-04-20"
        assert draft.mobile_number == "91234567"


def test_month_first_dob_with_comma_is_not_truncated(
    app, client, conversation_id
):
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "1")
    send(
        client,
        conversation_id,
        "Full name: Avery Tan; mobile: 91234567; email: avery@example.com; "
        "birthday: April 20th, 2000; NRIC: T0000000G",
    )

    with app.app_context():
        assert EnrollmentDraft.query.one().date_of_birth.isoformat() == "2000-04-20"


def test_any_unconfirmed_identity_field_can_be_corrected_naturally(
    app, client, conversation_id
):
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "1")
    send(
        client,
        conversation_id,
        "Full name: Avery Tan; mobile: 91234567; email: old@example.com; "
        "DOB: 20 April 2000; NRIC: T0000000G",
    )

    prompt = send(client, conversation_id, "actually change my email").get_json()
    assert "new email address" in prompt["message"]["content"].casefold()

    updated = send(client, conversation_id, "new.address@example.com").get_json()
    assert updated["enrollment"]["status"] == "awaiting_confirmation"
    assert updated["routing"]["rule"] == "pending_correction_value"
    assert "n***@example.com" in updated["message"]["content"]
    with app.app_context():
        assert EnrollmentDraft.query.one().email == "new.address@example.com"


@pytest.mark.parametrize(
    ("request_text", "replacement", "attribute", "expected"),
    [
        ("change my full name", "Jordan Lee", "full_name", "Jordan Lee"),
        ("fix my birthday", "21 May 2001", "date_of_birth", "2001-05-21"),
        ("update my nric", "S1234567D", "nric", "S1234567D"),
    ],
)
def test_other_identity_corrections_keep_the_same_draft(
    app, client, conversation_id, request_text, replacement, attribute, expected
):
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "1")
    send(
        client,
        conversation_id,
        "Full name: Avery Tan; mobile: 91234567; email: old@example.com; "
        "DOB: 20 April 2000; NRIC: T0000000G",
    )

    prompt = send(client, conversation_id, request_text).get_json()
    assert "Keep current" in prompt["message"]["content"]
    updated = send(client, conversation_id, replacement).get_json()

    assert updated["enrollment"]["status"] == "awaiting_confirmation"
    assert updated["routing"]["rule"] == "pending_correction_value"
    with app.app_context():
        value = getattr(EnrollmentDraft.query.one(), attribute)
        actual = value.isoformat() if attribute == "date_of_birth" else value
        assert actual == expected


@pytest.mark.parametrize(
    ("replacement", "expected", "display"),
    [
        ("02092007", "2007-09-02", "2 September 2007"),
        ("it's 02092007", "2007-09-02", "2 September 2007"),
        ("020927", "1927-09-02", "2 September 1927"),
    ],
)
def test_compact_birthday_correction_is_saved_and_confirmed(
    app, client, conversation_id, fake_ai, replacement, expected, display
):
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "1")
    send(
        client, conversation_id,
        "Full name: Avery Tan; mobile: 91234567; email: old@example.com; "
        "DOB: 02/09/2004",
    )

    prompt = send(client, conversation_id, "i want to change my birthdate").get_json()
    assert "send the new date of birth" in prompt["message"]["content"].casefold()
    assert "formats such as" not in prompt["message"]["content"].casefold()

    model_calls = len(fake_ai.classify_calls), len(fake_ai.enrollment_calls)
    updated = send(client, conversation_id, replacement).get_json()
    assert updated["routing"]["rule"] == "pending_correction_value"
    assert (len(fake_ai.classify_calls), len(fake_ai.enrollment_calls)) == model_calls
    assert display in updated["message"]["content"]
    assert "NRIC/FIN" in updated["message"]["content"]
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.date_of_birth.isoformat() == expected
        assert draft.pending_corrections == []

    recalled = send(client, conversation_id, "so is my birthday changed?").get_json()
    assert recalled["routing"]["rule"] == "saved_enrollment_detail_lookup"
    assert display in recalled["message"]["content"]
    assert "NRIC/FIN" in recalled["message"]["content"]


def test_compact_birthday_is_saved_during_initial_collection(
    app, client, conversation_id
):
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "1")
    send(client, conversation_id, "My name is Avery Tan")
    send(client, conversation_id, "My mobile is 91234567")
    prompt = send(client, conversation_id, "My email is avery@example.com").get_json()
    assert "May I have your date of birth?" in prompt["message"]["content"]
    assert "You can type it" not in prompt["message"]["content"]

    reply = send(client, conversation_id, "02092007").get_json()
    assert "2 September 2007" in reply["message"]["content"]
    assert "NRIC/FIN" in reply["message"]["content"]
    with app.app_context():
        assert EnrollmentDraft.query.one().date_of_birth.isoformat() == "2007-09-02"


def test_invalid_compact_birthday_keeps_correction_open(
    app, client, conversation_id
):
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "1")
    send(
        client, conversation_id,
        "Full name: Avery Tan; mobile: 91234567; email: old@example.com; "
        "DOB: 02/09/2004",
    )
    send(client, conversation_id, "change my birthday")

    invalid = send(client, conversation_id, "32092007").get_json()
    assert "Please check your date of birth" in invalid["message"]["content"]
    assert "May I have your NRIC/FIN" not in invalid["message"]["content"]
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.date_of_birth.isoformat() == "2004-09-02"
        assert draft.pending_corrections == ["date_of_birth"]

    corrected = send(client, conversation_id, "02092007").get_json()
    assert "2 September 2007" in corrected["message"]["content"]
    with app.app_context():
        assert EnrollmentDraft.query.one().date_of_birth.isoformat() == "2007-09-02"


def test_why_field_is_needed_is_answered_without_staff_escalation(
    app, client, conversation_id, fake_ai
):
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "1")
    calls_before = len(fake_ai.classify_calls)

    answer = send(client, conversation_id, "why do you need my nric?").get_json()

    assert answer["routing"]["rule"] == "enrollment_field_guidance"
    assert "official registration" in answer["message"]["content"]
    assert "masked" in answer["message"]["content"]
    assert "full name" in answer["message"]["content"].casefold()
    assert len(fake_ai.classify_calls) == calls_before
    with app.app_context():
        assert StaffEscalation.query.count() == 0
