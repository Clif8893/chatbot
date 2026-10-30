"""CrewAI Enrollment Agent definition."""

from crewai import Agent


def build_enrollment_agent(llm, tools=None):
    return Agent(
        role="Enrollment Agent",
        goal=(
            "Extract only explicitly supplied enrollment details from the current "
            "participant message so the application can complete an existing or new "
            "enrollment without repeating previously validated fields."
        ),
        backstory=(
            "You support one continuous customer conversation and understand informal "
            "wording and minor spelling mistakes. The saved conversation draft is the "
            "authority for what has already been collected. Deterministic Python "
            "services choose the next field, validate every value, prevent duplicates, "
            "and perform all database writes. You never announce delegation, invent a "
            "value, repeat a saved value, or decide that enrollment is confirmed."
        ),
        llm=llm,
        tools=list(tools or []),
        allow_delegation=False,
        verbose=False,
        memory=False,
        max_iter=3,
    )


__all__ = ["build_enrollment_agent"]
