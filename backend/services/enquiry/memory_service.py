"""Participant memory for Module A: what the enquiry agent may know about someone.

Context for each turn has four layers:
1. the current conversation's recent messages;
2. a rolling summary of the current conversation;
3. the participant's whitelisted profile facts;
4. summaries (plus unsummarised tails) of the participant's latest earlier chats.

Memory is strictly scoped to one subject: the signed-in participant account, or
a hashed channel identity for WhatsApp/email leads. Everything is rendered to the
model as data, and personal identifiers are never stored in the profile.
"""

import json
import re
from dataclasses import dataclass, field

from backend.agents.schemas import AIConfigurationError, AIServiceError, MemoryFact
from backend.extensions import db
from backend.models import Conversation, Message, utc_now
from backend.models.memory import ConversationMemory, ParticipantMemory
from backend.models.operations import ContactMapping
from backend.services.enquiry.facts import FactSet, extract_facts
from backend.services.enquiry.message_policy import INJECTION, precheck
from backend.services.privacy import (
    EMAIL_PATTERN,
    ISO_DATE_PATTERN,
    MOBILE_PATTERN,
    NRIC_PATTERN,
    SLASH_DATE_PATTERN,
    TEXT_DATE_PATTERN,
    mask_nrics_in_text,
)


SINGLE_VALUE_KEYS = (
    "preferred_name",
    "course_interest",
    "preferred_intake",
    "payment_preference",
    "sfc_balance_stated",
    "ntuc_member",
)
LIST_KEYS = ("concern", "constraint", "topic_answered")
LIST_LIMIT = 5
# Answered topics are approved-entry ids recorded by the application, used to
# avoid repeating answers and suggestions; there are only a few dozen entries.
LIST_LIMITS = {"topic_answered": 50}
VALUE_LIMIT = 200
SUMMARY_LIMIT = 1200
SUMMARY_BATCH = 10
PRIOR_CONVERSATION_LIMIT = 3
PRIOR_TAIL_LIMIT = 6
MESSAGE_CHAR_LIMIT = 500

LABELS = {
    "preferred_name": "Preferred name",
    "course_interest": "Course interest",
    "preferred_intake": "Preferred intake",
    "payment_preference": "Payment preference",
    "sfc_balance_stated": "SkillsFuture Credit balance they stated",
    "ntuc_member": "NTUC Union member",
    "concern": "Concerns raised",
    "constraint": "Constraints mentioned",
    "topic_answered": "Topics already answered",
}
# A full date with a year could be a date of birth; only an intake may hold one.
_FULL_DATE_PATTERNS = (ISO_DATE_PATTERN, SLASH_DATE_PATTERN, TEXT_DATE_PATTERN)
_DATED_KEYS = {"preferred_intake"}


@dataclass
class MemoryContext:
    conversation: str = ""
    conversation_summary: str = ""
    profile: dict = field(default_factory=dict)
    prior_conversations: list = field(default_factory=list)
    abuse_strikes: int = 0
    participant_facts: FactSet = field(default_factory=FactSet)

    @property
    def answered_entry_ids(self):
        return {item["value"] for item in self.profile.get("topic_answered") or []}

    def profile_block(self, topic_names=None):
        """Profile as prompt data; ``topic_names`` maps answered entry ids to topics."""
        topic_names = topic_names or {}
        lines = []
        for key in (*SINGLE_VALUE_KEYS, *LIST_KEYS):
            items = self.profile.get(key) or []
            if not items:
                continue
            if key == "topic_answered":
                # One line, no dates: the list can be long.
                rendered = "; ".join(
                    topic_names.get(item["value"], item["value"]) for item in items
                )
            else:
                rendered = "; ".join(
                    f"{item['value']} (noted {str(item.get('recorded_at', ''))[:10]})"
                    for item in items
                )
            lines.append(f"- {LABELS[key]}: {rendered}")
        return "\n".join(lines) or "(nothing remembered yet)"

    def prior_block(self):
        return "\n\n".join(self.prior_conversations) or "(no earlier conversations)"

    def conversation_block(self):
        parts = []
        if self.conversation_summary:
            parts.append(f"Summary of earlier messages in this chat:\n{self.conversation_summary}")
        parts.append(f"Recent messages:\n{self.conversation or '(this is the first message)'}")
        return "\n\n".join(parts)


