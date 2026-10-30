"""Web chat buttons and quick-reply menu options ask which course when none is settled."""

import pytest

from backend.tests.conftest import send


BUTTONS = {
    "about": "Tell me about the course",
    "fee": "How much is the course fee?",
    "dates": "What course dates are available?",
    "enrol": "I’d like to enroll",
}
FD = "Certificate in Dental Clinic Front Desk Operations"


def press(client, conversation_id, action):
    return client.post(
        "/api/chat",
        json={"conversation_id": conversation_id, "message": BUTTONS[action], "quick_action": action},
    ).get_json()


def text(client, conversation_id, message):
    return send(client, conversation_id, message).get_json()["message"]["content"]


@pytest.mark.parametrize("action", ["about", "fee", "dates"])
def test_course_dependent_button_asks_which_course_after_hi(client, conversation_id, fake_ai, action):
    text(client, conversation_id, "hi")
    reply = press(client, conversation_id, action)["message"]["content"]
    assert reply.startswith("Which course would you like")
    assert "1. Basic Certificate in Dental Assisting" in reply
    assert reply.endswith(f"2. {FD}")  # no "Reply with the course number..." instruction line
    assert fake_ai.faq_calls == []


def test_choosing_a_course_answers_and_later_buttons_remember_it(client, conversation_id):
    text(client, conversation_id, "hi")
    press(client, conversation_id, "fee")
    fee = text(client, conversation_id, "2")
    assert "$450" in fee and "$600" not in fee
    dates = press(client, conversation_id, "dates")["message"]["content"]
    assert "2 December 2026" in dates and "18–19 November 2026" not in dates
    about = press(client, conversation_id, "about")["message"]["content"]
    assert FD in about and "Basic Certificate in Dental Assisting" not in about


def test_choosing_both_answers_for_both_and_the_next_button_asks_again(client, conversation_id):
    press(client, conversation_id, "dates")
    both = text(client, conversation_id, "both")
    assert "18–19 November 2026" in both and "2 December 2026" in both
    assert press(client, conversation_id, "fee")["message"]["content"].startswith("Which course would you like")


def test_course_name_reply_is_accepted(client, conversation_id):
    press(client, conversation_id, "fee")
    reply = text(client, conversation_id, "the dental assisting one")
    assert "$600 nett" in reply and "$450" not in reply


def test_button_answers_directly_once_a_course_was_discussed(client, conversation_id):
    text(client, conversation_id, "how much is the front desk course?")
    reply = press(client, conversation_id, "dates")["message"]["content"]
    assert "2 December 2026" in reply and "18–19 November 2026" not in reply


def test_enrol_button_without_a_course_asks_for_one(client, conversation_id):
    text(client, conversation_id, "hi")
    reply = press(client, conversation_id, "enrol")["message"]["content"]
    assert "Please choose a course" in reply


def test_enrol_button_after_choosing_a_course_enrols_in_it(client, conversation_id):
    press(client, conversation_id, "about")
    text(client, conversation_id, "1")
    payload = press(client, conversation_id, "enrol")
    assert payload["routing"]["intent"] == "enrollment"
    assert "Please choose a course" not in payload["message"]["content"]


def test_ignoring_the_course_question_continues_normally(client, conversation_id):
    press(client, conversation_id, "fee")
    reply = text(client, conversation_id, "Where is the venue?")
    assert "venue" in reply.casefold()
    assert not reply.startswith("Which course")


def test_unknown_button_is_rejected(client, conversation_id):
    response = client.post(
        "/api/chat",
        json={"conversation_id": conversation_id, "message": "hello", "quick_action": "delete_everything"},
    )
    assert response.status_code == 400


@pytest.mark.parametrize("option", ["2", "3", "4", "5", "6"])
def test_course_dependent_menu_options_ask_which_course(client, conversation_id, option):
    text(client, conversation_id, "Hi")
    reply = text(client, conversation_id, option)
    assert reply.startswith("Which course would you like this information for?")


def test_menu_course_choice_answers_and_keeps_the_menu_open(client, conversation_id):
    text(client, conversation_id, "Hi")
    text(client, conversation_id, "4")
    dates = text(client, conversation_id, "front desk")
    assert "2 December 2026" in dates and "18–19 November 2026" not in dates
    assert "ask in your own words" in dates
    fees = text(client, conversation_id, "2")
    assert "$450" in fees and "$600" not in fees


def test_about_the_courses_menu_option_still_covers_both(client, conversation_id):
    text(client, conversation_id, "Hi")
    reply = text(client, conversation_id, "1")
    assert "The course fee is $600 nett" in reply and "The course fee is $450 nett" in reply


@pytest.mark.parametrize(
    "reply, expected",
    [
        ("1", "DA"), ("2", "FD"), ("the first one", "DA"), ("second", "FD"), ("2nd one", "FD"),
        ("front desk", "FD"), ("the front desk one", "FD"), ("dental assisting", "DA"), ("assisting", "DA"),
        ("DA", "DA"), ("fd", "FD"), ("the dental assisting one pls", "DA"),
        ("both", "BOTH"), ("both please", "BOTH"), ("1 and 2", "BOTH"), ("1,2", "BOTH"), ("all of them", "BOTH"),
    ],
)
def test_any_way_of_choosing_gets_the_requested_information(client, conversation_id, reply, expected):
    press(client, conversation_id, "fee")
    answer = text(client, conversation_id, reply)
    shown = {course for course, fee in (("DA", "$600"), ("FD", "$450")) if fee in answer}
    assert shown == ({"DA", "FD"} if expected == "BOTH" else {expected}), reply


def test_ambiguous_choice_asks_again(client, conversation_id):
    press(client, conversation_id, "fee")
    again = text(client, conversation_id, "dental")
    assert again.startswith("Which course would you like?")
    assert "$450" in text(client, conversation_id, "2")


def test_new_question_naming_a_course_is_answered_instead_of_the_button_topic(client, conversation_id):
    press(client, conversation_id, "fee")
    reply = text(client, conversation_id, "what about job opportunities for front desk?")
    assert "job opportunities" in reply and FD in reply
    assert "$450" not in reply


SFC_SENTENCE = "Payment wise, you may use Skillsfuture Credit to pay for the course fee."


def test_course_fee_button_omits_the_skillsfuture_sentence(client, conversation_id):
    press(client, conversation_id, "fee")
    assert SFC_SENTENCE not in text(client, conversation_id, "1")
    assert SFC_SENTENCE not in text(client, conversation_id, "both")
    assert SFC_SENTENCE not in text(client, conversation_id, "how much is the dental assisting course?")
    assert SFC_SENTENCE not in press(client, conversation_id, "fee")["message"]["content"]


def test_only_the_course_introduction_keeps_the_skillsfuture_sentence(client, conversation_id):
    # Fee answers leave it out; the introduction (About the course) keeps it.
    assert SFC_SENTENCE not in text(client, conversation_id, "what is the course fee")
    assert SFC_SENTENCE not in text(client, conversation_id, "what is the dental assisting course fee")
    # The conversation is now about Dental Assisting, so the button answers directly.
    assert SFC_SENTENCE in press(client, conversation_id, "about")["message"]["content"]
