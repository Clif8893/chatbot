"""Deterministic check that an enquiry reply states no unapproved Q&M facts.

Model instructions alone cannot guarantee that a price, date or link is never
invented, so every reply is checked against the approved allow-list plus the
values the participant supplied before it is sent.
"""

import re
from dataclasses import dataclass, field
from itertools import combinations

from backend.services.enquiry.facts import FactSet, extract_facts


MAX_REPLY_CHARS = 1500
# Reply types that must be grounded in cited approved content.
GROUNDED_TYPES = {"grounded_answer"}
# General-knowledge replies are not about Q&M, so they may carry no prices or links.
GENERAL_TYPES = {"general_knowledge"}

INTERNAL_TERMS = re.compile(
    r"\b(?:system\s+prompt|prompts?|language\s+model|LLM|GPT|OpenAI|Anthropic|"
    r"CrewAI|routing|router|classifier|database|knowledge\s+base|kb_entry_ids|"
    r"(?:da|fd|general|staff)\.[a-z0-9_-]+|tool\s+calls?|JSON)\b",
    re.I,
)


@dataclass(frozen=True)
class GuardContext:
    allowed: FactSet
    kb_ids: frozenset
    participant: FactSet = field(default_factory=FactSet)
    course_fees: tuple = ()

    def derived_amounts(self):
        """Arithmetic the assistant may state: fee minus a stated amount, and fee totals."""
        amounts = set()
        for fee in self.course_fees:
            for stated in self.participant.amounts:
                if 0 < stated < fee:
                    amounts.add(fee - stated)
        for first, second in combinations(self.course_fees, 2):
            amounts.add(first + second)
        return amounts


def check(turn, context):
    """Return human-readable violations; an empty list means the reply may be sent."""
    violations = []
    reply = turn.reply or ""

    if len(reply) > MAX_REPLY_CHARS:
        violations.append(f"The reply is longer than {MAX_REPLY_CHARS} characters.")

    # A mistyped citation is bookkeeping, not an unsafe fact: unknown ids are
    # ignored (the facts themselves are checked below), but a grounded answer
    # must still cite at least one real entry.
    if turn.response_type in GROUNDED_TYPES and not (set(turn.kb_entry_ids) & context.kb_ids):
        violations.append("A grounded answer must cite at least one approved-content id.")

    match = INTERNAL_TERMS.search(reply)
    if match:
        violations.append(f"The reply mentions an internal detail: '{match.group(0)}'.")

    facts = extract_facts(reply)
    allowed = context.allowed.union(context.participant)
    general = turn.response_type in GENERAL_TYPES

    if general and facts.amounts:
        violations.append("A general-knowledge reply must not state any price or amount.")
    elif not general:
        permitted = allowed.amounts | context.derived_amounts()
        for amount in sorted(facts.amounts - permitted):
            violations.append(f"Unapproved amount: ${_plain(amount)}.")

    for percent in sorted(facts.percents - allowed.percents):
        violations.append(f"Unapproved percentage: {_plain(percent)}%.")
    for month, day in sorted(facts.dates - allowed.dates):
        violations.append(f"Unapproved date: {day}/{month}.")
    for minutes in sorted(facts.times - allowed.times):
        violations.append(f"Unapproved time: {minutes // 60:02d}:{minutes % 60:02d}.")

    for url in sorted(facts.urls):
        if general:
            violations.append(f"A general-knowledge reply must not include a link: {url}.")
        elif not _url_allowed(url, allowed.urls):
            violations.append(f"Unapproved link: {url}.")

    return violations


def _url_allowed(url, allowed_urls):
    if url in allowed_urls:
        return True
    if "/" in url:
        return False
    # A bare domain is fine when it is the host, or a parent domain, of an approved link.
    for approved in allowed_urls:
        host = approved.split("/", 1)[0]
        if host == url or host.endswith("." + url):
            return True
    return False


def _plain(value):
    return format(value.normalize(), "f")


__all__ = ["GuardContext", "MAX_REPLY_CHARS", "check"]
