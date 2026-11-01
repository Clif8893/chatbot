"""Extract checkable facts (money, percentages, dates, times, web addresses) from text.

The knowledge base builds an allow-list from approved content with these
extractors, and the fact guard extracts the same kinds of facts from a reply, so
both sides normalise values identically.
"""

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation


_MONTHS = {
    name: index
    for index, names in enumerate(
        (
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ),
        start=1,
    )
    for name in names
}
_MONTH = r"(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?"
_DAY = r"(\d{1,2})(?:st|nd|rd|th)?"

MONEY_PATTERN = re.compile(
    r"(?:S\$|SGD\s*|\$)\s*(\d[\d,]*(?:\.\d{1,2})?)|\b(\d[\d,]*(?:\.\d{1,2})?)\s*(?:dollars|SGD)\b",
    re.I,
)
PERCENT_PATTERN = re.compile(r"\b(\d{1,3}(?:\.\d+)?)\s*(?:%|per\s*cent|percent)", re.I)
ISO_DATE_PATTERN = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
DAY_RANGE_PATTERN = re.compile(
    rf"\b{_DAY}\s*(?:-|–|—|to|&|and)\s*{_DAY}\s+(?:of\s+)?{_MONTH}(?![a-z])", re.I
)
DAY_MONTH_PATTERN = re.compile(rf"\b{_DAY}\s+(?:of\s+)?{_MONTH}(?![a-z])", re.I)
MONTH_DAY_PATTERN = re.compile(rf"\b{_MONTH}\s+{_DAY}(?![\d:])\b", re.I)
TIME_12H_PATTERN = re.compile(r"\b(\d{1,2})(?:[:.]?(\d{2}))?\s*([ap])\.?m\b\.?", re.I)
TIME_24H_PATTERN = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b(?!\s*[ap]\.?m)", re.I)
URL_PATTERN = re.compile(
    r"\b(?:https?://)?(?:www\.)?"
    r"((?:[a-z0-9-]+\.)+(?:com|sg|org|net|edu|gov|co|io|info|asia))(?![a-z0-9-])"
    r"(/[^\s)>\]]*)?",
    re.I,
)


@dataclass(frozen=True)
class FactSet:
    """Normalised facts: amounts as Decimal, dates as (month, day), times as minutes."""

    amounts: frozenset = frozenset()
    percents: frozenset = frozenset()
    dates: frozenset = frozenset()
    times: frozenset = frozenset()
    urls: frozenset = frozenset()

    def union(self, other):
        return FactSet(
            amounts=self.amounts | other.amounts,
            percents=self.percents | other.percents,
            dates=self.dates | other.dates,
            times=self.times | other.times,
            urls=self.urls | other.urls,
        )

    def is_empty(self):
        return not (self.amounts or self.percents or self.dates or self.times or self.urls)


def extract_facts(text):
    text = str(text or "")
    return FactSet(
        amounts=frozenset(_amounts(text)),
        percents=frozenset(_decimal(match.group(1)) for match in PERCENT_PATTERN.finditer(text)),
        dates=frozenset(_dates(text)),
        times=frozenset(_times(text)),
        urls=frozenset(normalise_url(match.group(0)) for match in URL_PATTERN.finditer(text)),
    )


def date_range_facts(start, end):
    """(month, day) facts for every day of an intake, inclusive."""
    start = date.fromisoformat(str(start))
    end = date.fromisoformat(str(end or start))
    days = []
    current = start
    while current <= end:
        days.append((current.month, current.day))
        current += timedelta(days=1)
    return FactSet(dates=frozenset(days))


def amount_fact(value):
    return FactSet(amounts=frozenset({_decimal(value)}))


def normalise_url(value):
    value = str(value).strip().casefold().rstrip(".,;:!?'\"")
    value = re.sub(r"^https?://", "", value)
    value = re.sub(r"^www\.", "", value)
    return value.rstrip("/")


def _amounts(text):
    for match in MONEY_PATTERN.finditer(text):
        raw = match.group(1) or match.group(2)
        amount = _decimal(raw)
        if amount is not None:
            yield amount


def _dates(text):
    for match in ISO_DATE_PATTERN.finditer(text):
        month, day = int(match.group(2)), int(match.group(3))
        if _valid(month, day):
            yield month, day
    for match in DAY_RANGE_PATTERN.finditer(text):
        month = _MONTHS[match.group(3).casefold()]
        for day in (int(match.group(1)), int(match.group(2))):
            if _valid(month, day):
                yield month, day
    for match in DAY_MONTH_PATTERN.finditer(text):
        month, day = _MONTHS[match.group(2).casefold()], int(match.group(1))
        if _valid(month, day):
            yield month, day
    for match in MONTH_DAY_PATTERN.finditer(text):
        month, day = _MONTHS[match.group(1).casefold()], int(match.group(2))
        if _valid(month, day):
            yield month, day


def _times(text):
    for match in TIME_12H_PATTERN.finditer(text):
        hour, minute = int(match.group(1)), int(match.group(2) or 0)
        if not (1 <= hour <= 12 and minute < 60):
            continue
        hour = hour % 12 + (12 if match.group(3).casefold() == "p" else 0)
        yield hour * 60 + minute
    for match in TIME_24H_PATTERN.finditer(text):
        yield int(match.group(1)) * 60 + int(match.group(2))


def _decimal(raw):
    try:
        return Decimal(str(raw).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _valid(month, day):
    return 1 <= month <= 12 and 1 <= day <= 31


__all__ = [
    "FactSet",
    "amount_fact",
    "date_range_facts",
    "extract_facts",
    "normalise_url",
]
