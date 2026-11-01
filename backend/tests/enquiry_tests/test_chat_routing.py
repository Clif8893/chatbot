"""Module A through the real chat endpoints: routing, hand-off, opt-out and follow-ups.

Ported from the retired Module A suites (lead nurturing, routing and FAQ,
unanswered questions) where the behaviour is still required, plus the new
cut-over contract between Module A and Module B.
"""

from datetime import timedelta

import pytest

from backend.app import create_app
from backend.config import Config
from backend.extensions import db
from backend.models import Conversation, Message, utc_now
from backend.models.communications import OutboundMessage
from backend.models.operations import ConsentRecord, FollowupRun, Lead, StaffEscalation
from backend.services.chat_service import ENROLLMENT_OFFER_FOLLOWUP
from backend.services.enquiry.enquiry_service import COURSE_UNSPECIFIED_HINT
from backend.tests.conftest import authenticated_client, send


TOKEN = "synthetic-automation-token"
SENDER = "6580000001"


def nurture_app(tmp_path, fake_ai, **overrides):
    config = {
        "TESTING": True,
        "SECRET_KEY": "enquiry-routing-test-secret",
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / 'enquiry.db').as_posix()}",
        "AUTOMATION_API_TOKEN": TOKEN,
        "FOLLOWUP_HOURS": (48, 96, 144),
    }
    config.update(overrides)
    return create_app(config, ai_service=fake_ai)


