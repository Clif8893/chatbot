"""Structured contracts returned by the CrewAI agents."""

from typing import Literal

from pydantic import BaseModel, Field


class IntentClassification(BaseModel):
    intent: Literal["faq", "enrollment", "unclear"]
    confidence: float = Field(ge=0, le=1)
    short_reason: str = Field(min_length=1, max_length=500)


class FAQAnswer(BaseModel):
    answer: str = Field(min_length=1)
    matched_topics: list[str] = Field(default_factory=list)


class EnrollmentFields(BaseModel):
    course: str | None = None
    full_name: str | None = None
    nric: str | None = None
    date_of_birth: str | None = None
    email: str | None = None
    preferred_intake_date: str | None = None
    mobile_number: str | None = None
    payment_method: str | None = None
    skillsfuture_amount: str | float | int | None = None
    paynow_amount: str | float | int | None = None


class EnrollmentExtraction(BaseModel):
    extracted_fields: EnrollmentFields = Field(default_factory=EnrollmentFields)
    missing_fields: list[str] = Field(default_factory=list)
    next_question: str | None = None
    ready_for_confirmation: bool = False
    confirmation: Literal["confirm", "reject", "none"] = "none"
    correction_requested: bool = False


class AIServiceError(RuntimeError):
    """Safe boundary for model/provider failures."""


class AIConfigurationError(AIServiceError):
    """Raised when a server-only model setting is missing."""
