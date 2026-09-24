"""Owner-scoped resolution of the enrollment a participant means."""

from dataclasses import dataclass, field
import re

from backend.extensions import db
from backend.models import Conversation, EnrollmentDraft
from backend.models.operations import CourseDate


REFERENCE_PATTERN = re.compile(r"\bENR-\d{4}-\d{6}\b", re.I)
INACTIVE_STATUSES = {"discarded", "cancelled"}


@dataclass
class ResolvedEnrollmentContext:
    enrollment: EnrollmentDraft | None = None
    course: dict | None = None
    intake: CourseDate | None = None
    conversation: Conversation | None = None
    resolution_source: str = "none"
    candidates: list = field(default_factory=list)

    @property
    def ambiguous(self):
        return bool(self.candidates) and self.enrollment is None


class CurrentEnrollmentResolver:
    """Resolve only enrollments owned by the authenticated conversation user."""

    def __init__(self, catalogue):
        self.catalogue = catalogue

    def resolve(self, conversation, message="", *, include_inactive=True):
        if conversation is None or not conversation.user_id:
            return ResolvedEnrollmentContext(resolution_source="no_authenticated_owner")

        reference = REFERENCE_PATTERN.search(str(message or ""))
        if reference:
            enrollment = self._owned_query(conversation).filter(
                EnrollmentDraft.friendly_reference == reference.group(0).upper()
            ).one_or_none()
            if enrollment is not None:
                return self._context(enrollment, "explicit_reference")

        current = conversation.enrollment_draft
        if current is not None and current.status != "discarded":
            return self._context(current, "current_conversation")

        if conversation.pending_enrollment_id:
            pending = self._owned_query(conversation).filter(
                EnrollmentDraft.id == conversation.pending_enrollment_id
            ).one_or_none()
            if pending is not None:
                return self._context(pending, "pending_action")

        owned = self._owned_query(conversation).order_by(
            EnrollmentDraft.updated_at.desc(), EnrollmentDraft.id.desc()
        ).all()
        active = [row for row in owned if row.status not in INACTIVE_STATUSES]
        if len(active) == 1:
            return self._context(active[0], "only_active_enrollment")
        if len(active) > 1:
            return ResolvedEnrollmentContext(
                resolution_source="multiple_active_enrollments",
                candidates=[self._context(row, "candidate") for row in active],
            )
        if include_inactive and len(owned) == 1:
            return self._context(owned[0], "only_owned_enrollment")
        return ResolvedEnrollmentContext(resolution_source="no_enrollment")

    def clarification_reply(self, context):
        lines = ["Your Enrollments", "", "I found more than one active enrollment:", ""]
        for item in context.candidates:
            enrollment = item.enrollment
            reference = enrollment.friendly_reference or "Reference pending"
            course_name = item.course.get("name") if item.course else enrollment.course
            lines.append(
                f"- {reference} — {course_name or 'Course not selected'} — "
                f"{self.intake_display(item.intake, enrollment)}"
            )
        lines.extend(
            [
                "",
                "Please reply with the enrollment reference you mean. Please open the relevant previous chat if preferred.",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def intake_display(intake, enrollment):
        if intake is not None:
            start, end = intake.date, intake.end_date
        else:
            start = enrollment.intake_start_date or enrollment.preferred_intake_date
            end = enrollment.intake_end_date or start
        if not start:
            return "Intake awaiting confirmation"
        if start == end:
            return f"{start.day} {start.strftime('%B %Y')}"
        if start.year == end.year and start.month == end.month:
            return f"{start.day}–{end.day} {start.strftime('%B %Y')}"
        if start.year == end.year:
            return f"{start.day} {start.strftime('%B')}–{end.day} {end.strftime('%B %Y')}"
        return f"{start.day} {start.strftime('%B %Y')}–{end.day} {end.strftime('%B %Y')}"

    def _owned_query(self, conversation):
        return EnrollmentDraft.query.join(
            Conversation, Conversation.id == EnrollmentDraft.conversation_id
        ).filter(Conversation.user_id == conversation.user_id)

    def _context(self, enrollment, source):
        intake = (
            db.session.get(CourseDate, enrollment.intake_id)
            if enrollment.intake_id
            else None
        )
        course = self.catalogue.course_for(
            enrollment.course_id
            or (intake.course_id if intake is not None else None)
            or enrollment.course
        )
        return ResolvedEnrollmentContext(
            enrollment=enrollment,
            course=course,
            intake=intake,
            conversation=enrollment.conversation,
            resolution_source=source,
        )


__all__ = ["CurrentEnrollmentResolver", "ResolvedEnrollmentContext"]
