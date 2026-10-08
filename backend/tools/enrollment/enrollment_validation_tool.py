"""Deterministic, read-only validation exposed to the Enrollment Agent."""

from datetime import date
from typing import Any

from crewai.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr

from backend.services.privacy import mask_nric
from backend.services.enrollment.validation import validate_field


VALIDATABLE_FIELDS = (
    "course",
    "full_name",
    "nric",
    "date_of_birth",
    "email",
    "preferred_intake_date",
    "mobile_number",
)


class EnrollmentValidationInput(BaseModel):
    fields: dict[str, Any] = Field(default_factory=dict)


class EnrollmentValidationOutput(BaseModel):
    valid_fields: list[str] = Field(default_factory=list)
    invalid_fields: dict[str, str] = Field(default_factory=dict)
    safe_normalized_values: dict[str, Any] = Field(default_factory=dict)


class EnrollmentValidationTool(BaseTool):
    name: str = "deterministic_enrollment_validation"
    description: str = (
        "Validate explicitly supplied enrollment fields using application rules, "
        "including NRIC/FIN checksum, DOB, email, Singapore mobile number, course, "
        "and intake. It never persists changes."
    )
    args_schema: type[BaseModel] = EnrollmentValidationInput
    result_schema: type[BaseModel] = EnrollmentValidationOutput

    _catalogue = PrivateAttr()
    _repository = PrivateAttr()
    _conversation_id: str = PrivateAttr()
    _user_id: str = PrivateAttr()
    _validation_options = PrivateAttr()

    def __init__(self, catalogue, repository, conversation_id, user_id, validation_options=None):
        super().__init__()
        self._catalogue = catalogue
        self._repository = repository
        self._conversation_id = conversation_id
        self._user_id = user_id
        self._validation_options = dict(validation_options or {})

    def _run(self, fields=None):
        supplied = dict(fields or {})
        if not supplied:
            supplied = self._saved_draft_values()
        valid = []
        invalid = {}
        normalized = {}
        for field, value in supplied.items():
            if field not in VALIDATABLE_FIELDS or value is None:
                continue
            result, error = validate_field(
                field, value, self._catalogue, **self._validation_options
            )
            if error:
                invalid[field] = error
                continue
            valid.append(field)
            normalized[field] = result
        return EnrollmentValidationOutput(
            valid_fields=valid,
            invalid_fields=invalid,
            safe_normalized_values={
                field: _safe_value(field, value)
                for field, value in normalized.items()
            },
        )

    def _saved_draft_values(self):
        conversation = self._repository.get_for_user(
            self._conversation_id, self._user_id
        )
        draft = conversation.enrollment_draft if conversation else None
        if draft is None:
            return {}
        return {
            field: getattr(draft, field)
            for field in VALIDATABLE_FIELDS
            if getattr(draft, field) is not None
        }

def _safe_value(field, value):
    if field == "nric":
        return mask_nric(value)
    if field == "email":
        local, separator, domain = str(value).partition("@")
        return f"{local[:1]}***@{domain}" if separator else "Protected"
    if field == "mobile_number":
        return f"****{str(value)[-4:]}"
    if isinstance(value, date):
        return value.isoformat()
    return value


__all__ = [
    "EnrollmentValidationInput",
    "EnrollmentValidationOutput",
    "EnrollmentValidationTool",
]
