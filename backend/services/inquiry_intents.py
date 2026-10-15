"""Deterministic read-only inquiry categories shared by chat routing."""

import re


_CAREER_TERMS = re.compile(
    r"\b(?:job|jobs|employment|employed|career|careers|position|positions|"
    r"hire|hired|hiring|placement|placements)\b|"
    r"\bwork\s+after\b|\bjob\s+guarantee(?:d)?\b|"
    r"\bguarantee(?:d)?\s+(?:a\s+)?(?:job|employment|position)\b",
    re.I,
)


def is_career_question(message):
    """Return whether a message asks about work or placement after training."""

    text = " ".join(str(message or "").split())
    if re.search(r"\b(?:skillsfuture|skillfuture|utap|funding|subsidy|grant)\b", text, re.I):
        return False
    return bool(text and _CAREER_TERMS.search(text))


__all__ = ["is_career_question"]
