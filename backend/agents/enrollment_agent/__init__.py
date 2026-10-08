"""CrewAI Enrollment Agent definition."""

from crewai import Agent


def build_enrollment_agent(llm, tools=None):
    return Agent(
        role="Enrollment Agent",
        goal=(
            "Extract only explicitly supplied course-date, identity, contact, "
            "correction, and confirmation details "
            "from natural enrolment conversation without deciding validity or repeating "
            "previously saved values."
        ),
        backstory=(
            "You support a friendly course enrollment conversation and understand "
            "informal wording and minor spelling mistakes. Deterministic Python services "
            "perform validation and persistence after your extraction. You never add "
            "a value that the participant did not provide."
        ),
        llm=llm,
        tools=list(tools or []),
        allow_delegation=False,
        verbose=False,
        memory=False,
        max_iter=3,
    )


__all__ = ["build_enrollment_agent"]
