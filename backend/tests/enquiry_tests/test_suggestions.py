"""Suggestion buttons come only from the approved menu and never repeat answers."""

import pytest

from backend.services.course_catalogue import CourseCatalogue
from backend.services.enquiry.knowledge_base import KnowledgeBase
from backend.services.enquiry.suggestions import MAX_SUGGESTIONS, choose, starter_labels


@pytest.fixture(scope="module")
def kb():
    catalogue = CourseCatalogue()
    catalogue.bind_intake_provider(lambda: [])
    return KnowledgeBase(catalogue)


def labels(kb, *ids):
    return [kb.suggestions[key].label for key in ids]


def test_every_menu_label_is_specific_and_short(kb):
    vague = ("what else", "anything else", "more info", "tell me more", "other questions")
    for item in kb.suggestions.values():
        assert len(item.label) <= 40
        assert not any(phrase in item.label.casefold() for phrase in vague), item.label


def test_model_ids_are_kept_in_order_when_relevant(kb):
    assert choose(kb, ["utap", "dates"], set(), response_type="grounded_answer") == labels(
        kb, "utap", "dates"
    )


def test_unknown_ids_and_duplicates_are_dropped_and_capped(kb):
    chosen = choose(
        kb,
        ["What else can I ask?", "dates", "dates", "jobs", "utap", "paynow"],
        set(),
        response_type="grounded_answer",
    )
    assert chosen == labels(kb, "dates", "jobs", "utap")
    assert len(chosen) == MAX_SUGGESTIONS


def test_answered_topics_hide_their_buttons(kb):
    answered = {"da.fee_and_timing", "da.course_content"}
    chosen = choose(kb, ["fees", "content", "jobs"], answered, response_type="grounded_answer")
    assert chosen == labels(kb, "jobs")


def test_fallback_order_skips_answered_topics(kb):
    answered = {"da.course_content", "general.courses_offered"}
    chosen = choose(kb, [], answered, response_type="small_talk")
    # "content" and "fees" are covered, so the order continues from "dates".
    assert chosen == labels(kb, "dates", "requirements", "jobs")


def test_enrol_is_hidden_while_already_enrolling(kb):
    chosen = choose(kb, ["enrol", "jobs"], set(), response_type="grounded_answer", enrolling=True)
    assert chosen == labels(kb, "jobs")


@pytest.mark.parametrize(
    ("response_type", "category"),
    [
        ("opt_out", ""),
        ("forget_me", ""),
        ("handoff_enrollment", ""),
        ("deescalation", "distress"),
        ("boundary", "abusive_conduct"),
        ("small_talk", "human_request"),
    ],
)
def test_no_buttons_when_they_would_be_unhelpful(kb, response_type, category):
    assert choose(kb, ["fees"], set(), response_type=response_type, escalation_category=category) == []


def test_a_frustrated_participant_is_always_offered_a_person(kb):
    # Live evaluation: rude or frustrated participants got only course buttons.
    chosen = choose(kb, ["fees", "dates"], set(), response_type="deescalation")
    assert chosen == labels(kb, "staff", "fees", "dates")
    assert choose(kb, [], set(), response_type="deescalation")[0] == labels(kb, "staff")[0]


def test_a_declined_question_still_gets_buttons_to_steer_back(kb):
    assert choose(kb, ["fees"], set(), response_type="boundary") == labels(kb, "fees")


def test_starter_menu_is_personalised_and_topped_up(kb):
    assert starter_labels(kb, set()) == kb.quick_replies
    answered = {"da.fee_and_timing", "da.intake_dates", "da.minimum_qualification"}
    starter = starter_labels(kb, answered)
    assert labels(kb, "fees")[0] not in starter
    assert labels(kb, "dates")[0] not in starter
    assert len(starter) >= 3
