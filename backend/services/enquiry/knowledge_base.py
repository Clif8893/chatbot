"""Approved enquiry content for Module A, resolved against the live course catalogue.

Staff-editable wording lives in ``backend/data/enquiry_knowledge_base.json``.
Fees, durations, intake dates and payment identifiers are placeholders filled
from the catalogue on every call, so approved wording never carries stale facts.
"""

import json
import string
from threading import Lock
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from backend.services.enquiry.facts import (
    FactSet,
    amount_fact,
    date_range_facts,
    extract_facts,
)


KNOWLEDGE_BASE_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "enquiry_knowledge_base.json"
)
GENERAL = "general"
# Rendered for a live catalogue value staff have not filled in yet; never guessed.
MISSING_VALUE = "to be confirmed by our team"

# Placeholder -> path into a catalogue course record.
COURSE_PLACEHOLDERS = {
    "course_name": ("name",),
    "course_fee": ("fee", "display"),
    "discount_terms": ("fee", "discount_terms"),
    "duration": ("duration",),
    "training_time": ("training_time",),
    "website": ("website",),
    "paynow_uen": ("paynow", "uen"),
    "claim_page": ("skillsfuture", "claim_page"),
}
LIVE_COURSE_PLACEHOLDERS = {"intake_list"}
GENERAL_COURSE_FEES = {
    "da_course_fee": "dental-assisting-basic",
    "fd_course_fee": "dental-clinic-front-desk-operations",
}
GENERAL_PLACEHOLDERS = {"course_list", "fd_discount_terms", *GENERAL_COURSE_FEES}
# Catalogue facts every course has, so an answer about them differs by course.
PER_COURSE_FACTS = ("course_fee", "duration", "training_time", "intake_list")
FIXED_REPLY_PLACEHOLDERS = {"unanswered": {"subject"}}
REQUIRED_FIXED_REPLIES = {
    "first_contact_privacy",
    "language_note",
    "unanswered",
    "human_request",
    "crisis_support",
    "decline_instructions",
    "abuse_close",
    "opt_out",
    "forget_me",
    "fallback",
}
MAX_QUICK_REPLY_LENGTH = 40


class KnowledgeBaseError(ValueError):
    """The approved-content file is missing, malformed or inconsistent."""


@dataclass(frozen=True)
class KnowledgeEntry:
    id: str
    course_id: str
    topic: str
    approved_text: str
    source: str
    confirmation_required: bool = False
    notes: str = ""
    # A reply citing this entry shows the course's selectable intake dates.
    lists_intakes: bool = False


@dataclass(frozen=True)
class Suggestion:
    """An approved button: its label is sent as the participant's message."""

    id: str
    label: str
    # Entries this button leads to; once any is answered the button is spent.
    covers: tuple = ()


