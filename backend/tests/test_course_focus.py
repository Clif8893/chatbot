"""Replies use the Reply Template wording and cover only the course(s) the participant means."""

import pytest

from backend.extensions import db
from backend.services.chat_service import _is_fee_question
from backend.tests.conftest import send


DA = "Basic Certificate in Dental Assisting"
FD = "Certificate in Dental Clinic Front Desk Operations"

# Facts that identify each course in a reply. Reply Template sentences such as
# "The course fee is $600 nett ..." do not repeat the course name.
COURSE_MARKERS = {
    "DA": ("Dental Assisting", "$600", "930am", "2-day course", "18–19 November 2026", "GCE 'N' Levels",
           "suitable candidates", 'only eligible for the "Basic Tier"', "201841969G", "$250 per year"),
    "FD": ("Front Desk", "$450", "2 December 2026"),
}

# Verbatim from "2 days DA course Reply Template.docx".
TEMPLATE_JOBS = (
    "We do offer job opportunities for suitable candidates on the last day of the class.\n\n"
    "Selection process from the HR involves various contributing factors to ensure we find the best fit "
    "for the role. Factors includes\n"
    "- Location of work place\n- Schedule\n- Skills and Experience\n- Cultural Fit\n- Interview Performance"
)
TEMPLATE_QUALIFICATION = (
    "We prefer candidates with GCE 'N' Levels. However, if you do not have GCE 'N' Levels, we are open "
    "to considering candidates who have basic reading and writing skills in English, as the assessment "
    "will be conducted in English. :)"
)


def content(client, conversation_id, message):
    return send(client, conversation_id, message).get_json()["message"]["content"]


def about(reply):
    return {course for course, markers in COURSE_MARKERS.items() if any(m in reply for m in markers)}


# Exact Reply Template answers ------------------------------------------------


def test_job_question_gets_the_exact_reply_template_answer(client, conversation_id):
    reply = content(client, conversation_id, "Will I be offered a position at the end of the course?")
    assert reply == TEMPLATE_JOBS


def test_qualification_question_gets_the_exact_reply_template_answer(client, conversation_id):
    reply = content(client, conversation_id, "What is the minimum qualification to attend 2 days DA course?")
    assert reply == TEMPLATE_QUALIFICATION


def test_course_specific_expensive_question_gets_the_exact_reply_template_answer(client, conversation_id):
    reply = content(client, conversation_id, "why is the dental assisting course so expensive?")
    assert reply.startswith("Thank you for your feedback regarding the course fee.")
    assert "The fee for our 2-day course includes comprehensive materials" in reply
    assert reply.endswith("please feel free to reach out :)")
    assert FD not in reply


def test_midcareer_question_gets_the_reply_template_answer(client, conversation_id):
    reply = content(client, conversation_id, "Can I use Midcareer SkillsFuture for dental assisting?")
    assert reply.startswith(
        "There are two tiers of SkillsFuture Credit. The Basic Certificate in Dental Assisting course is only "
        'eligible for the "Basic Tier". You cannot use Midcareer Skillsfuture Credit to pay for this course.'
    )
    assert "*" not in reply
    assert reply.endswith(
        'You may login into the Skillsfuture Portal to find out the balance of your "Basic Tier" skillsfuture '
        "credits using https://sfc.myskillsfuture.gov.sg/claim."
    )
    assert "https://sfc.myskillsfuture.gov.sg/claim" in reply
    assert FD not in reply


def test_intro_uses_the_reply_template_with_current_dates(client, conversation_id):
    reply = content(client, conversation_id, "Tell me about the dental assisting course")
    assert reply.startswith("Hi !\n\nThank you for your interest in our Basic Certificate in Dental Assisting course.")
    assert "• 18–19 November 2026 (Weekdays)" in reply
    assert "15th to 16th July 2026" not in reply  # historical template dates are never offered
    assert "The course fee is $600 nett for a duration of 2 consecutive days, from 930am to 530pm." in reply
    assert "Clementi Loop" not in reply  # venue still awaits confirmation


# Tailoring to the question ---------------------------------------------------


@pytest.mark.parametrize(
    "question",
    ["why is ur course so expensive?", "Why are your courses so expensive?", "Is your course overpriced?"],
)
def test_generic_fee_concern_covers_both_courses(client, conversation_id, question):
    reply = content(client, conversation_id, question)
    assert about(reply) == {"DA", "FD"}
    assert "Thank you for your feedback regarding the course fee." in reply


