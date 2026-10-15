"""Regression coverage for deterministic SkillsFuture FAQ attachments."""

import pytest

from backend.tests.conftest import send


EXPECTED_URL = "/assets/skillsfuture-credit-guide-awaiting-approval.svg"


@pytest.mark.parametrize(
    "question",
    [
        "Can I use Mid-Career SkillsFuture Credit?",
        "Which SkillsFuture tier can I use?",
        "Can I use skilsfuture credts for this course?",
    ],
)
def test_skillsfuture_questions_use_written_answer_without_unapproved_attachment(
    client, conversation_id, question
):
    response = send(client, conversation_id, question)
    assert response.status_code == 200
    message = response.get_json()["message"]

    assert "The Basic Certificate in Dental Assisting course is only eligible for the \"Basic Tier\"" in message["content"]
    assert "image_url" not in message
    assert "image_alt" not in message
    assert "image_status" not in message


def test_unapproved_skillsfuture_attachment_is_absent_after_history_reload(
    client, conversation_id
):
    response = send(
        client, conversation_id, "Can I use Mid-Career SkillsFuture Credit?"
    )
    original = response.get_json()["message"]

    history = client.get(
        f"/api/conversations/{conversation_id}/messages"
    ).get_json()["messages"]
    restored = history[-1]

    assert restored["id"] == original["id"]
    assert "image_url" not in original
    assert "image_url" not in restored
    assert "image_alt" not in restored


def test_attachment_metadata_is_not_sent_to_the_ai_service(
    client, conversation_id, fake_ai
):
    send(client, conversation_id, "Which SkillsFuture tier can I use?")
    call = fake_ai.faq_calls[-1]
    assert EXPECTED_URL not in call["message"]
    assert EXPECTED_URL not in call["context"]
    assert EXPECTED_URL not in call["catalogue_context"]
    assert "image_alt" not in call["catalogue_context"]


def test_unrelated_faq_and_enrollment_replies_have_no_attachment(
    client, conversation_id
):
    fee = send(client, conversation_id, "How much is the course fee?").get_json()
    assert "image_url" not in fee["message"]
    assert "image_alt" not in fee["message"]

    enrollment = send(client, conversation_id, "I want to enroll").get_json()
    assert "image_url" not in enrollment["message"]
    assert "image_alt" not in enrollment["message"]


def test_skillsfuture_faq_interruption_does_not_expose_unapproved_attachment(
    client, conversation_id
):
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "1")
    send(client, conversation_id, "My name is Test Student")
    faq = send(
        client,
        conversation_id,
        "Before continuing, which SkillsFuture tier can I use?",
    ).get_json()
    assert "image_url" not in faq["message"]

    resumed = send(client, conversation_id, "My NRIC is S1234567D").get_json()
    assert "image_url" not in resumed["message"]