def whatsapp(app, message_id, body, sender=SENDER):
    client = app.test_client()
    response = client.post(
        "/api/development/inbound",
        json={"id": message_id, "from": sender, "body": body},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 202
    with app.app_context():
        app.extensions["process_inbox"]()
        reply = (
            OutboundMessage.query.filter_by(purpose="inbound_reply")
            .order_by(OutboundMessage.created_at.desc())
            .first()
        )
        return reply.body, reply.conversation_id


def content(response):
    return response.get_json()["message"]["content"]


def escalations(app, conversation_id):
    with app.app_context():
        return [
            row.category
            for row in StaffEscalation.query.filter_by(conversation_id=conversation_id)
        ]


# Routing ------------------------------------------------------------------


def test_course_question_is_answered_by_the_enquiry_agent(app, client, conversation_id, fake_ai):
    payload = send(client, conversation_id, "How much is the course fee?").get_json()
    assert "S$600 nett" in payload["message"]["content"]
    assert payload["message"]["agent_name"] == "enquiry_agent"
    assert payload["routing"]["agent"] == "enquiry"
    assert payload["routing"]["rule"] == "enquiry_grounded_answer"
    assert fake_ai.enquiry_calls[-1]["message"] == "How much is the course fee?"


def test_first_reply_has_menu_and_no_memory_notice_on_any_channel(app, client, conversation_id, tmp_path, fake_ai):
    notice = app.extensions["knowledge_base"].fixed_reply("first_contact_privacy")
    payload = send(client, conversation_id, "Hi").get_json()
    assert notice not in payload["message"]["content"]
    menu = {item.label for item in app.extensions["knowledge_base"].suggestions.values()}
    assert 1 <= len(payload["quick_replies"]) <= 3
    assert set(payload["quick_replies"]) <= menu
    assert notice not in content(send(client, conversation_id, "how much is the course fee?"))

    whatsapp_app = nurture_app(tmp_path, fake_ai)
    first, _ = whatsapp(whatsapp_app, "wa-1", "Hi")
    assert notice not in first


def test_every_chat_response_has_quick_replies_and_enquiries_name_their_type(app, client, conversation_id):
    menu = app.extensions["knowledge_base"].quick_replies
    created = client.post("/api/conversations").get_json()
    assert created["quick_replies"] == menu
    empty_history = client.get(f"/api/conversations/{created['conversation_id']}/messages").get_json()
    assert empty_history["quick_replies"] == menu

    enquiry = send(client, conversation_id, "How much is the course fee?").get_json()
    assert enquiry["routing"]["response_type"] == "grounded_answer"
    enrollment = send(client, conversation_id, "I want to enrol").get_json()
    assert enrollment["quick_replies"] == []
    assert "response_type" not in enrollment["routing"]

    history = client.get(f"/api/conversations/{conversation_id}/messages").get_json()
    assert history["quick_replies"] == []


def test_fallback_replies_are_flagged_in_routing(client, conversation_id, fake_ai):
    from backend.agents.schemas import AIServiceError

    def broken(**prompt):
        raise AIServiceError("timeout")

    fake_ai.converse_enquiry = broken
    payload = send(client, conversation_id, "How much is the course fee?").get_json()
    assert payload["routing"]["used_fallback"] is True
    ok = send(client, conversation_id, "I want to enrol").get_json()
    assert "used_fallback" not in ok["routing"]


def test_a_quick_reply_label_is_an_ordinary_message(app, client, conversation_id):
    reply = send(client, conversation_id, "Talk to staff").get_json()
    # The menu's staff option is caught deterministically, before classification.
    assert reply["routing"]["handler"] == "deterministic"
    assert reply["routing"]["agent"] == "enquiry"
    assert escalations(app, conversation_id) == ["human_request"]


def test_general_question_gets_a_brief_answer_and_steer_back(client, conversation_id):
    payload = send(client, conversation_id, "what is the capital of Japan?").get_json()
    assert payload["routing"]["rule"] == "enquiry_general_knowledge"
    assert "our courses" in payload["message"]["content"]


def test_unanswerable_question_names_the_subject_and_is_flagged_once(app, client, conversation_id):
    reply = content(send(client, conversation_id, "Is parking included with the course?"))
    assert "parking at the training venue" in reply
    send(client, conversation_id, "Is parking included with the course?")
    assert escalations(app, conversation_id) == ["unanswered_question"]


def test_request_for_a_person_is_escalated_even_mid_enrollment(app, client, conversation_id):
    send(client, conversation_id, "I want to enrol in Dental Assisting.")
    send(client, conversation_id, "1")
    reply = content(send(client, conversation_id, "Can I speak to a staff please?"))
    assert "we can continue where you left off" in reply
    assert escalations(app, conversation_id) == ["human_request"]


def test_crisis_message_is_handled_by_module_a_in_any_state(app, client, conversation_id):
    send(client, conversation_id, "I want to enrol in Dental Assisting.")
    reply = content(send(client, conversation_id, "honestly i want to die"))
    assert "1767" in reply
    assert escalations(app, conversation_id) == ["distress"]


def test_customer_reply_hides_internal_terminology(client, conversation_id):
    reply = content(send(client, conversation_id, "How much is the course fee?"))
    for term in ("da.fee_and_timing", "approved content", "kb_entry", "database"):
        assert term not in reply


# Hand-off to Module B -------------------------------------------------------


def test_natural_enrollment_request_starts_module_b(client, conversation_id, fake_ai):
    payload = send(client, conversation_id, "I want to enrol").get_json()
    assert payload["message"]["agent_name"] == "enrollment_agent"
    assert fake_ai.enquiry_calls == []


def test_question_mid_enrollment_is_answered_and_the_draft_is_resumable(client, conversation_id):
    send(client, conversation_id, "I want to enrol in Dental Assisting.")
    send(client, conversation_id, "1")
    payload = send(client, conversation_id, "how much is the course fee?").get_json()
    assert "S$600 nett" in payload["message"]["content"]
    assert "we can continue where you left off" in payload["message"]["content"]
    assert payload["enrollment"]["status"] == "collecting"


def test_enquiry_handoff_enters_the_module_b_interest_flow(app, client, conversation_id, fake_ai):
    original = fake_ai.converse_enquiry

    def wants_to_join(**prompt):
        from backend.agents.schemas import EnquiryTurn

        if "count me in" in prompt["message"]:
            return EnquiryTurn(
                reply="Great, let's get you started!",
                response_type="handoff_enrollment",
                enrollment_interest=True,
            )
        return original(**prompt)

    fake_ai.converse_enquiry = wants_to_join
    send(client, conversation_id, "Tell me about the dental assisting course")
    payload = send(client, conversation_id, "ok count me in").get_json()
    assert payload["routing"]["intent"] == "enrollment_interest"
    assert payload["routing"]["rule"] == "qualified_interest_confirmation"
    assert "Would you like me to begin your enrollment" in payload["message"]["content"]
    with app.app_context():
        assert Lead.query.filter_by(conversation_id=conversation_id).one().status == "qualified"


def test_introduction_offer_makes_yes_an_enrollment_answer(app, client, conversation_id):
    reply = content(send(client, conversation_id, "Tell me about the course"))
    assert "interested in enrolling, let me know" in reply
    with app.app_context():
        conversation = db.session.get(Conversation, conversation_id)
        assert conversation.pending_followup == ENROLLMENT_OFFER_FOLLOWUP
    payload = send(client, conversation_id, "yes").get_json()
    assert payload["routing"]["rule"] == "pending_enrollment_confirmation"
    assert payload["message"]["agent_name"] == "enrollment_agent"


# Memory through the API -------------------------------------------------------


def test_a_new_chat_remembers_the_participant(app, client, conversation_id, fake_ai):
    send(client, conversation_id, "How much is the Dental Assisting course fee?")
    second = client.post("/api/conversations").get_json()["conversation_id"]
    send(client, second, "hello again")
    prompt = fake_ai.enquiry_calls[-1]
    assert "How much is the Dental Assisting course fee?" in prompt["prior_conversations"]
    topic = app.extensions["knowledge_base"].entry("da.fee_and_timing").topic
    assert f"Topics already answered: {topic}" in prompt["profile"]
    assert "da.fee_and_timing" not in prompt["profile"]


def test_buttons_never_repeat_what_was_answered_now_or_in_earlier_chats(app, client, conversation_id):
    kb = app.extensions["knowledge_base"]
    fees = kb.suggestions["fees"].label
    menu = {item.label for item in kb.suggestions.values()}
    reply = send(client, conversation_id, "How much is the Dental Assisting course fee?").get_json()
    assert fees not in reply["quick_replies"]
    assert set(reply["quick_replies"]) <= menu

    # A returning participant's opening menu leaves out what they already asked.
    created = client.post("/api/conversations").get_json()
    assert fees not in created["quick_replies"]
    assert len(created["quick_replies"]) >= 3
    history = client.get(f"/api/conversations/{created['conversation_id']}/messages").get_json()
    assert history["quick_replies"] == created["quick_replies"]

    # A different participant still sees the full starter menu.
    from backend.tests.conftest import authenticated_client

    stranger = authenticated_client(app)
    assert stranger.post("/api/conversations").get_json()["quick_replies"] == kb.quick_replies


def test_forget_me_works_from_the_chat(app, client, conversation_id, fake_ai):
    send(client, conversation_id, "How much is the course fee?")
    reply = content(send(client, conversation_id, "please forget me"))
    assert reply == app.extensions["knowledge_base"].fixed_reply("forget_me")
    send(client, conversation_id, "hello")
    prompt = fake_ai.enquiry_calls[-1]
    assert "How much is the course fee?" not in prompt["conversation"]
    assert "(nothing remembered yet)" in prompt["profile"]


# Opt-out and follow-ups (ported) ----------------------------------------------


def test_default_followups_follow_the_workflow_deck(tmp_path, fake_ai):
    # 30 minutes after the last contact, then 1 day and 2 days after the
    # previous reminder.
    assert Config.FOLLOWUP_HOURS == (0.5, 24.5, 72.5)
    app = nurture_app(tmp_path, fake_ai, FOLLOWUP_HOURS=Config.FOLLOWUP_HOURS)
    _, conversation_id = whatsapp(app, "wa-1", "Hi")
    operations = app.extensions["operations"]
    with app.app_context():
        start = Lead.query.filter_by(conversation_id=conversation_id).one().last_contact_at
        due = [
            (minutes, operations.run_followups(now=start + timedelta(minutes=minutes))["queued"])
            for minutes in (29, 31, 24 * 60 + 29, 24 * 60 + 31, 72 * 60 + 29, 72 * 60 + 31)
        ]
    assert due == [
        (29, 0), (31, 1), (24 * 60 + 29, 0), (24 * 60 + 31, 1),
        (72 * 60 + 29, 0), (72 * 60 + 31, 1),
    ]


def test_web_chat_lead_is_reminded_in_the_chat_and_stops_on_reply(tmp_path, fake_ai):
    app = nurture_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    send(client, conversation_id, "How much is the course fee?")
    operations = app.extensions["operations"]
    with app.app_context():
        lead = Lead.query.filter_by(conversation_id=conversation_id).one()
        assert (lead.status, lead.channel) == ("awaiting_response", "web")
        start = lead.last_contact_at
        assert operations.run_followups(now=start + timedelta(hours=49))["queued"] == 1
        assert OutboundMessage.query.count() == 0  # nothing is emailed

    messages = client.get(f"/api/conversations/{conversation_id}/messages").get_json()["messages"]
    kb = app.extensions["knowledge_base"]
    assert (messages[-1]["role"], messages[-1]["content"]) == ("assistant", kb.followup_template(0))

    # Replying restarts the sequence from the new reply.
    send(client, conversation_id, "What are the course dates?")
    with app.app_context():
        lead = Lead.query.filter_by(conversation_id=conversation_id).one()
        assert (lead.sequence, lead.followup_count) == (2, 0)
        assert operations.run_followups(now=lead.last_contact_at + timedelta(hours=47))["queued"] == 0

    send(client, conversation_id, "STOP")
    with app.app_context():
        assert Lead.query.filter_by(conversation_id=conversation_id).one().status == "declined"
        assert operations.run_followups(now=utc_now() + timedelta(days=30))["queued"] == 0


def test_automation_worker_queues_without_sending_when_no_channel_is_configured(
    tmp_path, fake_ai
):
    app = nurture_app(tmp_path, fake_ai, EMAIL_DELIVERY_ENABLED=False, SMTP_HOST="",
                      WHATSAPP_ACCESS_TOKEN="", WHATSAPP_PHONE_NUMBER_ID="")
    _, conversation_id = whatsapp(app, "wa-1", "Hi")
    with app.app_context():
        lead = Lead.query.filter_by(conversation_id=conversation_id).one()
        lead.last_inbound_at = utc_now() - timedelta(hours=50)
        lead.last_contact_at = utc_now() - timedelta(hours=49)
        db.session.commit()

    result = app.test_cli_runner().invoke(args=["automation-worker", "--once"])

    assert result.exit_code == 0
    assert "queued but not sent" in result.stderr
    assert "'followups_queued': 1" in result.output
    with app.app_context():
        statuses = {row.status for row in OutboundMessage.query.all()}
    assert statuses == {"queued"}


def test_followups_use_templates_without_explicit_consent_and_stop_on_reply(tmp_path, fake_ai):
    app = nurture_app(tmp_path, fake_ai)
    _, conversation_id = whatsapp(app, "wa-1", "Hi")
    operations = app.extensions["operations"]
    with app.app_context():
        lead = Lead.query.filter_by(conversation_id=conversation_id).one()
        assert lead.status == "awaiting_response"
        start = lead.last_contact_at
        assert operations.run_followups(now=start + timedelta(hours=47))["queued"] == 0
        assert operations.run_followups(now=start + timedelta(hours=49))["queued"] == 1
        assert operations.run_followups(now=start + timedelta(hours=97))["queued"] == 1
        bodies = [
            row.body
            for row in OutboundMessage.query.filter(OutboundMessage.purpose.like("lead_followup_%"))
            .order_by(OutboundMessage.created_at)
            .all()
        ]
        kb = app.extensions["knowledge_base"]
        assert bodies == [kb.followup_template(0), kb.followup_template(1)]

    whatsapp(app, "wa-2", "What is the course fee?")
    with app.app_context():
        lead = Lead.query.filter_by(conversation_id=conversation_id).one()
        assert lead.sequence == 2
        assert FollowupRun.query.filter_by(lead_id=lead.id, sequence=2).count() == 0


@pytest.mark.parametrize("opt_out", ["STOP", "Please stop sending me reminders", "don't message me anymore"])
def test_stop_opts_out_and_cancels_queued_followups(tmp_path, fake_ai, opt_out):
    app = nurture_app(tmp_path, fake_ai)
    _, conversation_id = whatsapp(app, "wa-1", "Hi")
    operations = app.extensions["operations"]
    with app.app_context():
        start = Lead.query.filter_by(conversation_id=conversation_id).one().last_contact_at
        operations.run_followups(now=start + timedelta(hours=49))
    reply, _ = whatsapp(app, "wa-2", opt_out)
    assert reply == app.extensions["knowledge_base"].fixed_reply("opt_out")
    with app.app_context():
        lead = Lead.query.filter_by(conversation_id=conversation_id).one()
        assert lead.status == "declined"
        followup = OutboundMessage.query.filter(OutboundMessage.purpose.like("lead_followup_%")).one()
        assert followup.status == "cancelled"
        assert operations.run_followups(now=utc_now() + timedelta(days=30))["queued"] == 0
    # Messaging again does not silently restart reminders after an opt-out.
    whatsapp(app, "wa-3", "What is the course fee?")
    with app.app_context():
        assert Lead.query.filter_by(conversation_id=conversation_id).one().status == "declined"


@pytest.mark.parametrize("withdrawn, strict", [(True, False), (False, True)])
def test_withdrawn_consent_or_strict_mode_blocks_followups(tmp_path, fake_ai, withdrawn, strict):
    app = nurture_app(tmp_path, fake_ai, FOLLOWUP_REQUIRE_EXPLICIT_CONSENT=strict)
    _, conversation_id = whatsapp(app, "wa-1", "Hi")
    operations = app.extensions["operations"]
    with app.app_context():
        lead = Lead.query.filter_by(conversation_id=conversation_id).one()
        if withdrawn:
            db.session.add(ConsentRecord(conversation_id=conversation_id, granted=False, version="test", source="test"))
            db.session.commit()
        assert operations.run_followups(now=lead.last_contact_at + timedelta(hours=49))["queued"] == 0


def test_history_reload_shows_enquiry_replies(client, conversation_id):
    send(client, conversation_id, "How much is the course fee?")
    history = client.get(f"/api/conversations/{conversation_id}/messages").get_json()
    roles = [message["role"] for message in history["messages"]]
    assert roles == ["user", "assistant"]
    assert "S$600 nett" in history["messages"][1]["content"]
    assert all(message.get("image_url") is None for message in history["messages"])


def test_assistant_messages_are_stored_with_the_enquiry_agent_name(app, client, conversation_id):
    send(client, conversation_id, "How much is the course fee?")
    with app.app_context():
        stored = Message.query.filter_by(conversation_id=conversation_id, role="assistant").one()
        assert stored.agent_name == "enquiry_agent"


FREE_TEXT_COURSE_LIST = (
    "We currently offer 2 courses:\n"
    "1. Basic Certificate in Dental Assisting\n"
    "2. Certificate in Dental Clinic Front Desk Operations.\n"
    "If you're interested in any of these, let me know! 😊"
)


@pytest.mark.parametrize(
    ("reply", "course"),
    [
        ("i am interested in the 1st", "Basic Certificate in Dental Assisting"),
        ("I am interested in the first one", "Basic Certificate in Dental Assisting"),
        ("interested in the 2nd", "Certificate in Dental Clinic Front Desk Operations"),
        ("I'm interested in the second one", "Certificate in Dental Clinic Front Desk Operations"),
    ],
)
def test_position_points_at_a_course_list_the_assistant_wrote(app, client, conversation_id, reply, course):
    with app.app_context():
        conversation = db.session.get(Conversation, conversation_id)
        app.extensions["repository"].add_message(conversation, "assistant", FREE_TEXT_COURSE_LIST)
        db.session.commit()

    answer = content(send(client, conversation_id, reply))

    assert "Which course are you interested in?" not in answer
    assert f"interested in the {course}" in answer


def test_new_chat_question_without_a_course_is_not_scoped_by_memory(app, client, fake_ai):
    # An earlier chat about Dental Assisting is remembered for this participant.
    first = client.post("/api/conversations").get_json()["conversation_id"]
    send(client, first, "How much is the dental assisting course?")

    new_chat = client.post("/api/conversations").get_json()["conversation_id"]
    send(client, new_chat, "dates plz")

    assert COURSE_UNSPECIFIED_HINT in fake_ai.enquiry_calls[-1]["policy_hint"]


@pytest.mark.parametrize(
    "message", ["dental assisting dates plz", "what about the front desk course dates?"]
)
def test_question_naming_a_course_gets_no_scope_note(client, conversation_id, fake_ai, message):
    send(client, conversation_id, message)

    assert COURSE_UNSPECIFIED_HINT not in fake_ai.enquiry_calls[-1]["policy_hint"]


def test_course_chosen_earlier_in_this_chat_still_applies(client, conversation_id, fake_ai):
    send(client, conversation_id, "How much is the dental assisting course?")
    send(client, conversation_id, "dates plz")

    assert COURSE_UNSPECIFIED_HINT not in fake_ai.enquiry_calls[-1]["policy_hint"]