@pytest.mark.parametrize(
    "message, expected",
    [
        ("why the front desk one so ex ah", True),
        ("very pricey leh", True),
        ("can I afford it", True),
        ("my ex-colleague recommended it", False),
        ("is there an exam", False),
    ],
)
def test_fee_concern_wording_includes_informal_phrasing(message, expected):
    # The FAQ agent's fee topic is kept only when the message is about cost.
    assert _is_fee_question(message) is expected


def test_generic_fact_question_answers_both_courses_instead_of_asking(client, conversation_id, fake_ai):
    fee = content(client, conversation_id, "How much is the course?")
    assert "Which course would you like to check?" not in fee
    assert about(fee) == {"DA", "FD"}
    paynow = content(client, client.post("/api/conversations").get_json()["conversation_id"], "Can I pay by PayNow?")
    assert "201841969G" in paynow and FD in paynow
    assert fake_ai.faq_calls == []


@pytest.mark.parametrize(
    "question, included, excluded",
    [
        ("Can I use SkillsFuture for the front desk course?", ["has not been confirmed"], ['only eligible for the "Basic Tier"']),
        ("Can I use SkillsFuture for dental assisting?", ['only eligible for the "Basic Tier"'], [FD]),
        ("What is the minimum qualification for front desk operations?", ["Entry requirements for the"], ["GCE 'N' Levels"]),
        ("Will I get a job after the dental assisting course?", ["suitable candidates"], [FD]),
        ("Will I be offered a position at the end of the front desk course?", ["has not been supplied"], ["suitable candidates"]),
    ],
)
def test_faq_topics_follow_the_named_course(client, conversation_id, question, included, excluded):
    reply = content(client, conversation_id, question)
    for text in included:
        assert text in reply, question
    for text in excluded:
        assert text not in reply, question


def test_generic_funding_answers_do_not_imply_front_desk_funding(client):
    # The SkillsFuture reply names the Dental Assisting course itself; UTAP and payment
    # replies add the Front Desk line so they cannot be read as covering it.
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    skillsfuture = content(client, conversation_id, "Can I use SkillsFuture?")
    assert "The Basic Certificate in Dental Assisting course is only eligible" in skillsfuture
    assert FD not in skillsfuture
    for question in ("Does UTAP work for your courses?", "What are the payment options?"):
        conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
        reply = content(client, conversation_id, question)
        assert "DA" in about(reply) and FD in reply and "not been confirmed" in reply, question
    utap = content(client, client.post("/api/conversations").get_json()["conversation_id"], "How does UTAP work?")
    assert "(UTAP) for this course" not in utap
    assert utap.endswith('The eligibility of "Basic Tier" Skillsfuture Credits and UTAP for the ' + FD + " has not been confirmed yet.")
    # Steps 1, 2 and 3 are separated by a blank line.
    assert "www.ntuc.org.sg\n\nStep 2: Enroll in the course" in utap
    assert "training.\n\nStep 3: After completing the course" in utap


def test_dates_follow_the_named_course(client, conversation_id):
    front_desk = content(client, conversation_id, "When is the next front desk class?")
    assert about(front_desk) == {"FD"}
    fresh = client.post("/api/conversations").get_json()["conversation_id"]
    both = content(client, fresh, "What course dates are available?")
    assert about(both) == {"DA", "FD"}


def test_naming_both_courses_gets_the_combined_answer(client, conversation_id):
    reply = content(client, conversation_id, "How much are dental assisting and front desk?")
    assert about(reply) == {"DA", "FD"}


def test_staff_can_save_per_course_answers_but_not_for_unknown_courses(app):
    operations = app.extensions["operations"]
    with app.app_context():
        faqs = app.extensions["catalogue"].public_summary()["faqs"]
        edited = [dict(item) for item in faqs]
        edited[0]["course_answers"] = dict(edited[0]["course_answers"])
        edited[0]["course_answers"]["dental-clinic-front-desk-operations"] = "Updated front desk description."
        operations.update_content({"faqs": edited})
        db.session.rollback()
        invalid = [dict(item) for item in faqs]
        invalid[0]["course_answers"] = {"retired-course": "Old text"}
        with pytest.raises(ValueError):
            operations.update_content({"faqs": invalid})


