"""Follow-ups such as "what about the other course?" repeat the last question for another course."""

import pytest

from backend.tests.conftest import send


FD = "Certificate in Dental Clinic Front Desk Operations"
MARKERS = {
    "DA": ("Dental Assisting", "$600", "18–19 November 2026", "suitable candidates", 'only eligible for the "Basic Tier"'),
    "FD": ("Front Desk", "$450", "2 December 2026"),
}


def text(client, conversation_id, message):
    return send(client, conversation_id, message).get_json()["message"]["content"]


def about(reply):
    return {course for course, markers in MARKERS.items() if any(m in reply for m in markers)}


@pytest.mark.parametrize(
    "first, follow_up, expected, topic_marker",
    [
        ("how much is the front desk course?", "what about the other course", {"DA"}, "$600"),
        ("how much is the front desk course?", "what about the other one?", {"DA"}, "$600"),
        ("how much is the front desk course?", "and the dental assisting one?", {"DA"}, "$600"),
        ("how much is the dental assisting course?", "how about front desk?", {"FD"}, "$450"),
        ("how much is the dental assisting course?", "same for course 2", {"FD"}, "$450"),
        ("can i get a job after the dental assisting course?", "what about the other course?", {"FD"}, "job opportunities"),
        ("when is the next front desk class", "what about the other course", {"DA"}, "Available Intake Dates"),
        ("when is the next front desk class", "what about both?", {"DA", "FD"}, "Available Intake Dates"),
        ("Can use Midcareer Skillsfuture for dental assisting?", "what about the other one", {"FD"}, "Skillsfuture Credits and UTAP"),
    ],
)
def test_follow_up_repeats_the_previous_topic_for_the_other_course(
    client, conversation_id, first, follow_up, expected, topic_marker
):
    text(client, conversation_id, first)
    reply = text(client, conversation_id, follow_up)
    assert about(reply) == expected, follow_up
    assert topic_marker in reply, follow_up
    assert not reply.startswith("Hi !")  # never falls back to the course introduction


def test_conversation_moves_to_the_other_course_after_the_follow_up(client, conversation_id):
    text(client, conversation_id, "how much is the front desk course?")
    text(client, conversation_id, "what about the other course")
    dates = text(client, conversation_id, "What course dates are available?")
    assert about(dates) == {"DA"}


def test_other_course_inside_a_full_question(client, conversation_id):
    text(client, conversation_id, "how much is the front desk course?")
    reply = text(client, conversation_id, "how much is the other course?")
    assert about(reply) == {"DA"}


def test_enrol_in_the_other_course(client, conversation_id):
    text(client, conversation_id, "how much is the front desk course?")
    payload = send(client, conversation_id, "i want to enrol in the other course").get_json()
    assert payload["routing"]["intent"] == "enrollment"
    assert "Basic Certificate in Dental Assisting" in payload["message"]["content"]
    assert "Please choose a course" not in payload["message"]["content"]


def test_other_course_after_an_answer_covering_both_asks_which(client, conversation_id):
    text(client, conversation_id, "what is the course fee")
    ask = text(client, conversation_id, "what about the other course")
    assert ask.startswith("Which course would you like?")
    reply = text(client, conversation_id, "2")
    assert "$450" in reply and "$600" not in reply


def test_a_new_question_is_not_treated_as_a_follow_up(client, conversation_id):
    text(client, conversation_id, "how much is the front desk course?")
    reply = text(client, conversation_id, "what about job opportunities for the other course?")
    assert "job opportunities" in reply and about(reply) == {"DA"}


def test_users_exact_transcript(client, conversation_id):
    first = text(client, conversation_id, "what is the course fee for Basic Certificate in Dental Assisting")
    assert first == "The course fee is $600 nett for a duration of 2 consecutive days, from 930am to 530pm."
    other = text(client, conversation_id, "what about the other course")
    assert other == f"The course fee for the {FD} is $450 nett for one training day."


def test_course_memory_covers_the_whole_chat_but_not_other_chats(client, conversation_id):
    text(client, conversation_id, "how much is the front desk course?")
    for _ in range(12):  # push the course mention far beyond the recent-message window
        text(client, conversation_id, "Where is the venue?")
    assert about(text(client, conversation_id, "What course dates are available?")) == {"FD"}
    new_chat = client.post("/api/conversations").get_json()["conversation_id"]
    assert about(text(client, new_chat, "What course dates are available?")) == {"DA", "FD"}


def test_other_course_in_a_new_chat_asks_which_course(client, conversation_id):
    reply = text(client, conversation_id, "what about the other course")
    assert reply.startswith("Which course would you like to know more about?")
