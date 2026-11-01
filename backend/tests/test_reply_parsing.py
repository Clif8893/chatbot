"""Shared interpretation of short replies to displayed lists and yes/no prompts."""

import pytest

from backend.services.reply_parsing import choice_position, referenced_position, reply_decision


@pytest.mark.parametrize(
    ("reply", "position"),
    [
        ("2", 2),
        ("2nd", 2),
        ("second", 2),
        ("the second one", 2),
        ("the 2nd one", 2),
        ("option 2", 2),
        ("no. 2", 2),
        ("#3", 3),
        ("3rd intake", 3),
        ("I'll take the 2nd", 2),
        ("ok 2", 2),
        ("2 la", 2),
        ("option one", 1),
        ("28th", 28),
    ],
)
def test_choice_position_reads_common_list_replies(reply, position):
    assert choice_position(reply) == position


@pytest.mark.parametrize(
    "reply",
    ["2nd December", "28 nov", "28-29", "91234567", "course 2", "that one",
     "is option 2 available", ""],
)
def test_choice_position_leaves_dates_and_other_text_alone(reply):
    assert choice_position(reply) is None


@pytest.mark.parametrize(
    "reply",
    ["yes", "y", "yep", "ya", "yeah", "ok", "okay confirm", "sure!", "can",
     "looks good", "yes, that's correct", "everything is correct", "go ahead",
     "proceed", "👍"],
)
def test_reply_decision_reads_yes(reply):
    assert reply_decision(reply) == "yes"


@pytest.mark.parametrize(
    "reply",
    ["no", "n", "nope", "nah", "not correct", "no thanks", "not now",
     "maybe later", "wait", "don't cancel"],
)
def test_reply_decision_reads_no(reply):
    assert reply_decision(reply, actions={"cancel"}) == "no"


@pytest.mark.parametrize(
    "reply",
    ["no I want to reschedule instead", "good morning", "no problem",
     "is it confirmed?", "no. 2", "what is the fee"],
)
def test_reply_decision_ignores_replies_that_ask_for_something_else(reply):
    assert reply_decision(reply) is None


def test_reply_decision_accepts_step_words_only_when_given():
    assert reply_decision("yes cancel it") is None
    assert reply_decision("yes cancel it", actions={"cancel"}) == "yes"


@pytest.mark.parametrize(
    ("reply", "position"),
    [
        ("i am interested in the 1st", 1),
        ("I am interested in the first", 1),
        ("the second one works for me", 2),
        ("can I go with option 2", 2),
        ("i like number 2", 2),
        ("#2 sounds good", 2),
        ("1.", 1),
    ],
)
def test_referenced_position_finds_a_choice_inside_a_sentence(reply, position):
    assert referenced_position(reply) == position


@pytest.mark.parametrize(
    "reply",
    ["the 1st of December", "28th Nov works", "first, how much is it?", "2 days course?",
     "first time doing this", "I prefer the 2nd and 3rd", "the 2nd day",
     "interested in dental assisting"],
)
def test_referenced_position_ignores_dates_counts_and_several_choices(reply):
    assert referenced_position(reply) is None
