"""Runtime contract and privacy tests for the Enrollment Agent tools."""

from types import SimpleNamespace

from crewai import Task
from pydantic import BaseModel

from backend.agents.schemas import EnrollmentExtraction
from backend.extensions import db
from backend.models import Conversation, User
from backend.services.conversation_repository import ConversationRepository
from backend.services.crewai_service import CrewAIService
from backend.services.enrollment_service import EnrollmentService
from backend.tools import build_enrollment_tools
from backend.tools.conversation_memory_tool import ConversationMemoryTool


def _tools_for(app, conversation_id, context_limit=3):
    repository = app.extensions["repository"]
    catalogue = app.extensions["catalogue"]
    conversation = db.session.get(Conversation, conversation_id)
    return build_enrollment_tools(
        repository,
        catalogue,
        conversation,
        context_limit,
        EnrollmentService.missing_fields,
    )


def test_tools_have_typed_inputs_outputs_and_safe_scoped_results(
    app, client, conversation_id
):
    repository = app.extensions["repository"]
    with app.app_context():
        conversation = db.session.get(Conversation, conversation_id)
        for content in ("first", "second", "NRIC S1234567D"):
            repository.add_message(conversation, "user", content)
        draft = repository.get_or_create_draft(conversation)
        draft.course = app.extensions["catalogue"].course_name
        draft.course_fee = app.extensions["catalogue"].course_fee
        draft.full_name = "Test Student"
        draft.nric = "S1234567D"
        db.session.commit()

        memory, course, current_draft, validation, intakes = _tools_for(
            app, conversation_id, context_limit=2
        )
        assert all(issubclass(tool.args_schema, BaseModel) for tool in (memory, course, current_draft, validation, intakes))
        assert all(issubclass(tool.result_schema, BaseModel) for tool in (memory, course, current_draft, validation, intakes))

        memory_result = memory.run(limit=100)
        assert memory_result.authorised is True
        assert [item.content for item in memory_result.messages] == [
            "second",
            "NRIC S*******D",
        ]
        course_result = course.run(topic="enrollment")
        assert course_result.approved_information["course"]["fee"]["amount"] == 600
        draft_result = current_draft.run()
        assert draft_result.safe_values["nric"] == "S*******D"
        assert "nric" in draft_result.saved_fields
        validation_result = validation.run(
            fields={
                "nric": "S1234567D",
                "email": "test.student@example.com",
                "mobile_number": "91234567",
                "payment_method": "SkillsFuture and PayNow",
                "skillsfuture_amount": "400",
                "paynow_amount": "200",
            }
        )
        assert validation_result.payment_valid is True
        assert validation_result.safe_normalized_values["nric"] == "S*******D"
        rendered = " ".join(
            (
                memory_result.model_dump_json(),
                draft_result.model_dump_json(),
                validation_result.model_dump_json(),
            )
        )
        assert "S1234567D" not in rendered


def test_conversation_memory_tool_cannot_bypass_ownership(
    app, client, conversation_id
):
    with app.app_context():
        repository = ConversationRepository()
        other = User(
            full_name="Other Participant",
            email="tool-other@example.com",
            password_hash="not-used",
        )
        db.session.add(other)
        db.session.commit()
        tool = ConversationMemoryTool(
            repository, conversation_id, other.id, maximum_limit=20
        )
        result = tool.run()
        assert result.authorised is False
        assert result.messages == []


def test_enrollment_tools_are_attached_and_invoked_by_runtime_task(
    monkeypatch, app, client, conversation_id
):
    recorded = {}

    class RecordingCrew:
        def __init__(self, *, agents, tasks, **kwargs):
            recorded["agent"] = agents[0]
            recorded["task"] = tasks[0]

        def kickoff(self):
            return SimpleNamespace(
                pydantic=EnrollmentExtraction(), json_dict=None, raw=""
            )

    monkeypatch.setattr("backend.services.crewai_service.Crew", RecordingCrew)
    service = CrewAIService(api_key="synthetic-test-key", model="gpt-5-mini")
    with app.app_context():
        tools = _tools_for(app, conversation_id)
        service.extract_enrollment("Please enrol me", "", {}, tools=tools)

    assert isinstance(recorded["task"], Task)
    assert [tool.name for tool in recorded["task"].tools] == [
        "authorised_conversation_memory",
        "approved_course_information",
        "current_enrollment_draft",
        "deterministic_enrollment_validation",
        "course_intake_availability",
    ]
    assert [tool.name for tool in recorded["agent"].tools] == [
        tool.name for tool in recorded["task"].tools
    ]
    assert all(tool.current_usage_count >= 1 for tool in tools)
