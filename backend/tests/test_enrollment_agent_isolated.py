"""Offline, isolated evidence for the CrewAI Enrollment Agent.

These tests exercise CrewAI's real Agent -> Task -> Crew -> kickoff path with a
scripted BaseLLM. They do not start Flask, open the application database, load
an environment file, or contact an external provider.
"""

import json
import os
import socket
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import requests
from crewai import Agent, Crew, Process, Task
from crewai.llms.base_llm import BaseLLM
from crewai.utilities.string_utils import sanitize_tool_name
from pydantic import PrivateAttr

from backend.agents.enrollment_agent import build_enrollment_agent
from backend.agents.schemas import EnrollmentExtraction
from backend.services.course_catalogue import CourseCatalogue
from backend.tools.course_information_tool import CourseInformationTool


os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")


class FakeLLM(BaseLLM):
    """CrewAI 1.15-compatible scripted LLM that records every prompt."""

    _replies: list[str] = PrivateAttr(default_factory=list)
    _calls: list[Any] = PrivateAttr(default_factory=list)

    def __init__(self, replies: list[str]):
        super().__init__(model="offline-fake-llm")
        self._replies = list(replies)
        self._calls = []

    def call(
        self,
        messages,
        tools=None,
        callbacks=None,
        available_functions=None,
        from_task=None,
        from_agent=None,
        response_model=None,
        **kwargs,
    ):
        self._calls.append(messages)
        if not self._replies:
            raise AssertionError("FakeLLM received an unexpected extra call.")
        return self._replies.pop(0)

    def prompt(self, index: int) -> str:
        return str(self._calls[index])

    @property
    def call_count(self) -> int:
        return len(self._calls)

    def supports_function_calling(self) -> bool:
        return False

    def supports_stop_words(self) -> bool:
        return True

    def get_context_window_size(self) -> int:
        return 8192


def tool_call(tool, **arguments) -> str:
    """Return a scripted CrewAI ReAct action for one real tool."""

    return (
        "Thought: I need approved local course information.\n"
        f"Action: {sanitize_tool_name(tool.name)}\n"
        f"Action Input: {json.dumps(arguments)}"
    )


def final_answer(payload: dict[str, Any]) -> str:
    """Return a scripted CrewAI final answer containing structured JSON."""

    return (
        "Thought: I now know the final answer\n"
        f"Final Answer: {json.dumps(payload)}"
    )


@pytest.fixture(autouse=True)
def block_external_network(monkeypatch):
    """Fail immediately if an isolated test attempts any external connection."""

    original_socket_connect = socket.socket.connect

    def blocked(*args, **kwargs):
        raise AssertionError("External network access is forbidden in isolated tests.")

    def local_event_loop_only(sock, address):
        host = address[0] if isinstance(address, tuple) and address else None
        if host in {"127.0.0.1", "::1"}:
            return original_socket_connect(sock, address)
        return blocked(sock, address)

    monkeypatch.setenv("CREWAI_DISABLE_TELEMETRY", "true")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", local_event_loop_only)
    monkeypatch.setattr(requests.sessions.Session, "request", blocked)
    monkeypatch.setattr(httpx.Client, "request", blocked)
    monkeypatch.setattr(httpx.AsyncClient, "request", blocked)


def test_course_information_tool_returns_approved_local_information():
    catalogue = CourseCatalogue()
    tool = CourseInformationTool(catalogue)

    result = tool.run(topic="enrollment")

    assert result.source == "backend/data/course_information.json"
    assert result.approved_information["course"]["name"] == catalogue.course["name"]
    assert result.matched_faqs
    assert not hasattr(tool, "_repository")


def test_course_information_tool_handles_unknown_and_unsupported_topics_safely():
    tool = CourseInformationTool(CourseCatalogue())

    unknown = tool.run(topic="topic-that-does-not-exist")
    unsupported = tool.run(topic="private participant records and payment evidence")

    for result in (unknown, unsupported):
        assert result.source == "backend/data/course_information.json"
        assert result.matched_faqs == []
        assert result.approved_information["course"]
        assert set(result.model_dump()) == {
            "source",
            "approved_information",
            "matched_faqs",
            "staff_confirmation_required",
        }


def test_enrollment_agent_wiring_uses_the_read_only_production_tool():
    tool = CourseInformationTool(CourseCatalogue())
    agent = build_enrollment_agent(FakeLLM([]), [tool])

    assert isinstance(agent, Agent)
    assert agent.role == "Enrollment Agent"
    assert "Extract only explicitly supplied" in agent.goal
    assert agent.allow_delegation is False
    # CrewAI 1.15.21 normalises a disabled Agent memory flag to None. The Crew
    # below retains the explicit memory=False runtime boundary.
    assert agent.memory in {False, None}
    assert [attached.name for attached in agent.tools] == [
        "approved_course_information"
    ]
    assert not hasattr(tool, "_repository")


def test_fake_llm_makes_real_crew_select_and_execute_course_tool_once():
    catalogue = CourseCatalogue()
    course_name = catalogue.course["name"]
    tool = CourseInformationTool(catalogue)
    extraction = {
        "extracted_fields": {"course": course_name},
        "missing_fields": [],
        "next_question": None,
        "ready_for_confirmation": False,
        "confirmation": "none",
        "correction_requested": False,
    }
    fake_llm = FakeLLM(
        [
            tool_call(tool, topic="enrollment"),
            final_answer(extraction),
        ]
    )
    agent = build_enrollment_agent(fake_llm, [tool])
    task = Task(
        description=(
            "A fictional participant says: 'I would like to enrol in the "
            f"{course_name}.' Use the approved course-information tool, then "
            "extract only fields explicitly supplied in that sentence."
        ),
        expected_output=(
            "Valid EnrollmentExtraction JSON with no invented identity, contact, "
            "intake or payment values."
        ),
        agent=agent,
        tools=[tool],
        output_pydantic=EnrollmentExtraction,
    )
    crew = Crew(
        agents=[agent],
        tasks=[task],
        process=Process.sequential,
        verbose=False,
        memory=False,
        share_crew=False,
        tracing=False,
    )

    assert len(crew.agents) == 1
    assert len(crew.tasks) == 1
    assert crew.tasks[0].agent is crew.agents[0]
    assert [attached.name for attached in crew.tasks[0].tools] == [tool.name]

    with patch.object(tool, "_run", wraps=tool._run) as tool_run:
        result = crew.kickoff()

    assert tool_run.call_count == 1
    assert fake_llm.call_count == 2
    assert "backend/data/course_information.json" in fake_llm.prompt(1)
    assert "approved_information" in fake_llm.prompt(1)
    assert isinstance(result.pydantic, EnrollmentExtraction)
    supplied = result.pydantic.extracted_fields.model_dump(exclude_none=True)
    assert supplied == {"course": course_name}
    assert result.pydantic.ready_for_confirmation is False