class MemoryService:
    def __init__(self, summary_batch=SUMMARY_BATCH):
        self.summary_batch = summary_batch

    # Subject ----------------------------------------------------------------

    @staticmethod
    def subject_for(conversation):
        if conversation.user_id:
            return "user", conversation.user_id
        mapping = ContactMapping.query.filter_by(conversation_id=conversation.id).first()
        if mapping is not None:
            return "contact", mapping.identity_digest
        return None

    # Reading ----------------------------------------------------------------

    def build_context(self, conversation, prior_messages, current_text):
        """Assemble the model's view of this participant for one turn."""
        state = self._conversation_memory(conversation)
        forgotten = state.forgotten_through_sequence if state else 0
        window = [
            message
            for message in prior_messages
            if (message.sequence or 0) > forgotten and message.role in {"user", "assistant"}
        ]
        subject = self.subject_for(conversation)
        profile_row = self._profile(subject) if subject else None
        profile = self._load(profile_row)

        facts = extract_facts(current_text)
        for message in window:
            if message.role == "user":
                facts = facts.union(extract_facts(message.content))
        for items in profile.values():
            for item in items:
                facts = facts.union(extract_facts(item["value"]))

        return MemoryContext(
            conversation=_render_messages(window),
            conversation_summary=(state.summary or "") if state else "",
            profile=profile,
            prior_conversations=self._prior_conversations(conversation, subject),
            abuse_strikes=state.abuse_strikes if state else 0,
            participant_facts=facts,
        )

    def _prior_conversations(self, conversation, subject):
        if subject is None:
            return []
        kind, subject_id = subject
        if kind == "user":
            query = Conversation.query.filter(Conversation.user_id == subject_id)
        else:
            mapped = db.select(ContactMapping.conversation_id).where(
                ContactMapping.identity_digest == subject_id
            )
            query = Conversation.query.filter(Conversation.id.in_(mapped))
        others = (
            query.filter(Conversation.id != conversation.id)
            .order_by(Conversation.updated_at.desc())
            .limit(PRIOR_CONVERSATION_LIMIT)
            .all()
        )
        rendered = []
        for other in others:
            state = self._conversation_memory(other)
            covered = max(
                state.through_sequence if state else 0,
                state.forgotten_through_sequence if state else 0,
            )
            tail = (
                Message.query.filter(
                    Message.conversation_id == other.id,
                    Message.sequence > covered,
                    Message.role.in_(("user", "assistant")),
                )
                .order_by(Message.sequence.desc())
                .limit(PRIOR_TAIL_LIMIT)
                .all()
            )
            summary = (state.summary or "") if state else ""
            if not summary and not tail:
                continue
            block = [f"Chat last active {str(other.updated_at)[:10]}:"]
            if summary:
                block.append(f"Summary: {summary}")
            if tail:
                block.append("Latest messages:\n" + _render_messages(list(reversed(tail))))
            rendered.append("\n".join(block))
        return rendered

    # Writing ----------------------------------------------------------------

    def apply(self, conversation, facts, now=None):
        """Store whitelisted facts; returns the keys stored. Rejected values are dropped."""
        subject = self.subject_for(conversation)
        if subject is None or not facts:
            return []
        recorded_at = (now or utc_now()).isoformat()
        row = self._profile(subject, create=True)
        data = self._load(row)
        stored = []
        for fact in facts:
            value = clean_fact_value(fact.key, fact.value)
            if value is None:
                continue
            entry = {
                "value": value,
                "conversation_id": conversation.id,
                "recorded_at": recorded_at,
            }
            others = [
                item
                for item in data.get(fact.key, [])
                if item["value"].casefold() != value.casefold()
            ]
            if fact.key in LIST_KEYS:
                data[fact.key] = [*others, entry][-LIST_LIMITS.get(fact.key, LIST_LIMIT):]
            else:
                data[fact.key] = [entry]
            stored.append(fact.key)
        if stored:
            row.facts = json.dumps(data)
            row.updated_at = now or utc_now()
            db.session.flush()
        return stored

    def record_strike(self, conversation):
        state = self._conversation_memory(conversation, create=True)
        state.abuse_strikes = (state.abuse_strikes or 0) + 1
        db.session.flush()
        return state.abuse_strikes

    def maybe_summarise(self, conversation, summarise):
        """Fold every full batch of unsummarised messages into the rolling summary.

        ``summarise(previous_summary, transcript)`` returns the new summary text.
        A failed call keeps the previous summary and is retried on a later turn.
        """
        state = self._conversation_memory(conversation, create=True)
        start = max(state.through_sequence or 0, state.forgotten_through_sequence or 0)
        pending = (
            Message.query.filter(
                Message.conversation_id == conversation.id,
                Message.sequence > start,
                Message.role.in_(("user", "assistant")),
            )
            .order_by(Message.sequence.asc())
            .all()
        )
        if len(pending) < self.summary_batch:
            return False
        try:
            summary = summarise(state.summary or "", _render_messages(pending))
        except AIConfigurationError:
            raise
        except AIServiceError:
            return False
        state.summary = _redact(str(summary or "")).strip()[:SUMMARY_LIMIT] or None
        state.through_sequence = pending[-1].sequence
        db.session.flush()
        return True

    def forget(self, conversation):
        """Delete the profile and all summaries for this participant, and stop
        earlier messages being used as context."""
        subject = self.subject_for(conversation)
        conversations = [conversation]
        if subject is not None:
            profile = self._profile(subject)
            if profile is not None:
                db.session.delete(profile)
            kind, subject_id = subject
            if kind == "user":
                conversations = Conversation.query.filter_by(user_id=subject_id).all()
            else:
                mapped = db.select(ContactMapping.conversation_id).where(
                    ContactMapping.identity_digest == subject_id
                )
                conversations = Conversation.query.filter(Conversation.id.in_(mapped)).all()
                if conversation not in conversations:
                    conversations.append(conversation)
        for item in conversations:
            last = (
                db.session.query(db.func.max(Message.sequence))
                .filter(Message.conversation_id == item.id)
                .scalar()
                or 0
            )
            state = self._conversation_memory(item, create=True)
            state.summary = None
            state.through_sequence = last
            state.forgotten_through_sequence = last
        db.session.flush()
        return len(conversations)

    def purge_expired(self, cutoff):
        """Delete profiles not updated since ``cutoff`` (decision D6)."""
        result = db.session.execute(
            db.delete(ParticipantMemory).where(ParticipantMemory.updated_at < cutoff)
        )
        return result.rowcount or 0

    # Storage helpers ----------------------------------------------------------

    @staticmethod
    def _profile(subject, create=False):
        kind, subject_id = subject
        row = ParticipantMemory.query.filter_by(
            subject_type=kind, subject_id=subject_id
        ).first()
        if row is None and create:
            row = ParticipantMemory(subject_type=kind, subject_id=subject_id, facts="{}")
            db.session.add(row)
            db.session.flush()
        return row

    @staticmethod
    def _conversation_memory(conversation, create=False):
        row = db.session.get(ConversationMemory, conversation.id)
        if row is None and create:
            row = ConversationMemory(conversation_id=conversation.id)
            db.session.add(row)
            db.session.flush()
        return row

    @staticmethod
    def _load(row):
        if row is None or not row.facts:
            return {}
        try:
            data = json.loads(row.facts)
        except (TypeError, ValueError):
            return {}
        return {
            key: [item for item in items if isinstance(item, dict) and item.get("value")]
            for key, items in data.items()
            if key in LABELS and isinstance(items, list)
        }


