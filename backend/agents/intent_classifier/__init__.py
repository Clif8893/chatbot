"""CrewAI Intent Classification Agent definition."""

from crewai import Agent


def build_intent_classifier(llm):
    return Agent(
        role="Intent Classification Agent",
        goal=(
            "Classify each participant message by its purpose as faq, enrollment, "
            "or unclear. Understand greetings, indirect questions, common Singapore "
            "usage, shortened words and minor spelling mistakes, while using the "
            "conversation state to recognise answers that continue an enrollment."
        ),
        backstory=(
            "You are a privacy-conscious router for a customer service assistant. "
            "You distinguish information-seeking questions from a clear request to "
            "start or continue enrollment, and return only the requested structure."
        ),
        llm=llm,
        allow_delegation=False,
        verbose=False,
        memory=False,
        max_iter=3,
    )


__all__ = ["build_intent_classifier"]