class KnowledgeBase:
    def __init__(self, catalogue, path=KNOWLEDGE_BASE_PATH):
        self.catalogue = catalogue
        self.path = Path(path)
        self._source_mtime_ns = None
        self._reload_lock = Lock()
        self._reload_if_changed()

    def _reload_if_changed(self):
        try:
            modified_ns = self.path.stat().st_mtime_ns
        except (OSError, json.JSONDecodeError) as error:
            raise KnowledgeBaseError(f"Cannot load knowledge base: {error}") from error
        if modified_ns == self._source_mtime_ns:
            return
        with self._reload_lock:
            try:
                modified_ns = self.path.stat().st_mtime_ns
                if modified_ns == self._source_mtime_ns:
                    return
                with self.path.open(encoding="utf-8") as kb_file:
                    data = json.load(kb_file)
            except (OSError, json.JSONDecodeError) as error:
                raise KnowledgeBaseError(f"Cannot load knowledge base: {error}") from error
            self.version = str(data.get("version", ""))
            self._entries = tuple(self._load_entries(data.get("entries")))
            self._by_id = {entry.id: entry for entry in self._entries}
            self._fixed_replies = self._load_fixed_replies(data.get("fixed_replies"))
            self._suggestions = self._load_suggestions(data.get("suggestions"))
            self._starter_ids = self._load_suggestion_ids(
                data.get("starter_suggestions"), "starter_suggestions"
            )
            self._suggestion_order = self._load_suggestion_ids(
                data.get("suggestion_order"), "suggestion_order"
            )
            self._followup_templates = tuple(
                self._load_followup_templates(data.get("followup_templates"))
            )
            self._source_mtime_ns = modified_ns

    @property
    def entries(self):
        self._reload_if_changed()
        from flask import has_app_context
        if not has_app_context():
            return self._entries
        from backend.extensions import db
        from backend.models.operations import CatalogueContent
        row = db.session.get(CatalogueContent, "approved_kb")
        approved = list(self._load_entries(row.value)) if row and row.value else []
        return (*self._entries, *approved)

    @property
    def quick_replies(self):
        """Labels of the default starter menu."""
        self._reload_if_changed()
        return [self._suggestions[key].label for key in self._starter_ids]

    @property
    def suggestions(self):
        self._reload_if_changed()
        return dict(self._suggestions)

    @property
    def starter_suggestion_ids(self):
        self._reload_if_changed()
        return list(self._starter_ids)

    @property
    def suggestion_order(self):
        self._reload_if_changed()
        return list(self._suggestion_order)

    def render_suggestions(self):
        """Prompt block listing the only buttons the assistant may offer."""
        self._reload_if_changed()
        return "\n".join(
            f"- {item.id}: \"{item.label}\""
            for item in self._suggestions.values()
        )

    def followup_template(self, step):
        """Approved reminder for a zero-based step; later steps reuse the last one."""
        self._reload_if_changed()
        return self._followup_templates[min(step, len(self._followup_templates) - 1)]

    def entry(self, entry_id):
        return next((entry for entry in self.entries if entry.id == entry_id), None)

    def entry_ids(self):
        return frozenset(entry.id for entry in self.entries)

    def entries_for(self, course_id=None):
        """General entries plus one course's entries, or every entry when None."""
        return [
            entry
            for entry in self.entries
            if course_id is None or entry.course_id in {GENERAL, course_id}
        ]

    def resolve(self, entry):
        """Approved text with live catalogue values substituted."""
        self._reload_if_changed()
        return entry.approved_text.format_map(self._values_for(entry.course_id))

    def fixed_reply(self, name, **values):
        self._reload_if_changed()
        if name not in self._fixed_replies:
            raise KnowledgeBaseError(f"Unknown fixed reply: {name}")
        expected = FIXED_REPLY_PLACEHOLDERS.get(name, set())
        if set(values) != expected:
            raise KnowledgeBaseError(
                f"Fixed reply {name} needs values {sorted(expected)}."
            )
        return self._fixed_replies[name].format_map(values)

    def render(self, course_id=None):
        """Prompt block of approved content; maintainer notes are never included."""
        self._reload_if_changed()
        sections = []
        for section_id, heading in self._section_headings(course_id):
            blocks = []
            for entry in self.entries:
                if entry.course_id != section_id:
                    continue
                title = f"[{entry.id}] {entry.topic}"
                if entry.confirmation_required:
                    title += (
                        " - NOT CONFIRMED for this topic only: if asked about it, "
                        "reply with the text below and state no other detail on it"
                    )
                blocks.append(f"{title}\n{self.resolve(entry)}")
            if blocks:
                sections.append(f"## {heading}\n\n" + "\n\n".join(blocks))
        return "\n\n".join(sections)

    def intake_listing_course(self, entry_ids):
        """The one course whose dates a reply citing ``entry_ids`` listed, else None."""
        courses = {
            entry.course_id
            for entry_id in entry_ids
            if (entry := self.entry(entry_id)) is not None and entry.lists_intakes
        }
        return courses.pop() if len(courses) == 1 else None

    def courses_cited(self, entry_ids):
        """Course ids of the course-specific entries in ``entry_ids`` (general ones excluded)."""
        return {
            entry.course_id
            for entry_id in entry_ids
            if (entry := self.entry(entry_id)) is not None and entry.course_id != GENERAL
        }

    def active_course_count(self):
        return len(self._active_courses())

    def cites_per_course_facts(self, entry_ids):
        """Whether a cited course entry states fee, duration, timing or dates.

        Every course has these catalogue facts, so they differ by course; topics
        written for one course only (such as UTAP) do not.
        """
        return any(
            (entry := self.entry(entry_id)) is not None
            and entry.course_id != GENERAL
            and any(f"{{{name}}}" in entry.approved_text for name in PER_COURSE_FACTS)
            for entry_id in entry_ids
        )

    def course_fees(self):
        """Live fees of active courses, for participant-specific arithmetic."""
        fees = []
        for course in self._active_courses():
            amount = course.get("fee", {}).get("amount")
            if amount not in (None, ""):
                fees.append(Decimal(str(amount)))
        return fees

    def fact_allow_list(self):
        """Every fact a reply may state about Q&M, from approved and live content."""
        facts = FactSet()
        for entry in self.entries:
            facts = facts.union(extract_facts(self.resolve(entry)))
        for name, text in self._fixed_replies.items():
            placeholders = {
                key: "" for key in FIXED_REPLY_PLACEHOLDERS.get(name, set())
            }
            facts = facts.union(extract_facts(text.format_map(placeholders)))
        for text in self._followup_templates:
            facts = facts.union(extract_facts(text))
        for course in self._active_courses():
            amount = course.get("fee", {}).get("amount")
            if amount not in (None, ""):
                facts = facts.union(amount_fact(amount))
            for intake in self.catalogue.selectable_intakes_for(course["id"]):
                facts = facts.union(
                    date_range_facts(intake["start_date"], intake["end_date"])
                )
                facts = facts.union(
                    extract_facts(
                        f"{intake.get('start_time') or ''} {intake.get('end_time') or ''}"
                    )
                )
        return facts

    # Loading and validation -------------------------------------------------

    def _load_entries(self, raw_entries):
        if not isinstance(raw_entries, list) or not raw_entries:
            raise KnowledgeBaseError("The knowledge base needs a non-empty entries list.")
        course_ids = {course["id"] for course in self._active_courses()}
        seen = set()
        for raw in raw_entries:
            if not isinstance(raw, dict):
                raise KnowledgeBaseError("Every knowledge-base entry must be an object.")
            for field in ("id", "course_id", "topic", "approved_text", "source"):
                if not isinstance(raw.get(field), str) or not raw[field].strip():
                    raise KnowledgeBaseError(
                        f"Entry {raw.get('id', '?')} needs a non-empty {field}."
                    )
            entry = KnowledgeEntry(
                id=raw["id"].strip(),
                course_id=raw["course_id"].strip(),
                topic=raw["topic"].strip(),
                approved_text=raw["approved_text"].strip(),
                source=raw["source"].strip(),
                confirmation_required=bool(raw.get("confirmation_required", False)),
                notes=str(raw.get("notes", "")),
                lists_intakes=bool(raw.get("lists_intakes", False)),
            )
            if entry.lists_intakes and (
                entry.course_id == GENERAL or "{intake_list}" not in entry.approved_text
            ):
                raise KnowledgeBaseError(
                    f"Entry {entry.id} sets lists_intakes but shows no course intake list."
                )
            if entry.id in seen:
                raise KnowledgeBaseError(f"Duplicate entry id: {entry.id}")
            seen.add(entry.id)
            if entry.course_id != GENERAL and entry.course_id not in course_ids:
                raise KnowledgeBaseError(
                    f"Entry {entry.id} names unknown or retired course {entry.course_id}."
                )
            self._check_placeholders(entry)
            yield entry

    def _check_placeholders(self, entry):
        names = _placeholders(entry.approved_text, entry.id)
        if entry.course_id == GENERAL:
            allowed = GENERAL_PLACEHOLDERS
        else:
            allowed = set(COURSE_PLACEHOLDERS) | LIVE_COURSE_PLACEHOLDERS
        unknown = names - allowed
        if unknown:
            raise KnowledgeBaseError(
                f"Entry {entry.id} uses unsupported placeholders: {sorted(unknown)}"
            )

    def _load_fixed_replies(self, raw):
        if not isinstance(raw, dict):
            raise KnowledgeBaseError("The knowledge base needs a fixed_replies object.")
        missing = REQUIRED_FIXED_REPLIES - set(raw)
        if missing:
            raise KnowledgeBaseError(f"Missing fixed replies: {sorted(missing)}")
        replies = {}
        for name, text in raw.items():
            if not isinstance(text, str) or not text.strip():
                raise KnowledgeBaseError(f"Fixed reply {name} must be non-empty text.")
            names = _placeholders(text, name)
            if names != FIXED_REPLY_PLACEHOLDERS.get(name, set()):
                raise KnowledgeBaseError(
                    f"Fixed reply {name} has unexpected placeholders: {sorted(names)}"
                )
            replies[name] = text.strip()
        return replies

    @staticmethod
    def _load_followup_templates(raw):
        if not isinstance(raw, list) or not raw:
            raise KnowledgeBaseError("The knowledge base needs followup_templates.")
        for index, text in enumerate(raw, start=1):
            if not isinstance(text, str) or not text.strip():
                raise KnowledgeBaseError(f"Follow-up template {index} must be non-empty text.")
            if "STOP" not in text:
                raise KnowledgeBaseError(
                    f"Follow-up template {index} must tell the participant how to reply STOP."
                )
            if _placeholders(text, f"followup template {index}"):
                raise KnowledgeBaseError(f"Follow-up template {index} must not use placeholders.")
            yield text.strip()

    def _load_suggestions(self, raw):
        if not isinstance(raw, list) or not raw:
            raise KnowledgeBaseError("The knowledge base needs a suggestions list.")
        suggestions = {}
        for item in raw:
            if not isinstance(item, dict):
                raise KnowledgeBaseError("Every suggestion must be an object.")
            key = str(item.get("id") or "").strip()
            label = str(item.get("label") or "").strip()
            covers = item.get("covers", [])
            if not key or key in suggestions:
                raise KnowledgeBaseError(f"Suggestion ids must be unique and non-empty: {key!r}")
            if not label or len(label) > MAX_QUICK_REPLY_LENGTH:
                raise KnowledgeBaseError(
                    f"Suggestion {key} label must be 1-{MAX_QUICK_REPLY_LENGTH} characters."
                )
            if not isinstance(covers, list) or any(entry not in self._by_id for entry in covers):
                raise KnowledgeBaseError(f"Suggestion {key} covers an unknown entry.")
            suggestions[key] = Suggestion(id=key, label=label, covers=tuple(covers))
        return suggestions

    def _load_suggestion_ids(self, raw, name):
        if not isinstance(raw, list) or not raw:
            raise KnowledgeBaseError(f"The knowledge base needs a {name} list.")
        unknown = [key for key in raw if key not in self._suggestions]
        if unknown:
            raise KnowledgeBaseError(f"{name} names unknown suggestions: {unknown}")
        return tuple(raw)

    # Live values ------------------------------------------------------------

    def _active_courses(self):
        retired = self.catalogue.retired_course_ids
        return [course for course in self.catalogue.courses if course["id"] not in retired]

    def _course(self, course_id):
        return next(
            course for course in self._active_courses() if course["id"] == course_id
        )

    def _section_headings(self, course_id):
        yield GENERAL, "General"
        for course in self._active_courses():
            if course_id is None or course["id"] == course_id:
                yield course["id"], course["name"]

    def _values_for(self, course_id):
        if course_id == GENERAL:
            courses = {course["id"]: course for course in self._active_courses()}
            return {
                "course_list": self._course_list(),
                "fd_discount_terms": _lookup(
                    courses.get("dental-clinic-front-desk-operations", {}), ("fee", "discount_terms")
                ) or "",
                **{
                    name: _lookup(courses.get(key, {}), ("fee", "display")) or MISSING_VALUE
                    for name, key in GENERAL_COURSE_FEES.items()
                },
            }
        course = self._course(course_id)
        values = {}
        for name, path in COURSE_PLACEHOLDERS.items():
            value = _lookup(course, path)
            values[name] = str(value) if value not in (None, "") else MISSING_VALUE
        values["intake_list"] = self._intake_list(course_id)
        return values

    def _course_list(self):
        lines = []
        for course in self._active_courses():
            fee = course.get("fee", {}).get("display") or "fee to be confirmed"
            duration = course.get("duration") or "duration to be confirmed"
            lines.append(f"• {course['name']}: {fee}, {duration}")
        return "\n".join(lines)

    def _intake_list(self, course_id):
        intakes = self.catalogue.selectable_intakes_for(course_id)
        if not intakes:
            return self.catalogue.no_intakes_message
        lines = []
        for intake in intakes:
            line = f"• {intake['display_date']} ({intake['intake_type']})"
            if intake.get("is_demo"):
                line += " - demo date, requires staff confirmation"
            lines.append(line)
        return "\n".join(lines)


def _placeholders(text, owner):
    try:
        return {
            field for _, field, _, _ in string.Formatter().parse(text) if field is not None
        }
    except ValueError as error:
        raise KnowledgeBaseError(f"{owner} has malformed braces: {error}") from error


def _lookup(record, path):
    value = record
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


__all__ = [
    "GENERAL",
    "KNOWLEDGE_BASE_PATH",
    "KnowledgeBase",
    "KnowledgeBaseError",
    "KnowledgeEntry",
]
