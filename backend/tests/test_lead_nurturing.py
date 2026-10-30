"""Module A coverage: quick-reply menu, enquiry commands, hand-off, opt-out and follow-ups."""

from datetime import timedelta

import pytest

from backend.app import create_app
from backend.extensions import db
from backend.models import Conversation, utc_now
from backend.models.communications import OutboundMessage
from backend.models.operations import ConsentRecord, FollowupRun, Lead, StaffEscalation
from backend.services.lead_nurturing import WELCOME_MENU_FOLLOWUP
from backend.tests.conftest import send


TOKEN = "synthetic-automation-token"
SENDER = "6580000001"


def nurture_app(tmp_path, fake_ai, **overrides):
    config = {
        "TESTING": True,
        "SECRET_KEY": "lead-nurturing-test-secret",
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / 'nurture.db').as_posix()}",
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


# Quick-reply menu --------------------------------------------------------


def test_first_contact_greeting_presents_numbered_quick_reply_menu(app, client, conversation_id, fake_ai):
    payload = send(client, conversation_id, "Hi").get_json()
    reply = payload["message"]["content"]
    assert "learn more about the course" in reply
    for label in ("1. About the courses", "3. SkillsFuture and UTAP funding", "7. Enrol now", "8. Talk to our team"):
        assert label in reply
    assert payload["routing"]["intent"] == "small_talk"
    assert payload["routing"]["handler"] == "crewai"
    assert len(fake_ai.classify_calls) == 1
    with app.app_context():
        assert db.session.get(Conversation, conversation_id).pending_followup == WELCOME_MENU_FOLLOWUP


def test_facebook_style_generic_first_message_gets_natural_course_intro(client, conversation_id):
    reply = content(send(client, conversation_id, "Hello! Can I get more info on this?"))
    assert "Thank you for your interest in our courses" in reply
    assert "Reply with a number" not in reply


def test_menu_number_answers_from_approved_content_without_the_model(client, conversation_id, fake_ai):
    send(client, conversation_id, "Hi")
    assert content(send(client, conversation_id, "3")).startswith("Which course would you like")
    reply = content(send(client, conversation_id, "1"))
    assert "The Basic Certificate in Dental Assisting course is only eligible for the \"Basic Tier\"" in reply
    assert "UTAP" in reply
    assert "ask in your own words" in reply
    assert fake_ai.faq_calls == []
    entry = content(send(client, conversation_id, "5"))
    assert "GCE 'N' Levels" in entry


def test_about_the_courses_describes_both_courses(client, conversation_id):
    send(client, conversation_id, "Hi")
    reply = content(send(client, conversation_id, "1"))
    assert "The course fee is $600 nett" in reply
    assert "The course fee is $450 nett" in reply
    assert "If you are interested in enrolling, let me know" in reply
    front_desk = content(send(client, conversation_id, "Tell me about the front desk course"))
    assert "essential skills for working at the front desk of a dental clinic" in front_desk
    assert "$450 nett" in front_desk


def test_menu_enrol_option_starts_the_enrollment_flow(client, conversation_id):
    send(client, conversation_id, "Hi")
    payload = send(client, conversation_id, "7").get_json()
    assert payload["routing"]["intent"] == "enrollment"
    assert "Basic Certificate in Dental Assisting" in payload["message"]["content"]


def test_menu_expires_after_another_reply(client, conversation_id):
    send(client, conversation_id, "Hi")
    send(client, conversation_id, "How much is the course?")
    reply = content(send(client, conversation_id, "3"))
    assert "UTAP" not in reply


def test_numbers_are_not_menu_choices_without_an_open_menu(client, conversation_id, fake_ai):
    send(client, conversation_id, "What is the course fee?")
    payload = send(client, conversation_id, "3").get_json()
    assert "UTAP" not in payload["message"]["content"]


# Natural-language equivalents (no slash commands) ------------------------------


def test_asking_for_help_or_the_menu_shows_options(client, conversation_id):
    send(client, conversation_id, "What is the course fee?")
    for message in ("help", "Show me the options", "menu"):
        reply = content(send(client, conversation_id, message))
        assert "ask in your own words" in reply
        assert "/help" not in reply


