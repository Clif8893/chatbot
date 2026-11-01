"""Approved Module A content: sources, live facts, unconfirmed topics and validation."""

import copy
import json
import os
from decimal import Decimal

import pytest

from backend.services.course_catalogue import CATALOGUE_PATH, CourseCatalogue
from backend.services.enquiry.facts import extract_facts
from backend.services.enquiry.knowledge_base import (
    GENERAL,
    KNOWLEDGE_BASE_PATH,
    MISSING_VALUE,
    KnowledgeBase,
    KnowledgeBaseError,
)

DA = "dental-assisting-basic"
FD = "dental-clinic-front-desk-operations"
SYNTHETIC_INTAKES = [
    {"id": "da-1", "course_id": DA, "start_date": "2027-03-10", "end_date": "2027-03-11"},
    {"id": "da-2", "course_id": DA, "start_date": "2027-03-20", "end_date": "2027-03-21"},
    {"id": "fd-1", "course_id": FD, "start_date": "2027-04-05", "end_date": "2027-04-05"},
    {
        "id": "da-full",
        "course_id": DA,
        "start_date": "2027-05-01",
        "end_date": "2027-05-02",
        "status": "full",
    },
]


def _catalogue(intakes=SYNTHETIC_INTAKES):
    catalogue = CourseCatalogue()
    catalogue.bind_intake_provider(lambda: copy.deepcopy(intakes))
    return catalogue


@pytest.fixture
def kb():
    return KnowledgeBase(_catalogue())


def _raw():
    return json.loads(KNOWLEDGE_BASE_PATH.read_text(encoding="utf-8"))


def _write(tmp_path, data):
    path = tmp_path / "kb.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_every_entry_cites_a_source_and_ids_are_unique(kb):
    ids = [entry.id for entry in kb.entries]
    assert len(ids) == len(set(ids))
    assert all(entry.source and entry.topic and entry.approved_text for entry in kb.entries)


def test_every_active_course_and_the_general_section_have_content(kb):
    courses = {entry.course_id for entry in kb.entries}
    assert {GENERAL, DA, FD} <= courses


def test_template_faq_topics_are_covered(kb):
    expected = {
        "da.introduction",
        "da.payment_modes",
        "da.mid_career",
        "da.fee_concern",
        "da.job_opportunities",
        "da.minimum_qualification",
        "da.utap",
        "da.enrollment_requirements",
        "da.skillsfuture_balance",
    }
    assert expected <= kb.entry_ids()


def test_render_resolves_every_placeholder(kb):
    rendered = kb.render()
    assert "{" not in rendered and "}" not in rendered
    assert "S$600 nett" in rendered
    assert "201841969G" in rendered
    assert "https://sfc.myskillsfuture.gov.sg/claim" in rendered


def test_general_fees_and_funding_reply_distinguishes_skillsfuture_and_utap(kb):
    entry = kb.entry("general.fees_and_funding")
    assert entry is not None
    assert "use basic-tier SkillsFuture Credit toward the fee" in entry.approved_text
    assert "UTAP is a reimbursement, not an upfront discount" in entry.approved_text
    assert "cannot be combined with SkillsFuture Credit" in entry.approved_text
    assert "remaining annual entitlement and claim approval" in entry.approved_text
    rendered = kb.render()
    assert "{da_course_fee}" not in rendered
    assert "{fd_discount_terms}" not in rendered


def test_knowledge_base_reloads_edited_file_without_restarting(tmp_path):
    data = _raw()
    entry = next(
        entry for entry in data["entries"]
        if entry["id"] == "general.fees_and_funding"
    )
    updated_phrase = "Updated funding wording is active."
    entry["approved_text"] = updated_phrase
    path = tmp_path / "enquiry_knowledge_base.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    kb = KnowledgeBase(_catalogue(), path=path)

    entry["approved_text"] = updated_phrase + " Updated again."
    path.write_text(json.dumps(data), encoding="utf-8")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    assert updated_phrase + " Updated again." in kb.render()


def test_live_intakes_replace_template_dates(kb):
    rendered = kb.render(DA)
    assert "10–11 March 2027" in rendered
    assert "20–21 March 2027" in rendered
    assert "July 2026" not in rendered
    assert "August 2026" not in rendered
    # A full intake is not offered.
    assert "May 2027" not in rendered


def test_no_intakes_uses_the_catalogue_message():
    catalogue = _catalogue(intakes=[])
    rendered = KnowledgeBase(catalogue).render(DA)
    assert catalogue.no_intakes_message in rendered


def test_unconfirmed_topics_are_marked_and_conflicting_venues_never_rendered(kb):
    rendered = kb.render()
    assert "[da.venue] Training venue - NOT CONFIRMED" in rendered
    assert "Clementi" not in rendered
    assert "City Square" not in rendered


def test_maintainer_notes_are_never_rendered(kb):
    rendered = kb.render()
    for entry in kb.entries:
        if entry.notes:
            assert entry.notes not in rendered


def test_course_render_includes_general_but_not_other_courses(kb):
    rendered = kb.render(FD)
    assert "S$450 nett" in rendered
    assert "5 April 2027" in rendered
    assert "[general.courses_offered]" in rendered
    assert "[da.introduction]" not in rendered
    # The prototype-drafted Front Desk overview is not approved content (D4).
    assert "welcoming and registering patients" not in rendered


