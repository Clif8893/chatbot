"""Fact extraction shared by the knowledge-base allow-list and the fact guard."""

from decimal import Decimal

import pytest

from backend.services.enquiry.facts import (
    FactSet,
    date_range_facts,
    extract_facts,
    normalise_url,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("The course fee is S$600 nett.", {Decimal("600")}),
        ("Top up $100 via PayNow", {Decimal("100")}),
        ("It costs $1,200.50 in total", {Decimal("1200.50")}),
        ("about 600 dollars", {Decimal("600")}),
        ("SGD 450 for one day", {Decimal("450")}),
        ("up to $250 per year (or up to $500)", {Decimal("250"), Decimal("500")}),
        ("Two training days, no fee mentioned", set()),
    ],
)
def test_money_amounts(text, expected):
    assert extract_facts(text).amounts == expected


def test_equal_amounts_compare_equal_regardless_of_decimals():
    assert Decimal("600.00") in extract_facts("S$600").amounts


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("50% off the course fee", {Decimal("50")}),
        ("a 30 per cent discount", {Decimal("30")}),
        ("no percentages here", set()),
    ],
)
def test_percentages(text, expected):
    assert extract_facts(text).percents == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("18–19 November 2026 (Weekdays)", {(11, 18), (11, 19)}),
        ("15th to 16th July 2026", {(7, 15), (7, 16)}),
        ("25th & 26th July", {(7, 25), (7, 26)}),
        ("28 November–1 December 2026", {(11, 28), (12, 1)}),
        ("starting 2026-11-14", {(11, 14)}),
        ("class on Nov 3rd", {(11, 3)}),
        ("the 12th of August", {(8, 12)}),
        ("intakes in May 2026", set()),
        ("you may use SkillsFuture Credit", set()),
    ],
)
def test_dates(text, expected):
    assert extract_facts(text).dates == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("9:30am to 5:30pm", {570, 1050}),
        ("from 930am to 530pm", {570, 1050}),
        ("ends at 17:30", {1050}),
        ("lunch at 12pm", {720}),
        ("call 1767 any time", set()),
        ("an amazing 4 amenities", set()),
    ],
)
def test_times(text, expected):
    assert extract_facts(text).times == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Check here: https://sfc.myskillsfuture.gov.sg/claim.",
            {"sfc.myskillsfuture.gov.sg/claim"},
        ),
        ("log in at www.ntuc.org.sg", {"ntuc.org.sg"}),
        ("chat at ntuc.co/chat", {"ntuc.co/chat"}),
        ("submit your UTAP claim.You will need", set()),
        ("visit ntuc.coolstuff later", set()),
    ],
)
def test_urls(text, expected):
    assert extract_facts(text).urls == expected


def test_normalise_url_ignores_scheme_www_case_and_trailing_slash():
    assert normalise_url("HTTPS://www.Example.com/Path/") == "example.com/path"


def test_date_range_facts_cover_every_day_across_a_month_boundary():
    assert date_range_facts("2026-11-30", "2026-12-01").dates == {(11, 30), (12, 1)}


def test_union_and_empty():
    combined = extract_facts("S$600").union(extract_facts("9:30am"))
    assert combined.amounts == {Decimal("600")}
    assert combined.times == {570}
    assert FactSet().is_empty()
    assert not combined.is_empty()