def test_slash_text_is_not_treated_as_a_command(client, conversation_id):
    reply = content(send(client, conversation_id, "/fees 1"))
    assert "Format:" not in reply
    assert "Quick commands" not in reply


def test_natural_language_covers_course_fee_dates_and_funding(client, conversation_id, fake_ai):
    courses = content(send(client, conversation_id, "What courses are available?"))
    assert "1. Basic Certificate in Dental Assisting — S$600 nett" in courses
    assert "2. Certificate in Dental Clinic Front Desk Operations — S$450 nett" in courses
    dates = content(send(client, conversation_id, "What course dates are available?"))
    assert "2 December 2026" in dates
    assert "18–19 November 2026" in dates
    fee = content(send(client, conversation_id, "How much is the dental assisting course?"))
    assert "$600 nett" in fee
    next_class = content(send(client, conversation_id, "when is the next dental assisting class"))
    assert "18–19 November 2026" in next_class
    assert fake_ai.faq_calls == []
    funding = content(send(client, conversation_id, "Can I use skillsfuture?"))
    assert "https://sfc.myskillsfuture.gov.sg/claim" in funding


def test_natural_enrolment_request_starts_enrollment(client, conversation_id):
    payload = send(client, conversation_id, "I'd like to enrol in the front desk course").get_json()
    assert payload["routing"]["intent"] == "enrollment"


# Human escalation --------------------------------------------------------


def test_request_for_a_person_is_escalated_to_staff(app, client, conversation_id):
    reply = content(send(client, conversation_id, "Can I speak to a staff please?"))
    assert "passed your message to our team" in reply
    with app.app_context():
        row = StaffEscalation.query.filter_by(conversation_id=conversation_id).one()
        assert (row.category, row.status) == ("human_request", "open")


def test_sensitive_message_is_escalated(app, client, conversation_id):
    send(client, conversation_id, "I want to make a complaint about my last class")
    with app.app_context():
        assert StaffEscalation.query.filter_by(
            conversation_id=conversation_id, category="sensitive"
        ).count() == 1


def test_unanswerable_question_is_flagged_once_for_staff(app, client, conversation_id):
    reply = content(send(client, conversation_id, "Is parking included with the course?"))
    assert "team will need to confirm" in reply.casefold()
    assert "passed your question to our team" in reply
    send(client, conversation_id, "Is parking included with the course?")
    with app.app_context():
        assert StaffEscalation.query.filter_by(
            conversation_id=conversation_id, category="unanswered_question", status="open"
        ).count() == 1


def test_menu_option_eight_escalates(app, client, conversation_id):
    send(client, conversation_id, "Hello")
    send(client, conversation_id, "8")
    with app.app_context():
        assert StaffEscalation.query.filter_by(conversation_id=conversation_id).count() == 1


# WhatsApp channel: first-contact notice, opt-out and follow-ups ------------------


def test_whatsapp_first_reply_discloses_reminders_but_web_reply_does_not(tmp_path, fake_ai, client, conversation_id):
    app = nurture_app(tmp_path, fake_ai)
    first, _ = whatsapp(app, "wa-1", "Hi")
    assert "Reply STOP at any time to opt out" in first
    second, _ = whatsapp(app, "wa-2", "What is the course fee?")
    assert "Reply STOP" not in second
    assert "Reply STOP" not in content(send(client, conversation_id, "Hi"))


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
        templates = app.extensions["catalogue"].lead_nurturing["followup_templates"]
        assert bodies == templates[:2]
        assert db.session.get(Conversation, conversation_id).pending_followup == WELCOME_MENU_FOLLOWUP

    # A numbered reply to the reminder uses the re-opened menu, and stops the sequence.
    reply, _ = whatsapp(app, "wa-2", "4")
    # The lead never named a course, so the menu asks which one first.
    assert reply.startswith("Which course would you like this information for?")
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
    assert "won’t receive any more reminders" in reply
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
