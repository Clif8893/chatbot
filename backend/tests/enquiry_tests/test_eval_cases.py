"""Offline checks for the Module A evaluation set and its scoring (no model calls)."""

import re
from pathlib import Path

from backend.tests.conftest import authenticated_client, send
from backend.tests.enquiry_tests.eval.runner import (
    CATEGORIES,
    THRESHOLDS,
    CaseResult,
    check_turn,
    load_cases,
    run_case,
    summarise,
    validate_cases,
)


REQUIREMENTS = (
    Path(__file__).resolve().parents[3] / "docs" / "module-a" / "requirements.md"
)


def _requirement_ids():
    """Requirement ids declared in headings such as "... (REQ-A01)"."""
    return set(re.findall(r"\((REQ-A\d{2})\)", REQUIREMENTS.read_text(encoding="utf-8")))


def test_case_file_is_valid_and_traced_to_requirements():
    cases = load_cases()
    assert validate_cases(cases, _requirement_ids()) == []


def test_case_file_covers_every_category_and_requirement():
    cases = load_cases()
    assert len(cases) >= 60
    assert {case["category"] for case in cases} == CATEGORIES
    traced = {requirement for case in cases for requirement in case["requirements"]}
    # REQ-A10 (follow-ups) is scheduler behaviour, covered by test_followup_composer.
    assert _requirement_ids() - traced == {"REQ-A10"}
    assert set(THRESHOLDS) == CATEGORIES


def _payload(content, agent="enquiry", response_type="grounded_answer"):
    routing = {"agent": agent}
    if agent == "enquiry":
        routing["response_type"] = response_type
    return {"message": {"content": content}, "routing": routing}


def test_check_turn_passes_a_matching_reply():
    expect = {
        "response_type": ["grounded_answer"],
        "include": [["600"], ["skillsfuture", "sfc"]],
        "exclude": ["Clementi"],
        "escalation": None,
        "max_words": 12,
    }
    payload = _payload("The fee is S$600 nett and SFC can be used.\n\nTo help you, notice.")
    assert check_turn(payload, set(), expect, notices=("To help you, notice.",)) == []


def test_check_turn_reports_each_kind_of_failure():
    expect = {
        "module": "A",
        "response_type": ["general_knowledge"],
        "include": [["tokyo"]],
        "exclude": ["$"],
        "escalation": None,
        "max_words": 3,
    }
    failures = check_turn(_payload("It costs $600 in Osaka today."), {"distress"}, expect)
    assert any("response_type" in item for item in failures)
    assert any("missing any of ['tokyo']" in item for item in failures)
    assert any("excluded '$'" in item for item in failures)
    assert any("unexpected escalation" in item for item in failures)
    assert any("exceeds 3" in item for item in failures)


def test_check_turn_module_and_expected_escalation():
    assert check_turn(_payload("Pick a course", agent="enrollment"), set(), {"module": "B"}) == []
    assert check_turn(_payload("Hi"), set(), {"module": "B"}) == ["expected Module B, got Module A"]
    assert check_turn(_payload("Hi"), set(), {"escalation": "human_request"}) == [
        "expected escalation 'human_request', got []"
    ]
    assert check_turn(_payload("Hi"), {"human_request"}, {"escalation": "human_request"}) == []


def test_check_turn_flags_off_menu_too_many_and_repeated_buttons():
    menu = {"Fees & funding", "Upcoming dates", "Job opportunities", "Talk to staff"}
    payload = _payload("The fee is S$600 nett.")
    payload["quick_replies"] = ["What else can I ask?", "Fees & funding", "Upcoming dates", "Talk to staff"]
    failures = check_turn(payload, set(), {"buttons_exclude": ["Fees & funding"]}, menu_labels=menu)
    assert "4 buttons shown (maximum 3)" in failures
    assert "buttons not on the approved menu: ['What else can I ask?']" in failures
    assert "buttons repeat covered topics: ['Fees & funding']" in failures
    payload["quick_replies"] = ["Upcoming dates", "Job opportunities"]
    assert check_turn(payload, set(), {"buttons_exclude": ["Fees & funding"]}, menu_labels=menu) == []


def test_summary_rates_and_thresholds():
    results = [
        CaseResult("a", "faq", True),
        CaseResult("b", "faq", False, ["turn 1: missing"]),
        CaseResult("c", "control", True),
    ]
    summary = summarise(results)
    assert summary["passed"] == 2
    assert summary["categories"]["faq"]["rate"] == 0.5
    assert summary["categories"]["faq"]["meets_threshold"] is False
    assert summary["categories"]["control"]["meets_threshold"] is True
    assert summary["failures"] == [{"id": "b", "category": "faq", "failures": ["turn 1: missing"]}]


def test_runner_plays_cases_through_the_chat_endpoint(app):
    """The runner itself works end to end with the deterministic fake model."""
    cases = {case["id"]: case for case in load_cases()}
    fee = run_case(app, cases["faq-fee-da"], authenticated_client, send)
    assert fee.passed, fee.failures
    human = run_case(app, cases["control-human"], authenticated_client, send)
    assert human.passed, human.failures
    memory = run_case(app, cases["memory-forget"], authenticated_client, send)
    assert [turn.get("new_chat") for turn in memory.transcript].count(True) == 1
    assert memory.transcript[1]["routing"]["response_type"] == "forget_me"


def test_funding_cases_use_the_deterministic_calculator(app):
    for case in load_cases():
        if case["id"].startswith("funding-calculator-"):
            result = run_case(app, case, authenticated_client, send)
            assert result.passed, result.failures
