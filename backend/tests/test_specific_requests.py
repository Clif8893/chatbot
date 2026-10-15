"""A specific request gets only the information asked for, however it is phrased."""

import pytest

from backend.services.chat_service import _is_intake_availability_request
from backend.tests.conftest import send


def text(client, conversation_id, message):
    return send(client, conversation_id, message).get_json()["message"]["content"]


def test_users_exact_conversation(client, conversation_id):
    assert text(client, conversation_id, "course fee for Basic Certificate in Dental Assisting").startswith(
        "The course fee is $600 nett"
    )
    assert "$450" in text(client, conversation_id, "the other course?")
    dates = text(client, conversation_id, "state the available dates for Basic Certificate in Dental Assisting  only")
    assert dates.startswith("📅 Available Intake Dates")
    assert "18–19 November 2026" in dates and "2 December 2026" not in dates
    assert "course fee" not in dates and "Hi !" not in dates


@pytest.mark.parametrize(
    "message, included, excluded",
    [
        ("list the dates for front desk", "2 December 2026", "18–19 November 2026"),
        ("give me the intake dates for the dental assisting course", "18–19 November 2026", "2 December 2026"),
        ("dental assisting dates pls", "18–19 November 2026", "2 December 2026"),
        ("can i know the upcoming dates for front desk", "2 December 2026", "18–19 November 2026"),
        ("pls send the schedule for front desk", "2 December 2026", "18–19 November 2026"),
    ],
)
def test_any_phrasing_of_a_dates_request_gets_only_the_dates(client, conversation_id, message, included, excluded):
    reply = text(client, conversation_id, message)
    assert reply.startswith("📅 Available Intake Dates"), message
    assert included in reply and excluded not in reply, message
    assert "$600" not in reply and "$450" not in reply, message


@pytest.mark.parametrize(
    "message, expected_start",
    [
        ("tell me the training hours for dental assisting", "The course is for a duration of 2"),
        ("state the course fee for front desk", "The course fee for the Certificate in Dental Clinic Front Desk Operations"),
        ("list the payment methods for dental assisting", "There are three modes of payment."),
        ("what will i learn in the dental assisting course", "Dental Assisting is a valuable skill set"),
    ],
)
def test_specific_requests_are_not_answered_with_the_course_introduction(client, conversation_id, message, expected_start):
    reply = text(client, conversation_id, message)
    assert reply.startswith(expected_start), message
    assert not reply.startswith("Hi !"), message


def test_general_request_still_gets_the_introduction(client, conversation_id):
    reply = text(client, conversation_id, "Tell me about the dental assisting course")
    assert reply.startswith("Hi !\n\nThank you for your interest in our Basic Certificate in Dental Assisting course.")


@pytest.mark.parametrize(
    "message",
    ["my date of birth is 12 May 1990", "DOB 12/05/1990", "my preferred intake date is 18 November"],
)
def test_enrolment_answers_are_not_mistaken_for_a_dates_request(message):
    assert _is_intake_availability_request(message) is False
