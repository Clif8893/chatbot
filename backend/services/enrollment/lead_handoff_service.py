"""Validated, deterministic handoff from an enquiry lead into enrollment.

The future enquiry agent should call this application service.  It deliberately
contains no CrewAI or LLM code: validation, duplicate checks and writes remain
owned by the Flask application.
"""

import re

from backend.extensions import db
from backend.models import Conversation
from backend.services.enrollment.registration_group_service import (
    add_course,
    ordered_courses,
    save_intake,
)
from backend.services.enrollment.validation import validate_field


class LeadHandoffConflict(ValueError):
    """Raised when a handoff conflicts with validated data already saved."""


class EnrollmentLeadHandoffService:
    """Merge one validated enquiry lead into its conversation-scoped draft."""

    def __init__(
        self,
        catalogue,
        repository,
        operations_service,
        validation_options=None,
    ):
        self.catalogue = catalogue
        self.repository = repository
        self.operations = operations_service
        self.validation_options = dict(validation_options or {})

    def receive(
        self,
        *,
        conversation_id,
        full_name,
        phone_number,
        selected_course,
        preferred_intake_date,
        lead_status="qualified",
    ):
        """Validate and stage lead data; the caller owns the transaction commit.

        Replaying an identical payload is idempotent.  Existing validated values
        are never replaced by a conflicting handoff.
        """
        conversation = db.session.get(Conversation, str(conversation_id or "").strip())
        if conversation is None:
            raise LookupError("Conversation was not found.")

        status = self._validate_status(lead_status)
        course = self.catalogue.match_course(selected_course)
        if course is None:
            raise ValueError("Selected course is not available for enrollment.")

        name = self._validate("full_name", full_name, course["name"])
        mobile = self._validate("mobile_number", phone_number, course["name"])
        intake_date = self._validate(
            "preferred_intake_date", preferred_intake_date, course["name"]
        )
        intake, intake_error = self.catalogue.match_intake_selection(
            intake_date.isoformat(), course["id"]
        )
        if intake_error or intake is None:
            raise ValueError(intake_error or "Preferred intake is not available.")

        draft = self.repository.get_or_create_draft(conversation)
        self._require_same_or_empty(draft, "full_name", name)
        self._require_same_or_empty(draft, "mobile_number", mobile)
        existing_courses = ordered_courses(draft)
        if existing_courses and all(row.course_id != course["id"] for row in existing_courses):
            raise LeadHandoffConflict(
                "Selected course conflicts with the enrollment already saved for this conversation."
            )

        if not existing_courses:
            course_row, _ = add_course(draft, course, self.catalogue)
        else:
            course_row = next(row for row in existing_courses if row.course_id == course["id"])
        if course_row.intake_id and course_row.intake_id != intake["id"]:
            raise LeadHandoffConflict(
                "Preferred intake conflicts with the enrollment already saved for this conversation."
            )

        draft.full_name = name
        draft.mobile_number = mobile
        if not course_row.intake_id:
            save_intake(course_row, intake, self.catalogue)
        # A delayed/retried handoff must never move a participant backwards from
        # confirmation or a later operational state.
        if draft.status in {None, "collecting"}:
            draft.status = "collecting"
        if draft.validation_status in {None, "pending", "incomplete"}:
            draft.validation_status = "incomplete"
        conversation.active_intent = "enrollment"

        lead = self.operations.lead_for(conversation.id)
        if lead.status not in {"enrolled", "declined", "escalated"}:
            lead.status = status
        db.session.flush()
        return draft

    def _validate(self, field, value, course_name):
        normalized, error = validate_field(
            field,
            value,
            self.catalogue,
            course_value=course_name,
            **self.validation_options,
        )
        if error:
            raise ValueError(error)
        return normalized

    @staticmethod
    def _validate_status(value):
        status = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", status):
            raise ValueError("Lead status must be a short text status value.")
        return status

    @staticmethod
    def _require_same_or_empty(draft, field, value):
        current = getattr(draft, field)
        if current is not None and current != value:
            raise LeadHandoffConflict(
                f"{field.replace('_', ' ').title()} conflicts with the value already saved "
                "for this conversation."
            )

__all__ = [
    "EnrollmentLeadHandoffService",
    "LeadHandoffConflict",
]
