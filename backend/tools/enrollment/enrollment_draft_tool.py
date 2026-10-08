"""Read-only, ownership-scoped enrollment draft access for CrewAI."""

from datetime import timezone
from decimal import Decimal
from typing import Any

from crewai.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr

from backend.services.privacy import mask_nric


class EnrollmentDraftInput(BaseModel):
    include_safe_values: bool = True


class EnrollmentDraftOutput(BaseModel):
    authorised: bool
    conversation_id: str
    status: str | None = None
    confirmation_status: str
    saved_fields: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    needs_correction: dict[str, str] = Field(default_factory=dict)
    safe_values: dict[str, Any] = Field(default_factory=dict)
    updated_at: str | None = None


class EnrollmentDraftTool(BaseTool):
    name: str = "current_enrollment_draft"
    description: str = (
        "Retrieve the current authorised enrollment draft, its saved and missing "
        "fields, and privacy-safe display values. This tool never writes to the database."
    )
    args_schema: type[BaseModel] = EnrollmentDraftInput
    result_schema: type[BaseModel] = EnrollmentDraftOutput

    _repository = PrivateAttr()
    _catalogue = PrivateAttr()
    _conversation_id: str = PrivateAttr()
    _user_id: str = PrivateAttr()
    _missing_fields_resolver = PrivateAttr()

    def __init__(
        self,
        repository,
        catalogue,
        conversation_id,
        user_id,
        missing_fields_resolver,
    ):
        super().__init__()
        self._repository = repository
        self._catalogue = catalogue
        self._conversation_id = conversation_id
        self._user_id = user_id
        self._missing_fields_resolver = missing_fields_resolver

    def _run(self, include_safe_values=True):
        conversation = self._repository.get_for_user(
            self._conversation_id, self._user_id
        )
        if conversation is None:
            return EnrollmentDraftOutput(
                authorised=False,
                conversation_id=self._conversation_id,
                confirmation_status="unavailable",
            )
        draft = conversation.enrollment_draft
        if draft is None:
            return EnrollmentDraftOutput(
                authorised=True,
                conversation_id=conversation.id,
                confirmation_status="not_started",
            )
        field_names = (
            "course",
            "course_fee",
            "full_name",
            "nric",
            "date_of_birth",
            "email",
            "mobile_number",
            "preferred_intake_date",
            "payment_method",
            "skillsfuture_amount",
            "paynow_amount",
        )
        saved = [name for name in field_names if getattr(draft, name) is not None]
        safe_values = _safe_values(draft) if include_safe_values else {}
        return EnrollmentDraftOutput(
            authorised=True,
            conversation_id=conversation.id,
            status=draft.status,
            confirmation_status=(
                "confirmed" if draft.confirmed_at else "not_confirmed"
            ),
            saved_fields=saved,
            missing_fields=self._missing_fields_resolver(draft, self._catalogue),
            needs_correction=dict(draft.validation_errors or {}),
            safe_values=safe_values,
            updated_at=_iso_timestamp(draft.updated_at),
        )


def _safe_values(draft):
    return {
        "course": draft.course,
        "course_fee": _amount(draft.course_fee),
        "full_name": draft.full_name,
        "nric": mask_nric(draft.nric),
        "date_of_birth_saved": draft.date_of_birth is not None,
        "email": _mask_email(draft.email),
        "mobile_number": _mask_mobile(draft.mobile_number),
        "preferred_intake_date": (
            draft.preferred_intake_date.isoformat()
            if draft.preferred_intake_date
            else None
        ),
        "selected_intake_id": draft.intake_id,
        "payment_method": draft.payment_method,
        "skillsfuture_amount": _amount(draft.skillsfuture_amount),
        "paynow_amount": _amount(draft.paynow_amount),
    }


def _amount(value):
    return f"{Decimal(value):.2f}" if value is not None else None


def _mask_email(value):
    if not value or "@" not in value:
        return None
    local, domain = value.split("@", 1)
    return f"{local[:1]}***@{domain}"


def _mask_mobile(value):
    return f"****{str(value)[-4:]}" if value else None


def _iso_timestamp(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


__all__ = ["EnrollmentDraftInput", "EnrollmentDraftOutput", "EnrollmentDraftTool"]