@pytest.mark.parametrize(
    "question, course",
    [
        ("course fee for the 2nd course", "FD"),
        ("how much is the second course", "FD"),
        ("fee for course 2", "FD"),
        ("how much is the first one", "DA"),
        ("Is the 2-day course overpriced?", "DA"),
        ("how much is the 1-day course", "FD"),
        ("price of the 2 day course", "DA"),
        ("how much is the DA course", "DA"),
        ("how much is the frontdesk course", "FD"),
        ("how much is the $450 course", "FD"),
        ("how long is the front-desk course", "FD"),
        ("what time does course 1 start", "DA"),
        ("when is the next class for the second course", "FD"),
        ("dentl asisting course fee", "DA"),
    ],
)
def test_any_reference_to_one_course_gets_only_that_course(client, conversation_id, question, course):
    assert about(content(client, conversation_id, question)) == {course}, question


@pytest.mark.parametrize(
    "question",
    ["is the course 2 days long?", "how much are both courses", "I have $600 skillsfuture credits, how much is the fee?"],
)
def test_generic_wording_is_not_mistaken_for_one_course(client, conversation_id, question):
    assert about(content(client, conversation_id, question)) == {"DA", "FD"}, question


def test_follow_up_questions_stay_on_the_course_being_discussed(client, conversation_id):
    content(client, conversation_id, "course fee for the 2nd course")
    # The web chat's quick-action buttons send these fixed messages.
    for message in (
        "what time does it start?",
        "What course dates are available?",
        "Tell me about the course",
        "How much is the course fee?",
        "Can I use SkillsFuture?",
    ):
        assert about(content(client, conversation_id, message)) == {"FD"}, message


def test_your_course_or_both_resets_the_conversation_to_both_courses(client, conversation_id):
    content(client, conversation_id, "How much is the front desk course?")
    reset = content(client, conversation_id, "why is ur course so expensive?")
    assert about(reset) == {"DA", "FD"}
    dates = content(client, conversation_id, "What course dates are available?")
    assert about(dates) == {"DA", "FD"}


def test_fresh_generic_question_covers_both_courses(client, conversation_id):
    assert about(content(client, conversation_id, "How much is the course?")) == {"DA", "FD"}


def test_enrol_button_enrols_in_the_course_being_discussed(client, conversation_id):
    content(client, conversation_id, "What are the dates for the second course?")
    payload = send(client, conversation_id, "I’d like to enroll").get_json()
    assert payload["routing"]["intent"] == "enrollment"
    assert "Please choose a course" not in payload["message"]["content"]
    assert FD in payload["message"]["content"]


def test_menu_answers_follow_the_course_being_discussed(client, conversation_id):
    content(client, conversation_id, "How much is the dental assisting course?")
    content(client, conversation_id, "menu")
    dates = content(client, conversation_id, "4")
    assert "18–19 November 2026" in dates and "2 December 2026" not in dates


def test_enrolling_in_the_second_course_selects_it_directly(client, conversation_id):
    payload = send(client, conversation_id, "I want to enrol in the second course").get_json()
    assert payload["routing"]["intent"] == "enrollment"
    assert "Please choose a course" not in payload["message"]["content"]
    assert FD in payload["message"]["content"]


def test_followup_reminders_do_not_single_out_one_course(app):
    templates = app.extensions["catalogue"].lead_nurturing["followup_templates"]
    for template in templates:
        mentions = app.extensions["catalogue"].courses_mentioned(template)
        assert len(mentions) != 1, template


def test_agent_course_guess_never_narrows_a_generic_question(client, conversation_id, fake_ai, monkeypatch):
    original = fake_ai.answer_faq

    def over_focused(*args, **kwargs):
        result = original(*args, **kwargs)
        result.course_ids = ["dental-assisting-basic"]
        return result

    monkeypatch.setattr(fake_ai, "answer_faq", over_focused)
    generic = content(client, conversation_id, "How does UTAP work?")
    assert FD in generic
    fresh = client.post("/api/conversations").get_json()["conversation_id"]
    described = content(client, fresh, "How does UTAP work for the course meant for chairside work?")
    assert "$250 per year" in described and FD not in described


def test_job_role_wording_identifies_the_front_desk_course(client, conversation_id):
    reply = content(client, conversation_id, "how much is the course for clinic counter staff")
    assert about(reply) == {"FD"}
