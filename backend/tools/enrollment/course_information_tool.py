"""Approved course-information access for the Enrollment Agent."""

import json
from typing import Any

from crewai.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr


class CourseInformationInput(BaseModel):
    topic: str = Field(default="enrollment", min_length=1, max_length=100)


class CourseInformationOutput(BaseModel):
    source: str
    approved_information: dict[str, Any]
    matched_faqs: list[dict[str, Any]] = Field(default_factory=list)
    staff_confirmation_required: bool = False


class CourseInformationTool(BaseTool):
    name: str = "approved_course_information"
    description: str = (
        "Retrieve only staff-approved course, intake, fee, funding, payment and "
        "enrolment information from the structured course catalogue."
    )
    args_schema: type[BaseModel] = CourseInformationInput
    result_schema: type[BaseModel] = CourseInformationOutput

    _catalogue = PrivateAttr()

    def __init__(self, catalogue):
        super().__init__()
        self._catalogue = catalogue

    def _run(self, topic="enrollment"):
        approved = json.loads(self._catalogue.agent_context())
        lowered = str(topic).casefold()
        matches = [
            item
            for item in approved.get("faqs", [])
            if lowered in str(item.get("topic", "")).casefold()
            or str(item.get("topic", "")).casefold() in lowered
        ]
        if lowered in {"enrollment", "enrolment", "all"}:
            matches = approved.get("faqs", [])
        course = approved.get("course", {})
        return CourseInformationOutput(
            source="backend/data/course_information.json",
            approved_information={
                "course": course,
                "reply_style": approved.get("reply_style", {}),
                "demo_intakes_enabled": approved.get(
                    "demo_intakes_enabled", False
                ),
            },
            matched_faqs=matches,
            staff_confirmation_required=bool(
                course.get("venue", {}).get("confirmation_required")
                or not course.get("intakes")
            ),
        )


__all__ = [
    "CourseInformationInput",
    "CourseInformationOutput",
    "CourseInformationTool",
]
