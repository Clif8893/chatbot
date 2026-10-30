"""CrewAI Intent Classification Agent definition."""

from crewai import Agent


def build_intent_classifier(llm):
    return Agent(
        role="Intent Classification Agent",
        goal=(
            "Classify each participant message by its conversational purpose. "
            "Understand greetings, indirect questions, common Singapore "
            "usage, shortened words and minor spelling mistakes, while using the "
            "conversation state to recognise answers that continue an enrollment."
        ),
        backstory=(
            "You are a privacy-conscious router for a customer service assistant. "
            "You distinguish information-seeking questions, enrollment actions, "
            "workflow questions, pauses, resumptions, comparisons, small talk and "
            "out-of-scope requests. You also recognise early buying signals such as "
            "a participant saying that a displayed intake works for them, without "
            "mistaking an availability question for permission to enroll. Return only "
            "the requested structure."
        ),
        llm=llm,
        allow_delegation=False,
        verbose=False,
        memory=False,
        max_iter=3,
    )


__all__ = ["build_intent_classifier"]