def test_fact_allow_list_includes_approved_and_live_facts(kb):
    facts = kb.fact_allow_list()
    assert {Decimal("600"), Decimal("450"), Decimal("250"), Decimal("500")} <= facts.amounts
    assert Decimal("50") in facts.percents
    assert {(3, 10), (3, 11), (3, 20), (3, 21), (4, 5)} <= facts.dates
    assert {570, 1050} <= facts.times
    assert {
        "sfc.myskillsfuture.gov.sg/claim",
        "ntuc.co/chat",
        "ntuc.org.sg",
    } <= facts.urls


def test_fact_allow_list_excludes_unapproved_values(kb):
    facts = kb.fact_allow_list()
    # Participant-specific arithmetic is added per turn, not approved globally.
    assert Decimal("100") not in facts.amounts
    # Historical template dates and full intakes are not allowed.
    assert (7, 15) not in facts.dates
    assert (5, 1) not in facts.dates


def test_intake_listing_course_identifies_one_course_date_listing(kb):
    assert kb.intake_listing_course(["da.intake_dates"]) == DA
    assert kb.intake_listing_course(["fd.catalogue_facts", "general.language"]) == FD
    # Both courses' dates, or no date listing at all, are not one course's dates.
    assert kb.intake_listing_course(["da.intake_dates", "fd.catalogue_facts"]) is None
    assert kb.intake_listing_course(["da.introduction"]) is None
    assert kb.intake_listing_course(["unknown.entry"]) is None


def test_followup_templates_offer_stop_and_carry_no_facts(kb):
    templates = [kb.followup_template(step) for step in range(3)]
    assert len(set(templates)) == 3
    # Later steps reuse the final reminder.
    assert kb.followup_template(9) == templates[2]
    for text in templates:
        assert "STOP" in text
        assert "Reply 4" not in text and "Reply 7" not in text
        assert extract_facts(text).is_empty()


def test_fixed_replies_and_quick_replies(kb):
    assert kb.fixed_reply("unanswered", subject="parking at the venue").startswith(
        "I don't have confirmed information about parking at the venue."
    )
    assert "1767" in kb.fixed_reply("crisis_support")
    assert "Talk to staff" in kb.quick_replies
    with pytest.raises(KnowledgeBaseError):
        kb.fixed_reply("unanswered")
    with pytest.raises(KnowledgeBaseError):
        kb.fixed_reply("does_not_exist")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda data: data["entries"].append(dict(data["entries"][0])), "Duplicate entry id"),
        (lambda data: data["entries"][0].update(course_id="unknown-course"), "unknown or retired"),
        (lambda data: data["entries"][0].update(course_id="dental-infection-control"), "unknown or retired"),
        (lambda data: data["entries"][3].update(approved_text="Fee {discount}"), "unsupported placeholders"),
        (lambda data: data["entries"][0].update(approved_text="Fee {course_fee}"), "unsupported placeholders"),
        (lambda data: data["entries"][3].update(source=""), "non-empty source"),
        (lambda data: data["fixed_replies"].pop("crisis_support"), "Missing fixed replies"),
        (lambda data: data["fixed_replies"].update(fallback="Sorry {name}"), "unexpected placeholders"),
        (lambda data: data["suggestions"].append({"id": "x", "label": "x" * 41, "covers": []}), "1-40 characters"),
        (lambda data: data["suggestions"].append(dict(data["suggestions"][0])), "unique"),
        (lambda data: data["suggestions"][0].update(covers=["da.discounts"]), "unknown entry"),
        (lambda data: data["starter_suggestions"].append("pricing"), "unknown suggestions"),
        (lambda data: data.update(suggestion_order=[]), "needs a suggestion_order"),
        (lambda data: data.update(entries=[]), "non-empty entries"),
        (lambda data: data.update(followup_templates=[]), "needs followup_templates"),
        (lambda data: data["followup_templates"].__setitem__(0, "Hi! Any questions?"), "reply STOP"),
        (lambda data: data["followup_templates"].__setitem__(1, "Fee {course_fee}. Reply STOP."), "placeholders"),
        (lambda data: data["entries"][4].update(lists_intakes=True), "no course intake list"),
        (lambda data: data["entries"][0].update(lists_intakes=True), "no course intake list"),
    ],
)
def test_invalid_content_is_rejected_on_load(tmp_path, mutate, message):
    data = _raw()
    mutate(data)
    with pytest.raises(KnowledgeBaseError, match=message):
        KnowledgeBase(_catalogue(), path=_write(tmp_path, data))


def test_missing_catalogue_value_renders_as_to_be_confirmed(kb):
    # The Front Desk course has no confirmed training time in the catalogue.
    rendered = kb.resolve(kb.entry("fd.catalogue_facts"))
    assert f"Training hours: {MISSING_VALUE}" in rendered


def test_missing_fee_does_not_stop_the_knowledge_base_loading(tmp_path):
    data = json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))
    data["course"]["fee"]["amount"] = None
    data["course"]["fee"]["display"] = ""
    path = tmp_path / "catalogue.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    catalogue = CourseCatalogue(path)
    catalogue.bind_intake_provider(lambda: [])
    kb = KnowledgeBase(catalogue)
    assert f"Course fee: {MISSING_VALUE}" in kb.render(DA)
    assert Decimal("600") not in kb.fact_allow_list().amounts


def test_unreadable_file_is_rejected(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(KnowledgeBaseError, match="Cannot load"):
        KnowledgeBase(_catalogue(), path=path)
