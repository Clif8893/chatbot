"""Structured contracts returned by the CrewAI agents."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class IntentClassification(BaseModel):
    intent: Literal[
        "faq",
        "enrollment",
        "enrollment_interest",
        "workflow_question",
        "pause_workflow",
        "resume_workflow",
        "course_comparison",
        "small_talk",
        "unsupported_general_question",
        "unclear",
    ]
    confidence: float = Field(ge=0, le=1)
    short_reason: str = Field(min_length=1, max_length=500)


class EnrollmentFields(BaseModel):
    course: str | None = None
    full_name: str | None = None
    nric: str | None = None
    date_of_birth: str | None = None
    email: str | None = None
    preferred_intake_date: str | None = None
    mobile_number: str | None = None


class EnrollmentExtraction(BaseModel):
    extracted_fields: EnrollmentFields = Field(default_factory=EnrollmentFields)
    missing_fields: list[str] = Field(default_factory=list)
    next_question: str | None = None
    ready_for_confirmation: bool = False
    confirmation: Literal["confirm", "reject", "none"] = "none"
    correction_requested: bool = False


class MemoryFact(BaseModel):
    """One fact the enquiry agent proposes remembering about the participant."""

    key: Literal[
        "preferred_name",
        "course_interest",
        "preferred_intake",
        "payment_preference",
        "sfc_balance_stated",
        "ntuc_member",
        "concern",
        "constraint",
        "topic_answered",
    ]
    value: str = Field(min_length=1, max_length=200)


class EnquiryTurn(BaseModel):
    """Structured result of one Module A enquiry turn."""

    reply: str = Field(min_length=1, max_length=2000)
    response_type: Literal[
        "grounded_answer",
        "general_knowledge",
        "no_confirmed_info",
        "small_talk",
        "deescalation",
        "clarifying_question",
        "boundary",
        "handoff_enrollment",
    ]
    # Knowledge-base entry ids the reply relies on; required for grounded answers.
    kb_entry_ids: list[str] = Field(default_factory=list)
    user_tone: Literal[
        "neutral", "positive", "frustrated", "rude", "abusive", "distressed"
    ] = "neutral"
    escalate: bool = False
    escalation_category: Literal[
        "",
        "unanswered_question",
        "human_request",
        "abusive_conduct",
        "distress",
        "complaint",
        "sensitive",
    ] = ""
    # For no_confirmed_info: what was asked about, phrased to follow
    # "I don't have confirmed information about". Never contains facts.
    unanswered_subject: str = Field(default="", max_length=120)
    enrollment_interest: bool = False
    memory_updates: list[MemoryFact] = Field(default_factory=list, max_length=10)
    # Ids from the approved suggestion menu for the buttons under the reply. Up
    # to five candidates are kept; the application filters, then shows three.
    suggestions: list[str] = Field(default_factory=list, max_length=5)

    # Optional extras are trimmed rather than rejected, so a slightly oversized
    # field never costs the participant their reply. The reply and its type
    # stay strict.
    @field_validator("suggestions", mode="before")
    @classmethod
    def _trim_suggestions(cls, ids):
        if not isinstance(ids, list):
            return []
        cleaned = [
            item.strip()
            for item in ids
            if isinstance(item, str) and item.strip() and len(item.strip()) <= 40
        ]
        return cleaned[:5]

    @field_validator("memory_updates", mode="before")
    @classmethod
    def _trim_memory_updates(cls, facts):
        if not isinstance(facts, list):
            return []
        allowed = set(MemoryFact.model_fields["key"].annotation.__args__)
        kept = []
        for fact in facts:
            if isinstance(fact, MemoryFact):
                kept.append(fact)
            elif (
                isinstance(fact, dict)
                and fact.get("key") in allowed
                and isinstance(fact.get("value"), str)
                and fact["value"].strip()
            ):
                kept.append({"key": fact["key"], "value": fact["value"].strip()[:200]})
        return kept[:10]

    @field_validator("unanswered_subject", mode="before")
    @classmethod
    def _trim_subject(cls, subject):
        return str(subject or "")[:120]

    @field_validator("user_tone", mode="before")
    @classmethod
    def _known_tone(cls, tone):
        known = {"neutral", "positive", "frustrated", "rude", "abusive", "distressed"}
        return tone if tone in known else "neutral"

    @field_validator("escalation_category", mode="before")
    @classmethod
    def _known_category(cls, category):
        known = {
            "", "unanswered_question", "human_request", "abusive_conduct",
            "distress", "complaint", "sensitive",
        }
        if category in known:
            return category
        # An unrecognised label still reaches staff, as a general case.
        return "sensitive" if category else ""

    @model_validator(mode="after")
    def _consistent_escalation(self):
        # A category implies an escalation; an escalation the model did not
        # categorise still reaches staff, as a general sensitive case.
        if self.escalation_category and not self.escalate:
            self.escalate = True
        elif self.escalate and not self.escalation_category:
            self.escalation_category = "sensitive"
        return self


class FollowupMessage(BaseModel):
    """A personalised lead reminder based on an approved template."""

    message: str = Field(min_length=1, max_length=700)


class ConversationDigest(BaseModel):
    """Rolling summary of a conversation, used as memory for later turns and chats."""

    summary: str = Field(min_length=1, max_length=1200)


class AIServiceError(RuntimeError):
    """Safe boundary for model/provider failures."""


class AIConfigurationError(AIServiceError):
    """Raised when a server-only model setting is missing."""