_AMOUNT = r"(?:S\$|SGD\s*|\$)\s*(\d[\d,]*(?:\.\d{1,2})?)"
_SFC = r"\b(?:skills\s*future|skilsfuture|sfc)\b"
# "I have $500 in SkillsFuture credits" / "my SkillsFuture balance is $350".
_STATED_CREDIT = re.compile(
    rf"\b(?:i\s+(?:have|got|only\s+have|still\s+have)|i'?ve\s+got)\b[^.?!]{{0,40}}?{_AMOUNT}"
    rf"[^.?!]{{0,30}}?{_SFC}"
    rf"|{_SFC}[^.?!]{{0,30}}?\b(?:balance|credits?)\b[^.?!]{{0,15}}?{_AMOUNT}",
    re.I,
)


def stated_credit(text):
    """The SkillsFuture balance a participant states, e.g. "S$500", or None."""
    match = _STATED_CREDIT.search(str(text or ""))
    if not match:
        return None
    amount = (match.group(1) or match.group(2)).replace(",", "")
    return f"S${amount}"


def facts_from_text(text):
    """Facts recognised deterministically, so they are remembered whichever
    module answers the message."""
    credit = stated_credit(text)
    return [MemoryFact(key="sfc_balance_stated", value=credit)] if credit else []


def clean_fact_value(key, value):
    """Normalise a proposed memory value, or return None when it must not be stored."""
    if key not in LABELS:
        return None
    text = re.sub(r"\s+", " ", str(value or "")).strip()[:VALUE_LIMIT]
    if not text:
        return None
    if (
        NRIC_PATTERN.search(text)
        or EMAIL_PATTERN.search(text)
        or MOBILE_PATTERN.search(text)
    ):
        return None
    if key not in _DATED_KEYS and any(p.search(text) for p in _FULL_DATE_PATTERNS):
        return None
    if precheck(text).kind == INJECTION:
        return None
    return text


def _redact(text):
    text = mask_nrics_in_text(str(text or ""))
    text = EMAIL_PATTERN.sub("[email]", text)
    return MOBILE_PATTERN.sub("[mobile]", text)


def _render_messages(messages):
    lines = []
    for message in messages:
        speaker = "Participant" if message.role == "user" else "Assistant"
        content = _redact(message.content)
        if len(content) > MESSAGE_CHAR_LIMIT:
            content = content[:MESSAGE_CHAR_LIMIT].rstrip() + " …"
        lines.append(f"{speaker}: {content}")
    return "\n".join(lines)


__all__ = [
    "MemoryContext",
    "MemoryService",
    "clean_fact_value",
    "facts_from_text",
    "stated_credit",
]
