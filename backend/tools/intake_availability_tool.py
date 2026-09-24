"""Read-only database-backed intake availability for the Enrollment Agent."""

from typing import Any

from crewai.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr


class IntakeAvailabilityInput(BaseModel):
    requested_intake: str | None = None


class IntakeAvailabilityOutput(BaseModel):
    status: str
    available_intakes: list[dict[str, Any]] = Field(default_factory=list)
    selected_intake: dict[str, Any] | None = None
    message: str | None = None


class IntakeAvailabilityTool(BaseTool):
    name: str = "course_intake_availability"
    description: str = (
        "Read future course intakes from the application database, check status and "
        "capacity, and resolve an intake number, date or description. It never writes "
        "to the database or invents a course date."
    )
    args_schema: type[BaseModel] = IntakeAvailabilityInput
    result_schema: type[BaseModel] = IntakeAvailabilityOutput

    _catalogue = PrivateAttr()
    _repository = PrivateAttr()
    _conversation_id: str = PrivateAttr()
    _user_id: str = PrivateAttr()

    def __init__(self, catalogue, repository, conversation_id, user_id):
        super().__init__()
        self._catalogue = catalogue
        self._repository = repository
        self._conversation_id = conversation_id
        self._user_id = user_id

    def _run(self, requested_intake=None):
        conversation = self._repository.get_for_user(
            self._conversation_id, self._user_id
        )
        if conversation is None:
            return IntakeAvailabilityOutput(status="unauthorised")
        available = [_safe_item(item) for item in self._catalogue.selectable_intakes]
        if not requested_intake:
            return IntakeAvailabilityOutput(
                status="available" if available else "none_available",
                available_intakes=available,
            )
        selected, error = self._catalogue.match_intake_selection(requested_intake)
        return IntakeAvailabilityOutput(
            status="selected" if selected else "requires_clarification",
            available_intakes=available,
            selected_intake=_safe_item(selected) if selected else None,
            message=error,
        )


def _safe_item(item):
    if item is None:
        return None
    return {
        key: item.get(key)
        for key in (
            "id", "start_date", "end_date", "start_time", "end_time",
            "intake_type", "display_date", "status", "available_places", "is_demo",
        )
    }


__all__ = ["IntakeAvailabilityInput", "IntakeAvailabilityOutput", "IntakeAvailabilityTool"]
