"""Questions approved content cannot answer get a reply framed around what was asked."""

import pytest

from backend.agents.schemas import FAQAnswer
from backend.models.operations import StaffEscalation
from backend.tests.conftest import send


def text(client, conversation_id, message):
    return send(client, conversation_id, message).get_json()["message"]["content"]


def test_unanswerable_question_names_what_was_asked_and_escalates(app, client, conversation_id):
    reply = text(client, conversation_id, "Is parking included with the course?")
    assert reply.startswith(
        "I’m sorry, I don’t have confirmed information about parking at the training venue at the moment."
    )
    assert "passed your question to our team" in reply
    with app.app_context():
        assert StaffEscalation.query.filter_by(
            conversation_id=conversation_id, category="unanswered_question"
        ).count() == 1


@pytest.mark.parametrize(
    "subject, framed",
    [
        ("bringing a child to class", True),
        ("NTUC membership for foreigners", True),
        ("", False),
        ("parking costs $5 per hour", False),  # facts or numbers are never repeated
        ("see https://example.com", False),
        ("yes, lunch is provided", False),
        ("a very long subject that goes on and on and on and keeps going past the limit", False),
    ],
)
def test_subject_is_used_only_when_it_safely_names_the_question(app, subject, framed):
    catalogue = app.extensions["catalogue"]
    reply = catalogue.unanswered_reply(subject)
    if framed:
        assert reply == f"I’m sorry, I don’t have confirmed information about {subject} at the moment. Our team will need to confirm it for you."
    else:
        assert reply == catalogue.staff_confirmation_message


def test_unclear_question_is_answered_not_greeted(client, conversation_id, fake_ai, monkeypatch):
    original = fake_ai.answer_faq

    def no_topic(*args, **kwargs):
        original(*args, **kwargs)
        return FAQAnswer(
            answer="I’m sorry, I don’t have confirmed information about that at the moment.",
            matched_topics=[],
            unanswered_subject="bringing a child to class",
        )

    monkeypatch.setattr(fake_ai, "answer_faq", no_topic)
    reply = text(client, conversation_id, "Can I bring my kid along?")  # the fake classifier says "unclear"
    assert "bringing a child to class" in reply
    assert "Would you like to learn more about the course" not in reply


def test_small_talk_still_gets_the_greeting(client, conversation_id):
    text(client, conversation_id, "What is the venue?")
    reply = text(client, conversation_id, "ok thanks")
    assert "Would you like to learn more about the course" in reply


def test_venue_answer_is_only_given_for_venue_questions(client, conversation_id, fake_ai, monkeypatch):
    original = fake_ai.answer_faq

    def picks_venue(*args, **kwargs):
        original(*args, **kwargs)
        return FAQAnswer(answer="x", matched_topics=["venue"], unanswered_subject="bringing your own uniform")

    monkeypatch.setattr(fake_ai, "answer_faq", picks_venue)
    reply = text(client, conversation_id, "do i need to bring my own uniform")
    assert "training venue has not been confirmed" not in reply
    assert "bringing your own uniform" in reply


def test_online_delivery_is_not_treated_as_a_venue_question(client, conversation_id):
    from backend.services.chat_service import _is_venue_question

    assert _is_venue_question("is the front desk course held online?") is False
    assert _is_venue_question("where is the course held?") is True


@pytest.mark.parametrize(
    "message, subject",
    [
        ("what's your refund policy if i cancel", "refunds and cancellations"),
        ("I want to make a complaint about my class", "your feedback"),
        ("I will take legal action", "this legal matter"),
    ],
)
def test_sensitive_hand_off_names_the_matter(app, client, conversation_id, message, subject):
    reply = text(client, conversation_id, message)
    assert reply.startswith(f"Thank you for reaching out about {subject}.")
    assert "passed your message to them" in reply
