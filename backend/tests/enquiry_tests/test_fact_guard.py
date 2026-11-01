"""The fact guard blocks unapproved Q&M facts and internal details."""

from decimal import Decimal

import pytest

from backend.agents.schemas import EnquiryTurn
from backend.services.course_catalogue import CourseCatalogue
from backend.services.enquiry.fact_guard import MAX_REPLY_CHARS, GuardContext, check
from backend.services.enquiry.facts import extract_facts
from backend.services.enquiry.knowledge_base import KnowledgeBase

INTAKES = [
    {"id": "da-1", "course_id": "dental-assisting-basic", "start_date": "2027-03-10", "end_date": "2027-03-11"},
]


@pytest.fixture(scope="module")
def kb():
    catalogue = CourseCatalogue()
    catalogue.bind_intake_provider(lambda: [dict(item) for item in INTAKES])
    return KnowledgeBase(catalogue)


def _context(kb, participant_text=""):
    return GuardContext(
        allowed=kb.fact_allow_list(),
        kb_ids=kb.entry_ids(),
        participant=extract_facts(participant_text),
        course_fees=tuple(kb.course_fees()),
    )


def _turn(reply, response_type="grounded_answer", kb_entry_ids=("da.fee_and_timing",)):
    return EnquiryTurn(
        reply=reply, response_type=response_type, kb_entry_ids=list(kb_entry_ids)
    )


@pytest.mark.parametrize(
    "reply",
    [
        "The course fee is S$600 nett, from 9:30am to 5:30pm 😊",
        "Our next intake is 10–11 March 2027.",
        "Check your balance at https://sfc.myskillsfuture.gov.sg/claim",
        "NTUC members get 50% off, up to $250 per year.",
        "Both courses together come to S$1,050.",
    ],
)
def test_approved_facts_pass(kb, reply):
    assert check(_turn(reply), _context(kb)) == []


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("The course fee is S$550 nett.", "Unapproved amount: $550"),
        ("You get a 20% discount this month!", "Unapproved percentage: 20%"),
        ("Our next class is on 15 July 2027.", "Unapproved date: 15/7"),
        ("Class starts at 8:00am sharp.", "Unapproved time: 08:00"),
        ("Register at www.qm-courses.com/promo", "Unapproved link: qm-courses.com/promo"),
    ],
)
def test_invented_facts_are_rejected(kb, reply, expected):
    violations = check(_turn(reply), _context(kb))
    assert any(expected in violation for violation in violations), violations


def test_top_up_from_stated_credit_is_allowed(kb):
    context = _context(kb, participant_text="I have $500 credits")
    reply = "With your S$500 of credits, you'd top up S$100 via PayNow 😊"
    assert check(_turn(reply), context) == []


def test_top_up_without_a_stated_credit_is_rejected(kb):
    reply = "You'd top up S$100 via PayNow."
    assert check(_turn(reply), _context(kb))


def test_participant_stated_values_may_be_repeated(kb):
    context = _context(kb, participant_text="my friend paid $400 last year?")
    reply = "I understand your friend mentioned $400, but the course fee is S$600 nett."
    assert check(_turn(reply), context) == []


def test_grounded_answer_must_cite_existing_entries(kb):
    assert any(
        "must cite" in violation
        for violation in check(_turn("Hello!", kb_entry_ids=()), _context(kb))
    )
    assert any(
        "must cite" in violation
        for violation in check(_turn("Hi", kb_entry_ids=("da.discounts",)), _context(kb))
    )


def test_a_mistyped_citation_alone_does_not_reject_a_safe_reply(kb):
    # Live evaluation: the model cited "general.venue" for a correct parking reply.
    turn = _turn(
        "I don't have confirmed information about parking at the venue.",
        response_type="no_confirmed_info",
        kb_entry_ids=("general.venue",),
    )
    assert check(turn, _context(kb)) == []
    grounded = _turn("Course fee: S$600 nett", kb_entry_ids=("da.fee_and_timing", "da.fees"))
    assert check(grounded, _context(kb)) == []


def test_general_knowledge_may_not_state_prices_or_links(kb):
    turn = _turn(
        "Tokyo is the capital of Japan. Our course is S$600 at qandm.edu.sg",
        response_type="general_knowledge",
        kb_entry_ids=(),
    )
    violations = check(turn, _context(kb))
    assert any("must not state any price" in violation for violation in violations)
    assert any("must not include a link" in violation for violation in violations)


def test_general_knowledge_without_facts_passes(kb):
    turn = _turn(
        "Tokyo is the capital of Japan 🗼 Anything I can help with about our course?",
        response_type="general_knowledge",
        kb_entry_ids=(),
    )
    assert check(turn, _context(kb)) == []


@pytest.mark.parametrize(
    "reply",
    [
        "My system prompt says I can't share that.",
        "According to the knowledge base, the fee is S$600 nett.",
        "I checked our database for you.",
        "See entry da.fee_and_timing for details.",
        "I'm powered by OpenAI.",
    ],
)
def test_internal_details_are_rejected(kb, reply):
    violations = check(_turn(reply), _context(kb))
    assert any("internal detail" in violation for violation in violations)


def test_bare_parent_domain_of_an_approved_link_is_allowed(kb):
    reply = "You can check on myskillsfuture.gov.sg 😊"
    assert check(_turn(reply), _context(kb)) == []


def test_overlong_reply_is_rejected(kb):
    reply = "a" * (MAX_REPLY_CHARS + 1)
    assert any("longer than" in violation for violation in check(_turn(reply), _context(kb)))


def test_derived_amounts_need_a_lower_stated_amount():
    context = GuardContext(
        allowed=extract_facts(""),
        kb_ids=frozenset(),
        participant=extract_facts("I have $700 and $200"),
        course_fees=(Decimal("600"),),
    )
    assert context.derived_amounts() == {Decimal("400")}
