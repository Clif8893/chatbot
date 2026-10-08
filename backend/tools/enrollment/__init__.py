"""CrewAI tools used by the enrollment task at runtime."""

from backend.tools.enrollment.conversation_memory_tool import ConversationMemoryTool
from backend.tools.enrollment.course_information_tool import CourseInformationTool
from backend.tools.enrollment.enrollment_draft_tool import EnrollmentDraftTool
from backend.tools.enrollment.enrollment_validation_tool import EnrollmentValidationTool
from backend.tools.enrollment.intake_availability_tool import IntakeAvailabilityTool


def build_enrollment_tools(
    repository,
    catalogue,
    conversation,
    context_limit,
    missing_fields_resolver,
    validation_options=None,
):
    """Build tools scoped to one authenticated participant conversation."""
    if not conversation.user_id:
        raise ValueError("Enrollment tools require an authenticated conversation owner.")
    return [
        ConversationMemoryTool(
            repository,
            conversation.id,
            conversation.user_id,
            context_limit,
        ),
        CourseInformationTool(catalogue),
        EnrollmentDraftTool(
            repository,
            catalogue,
            conversation.id,
            conversation.user_id,
            missing_fields_resolver,
        ),
        EnrollmentValidationTool(
            catalogue,
            repository,
            conversation.id,
            conversation.user_id,
            validation_options,
        ),
        IntakeAvailabilityTool(
            catalogue,
            repository,
            conversation.id,
            conversation.user_id,
        ),
    ]


__all__ = [
    "ConversationMemoryTool",
    "CourseInformationTool",
    "EnrollmentDraftTool",
    "EnrollmentValidationTool",
    "IntakeAvailabilityTool",
    "build_enrollment_tools",
]
