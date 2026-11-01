"""Memory-aware lead reminders with approved-template fallback (REQ-A10)."""

from datetime import timedelta

import pytest

from backend.agents.schemas import (
    AIConfigurationError,
    AIServiceError,
    FollowupMessage,
    MemoryFact,
)
from backend.app import create_app
from backend.extensions import db
from backend.models import Conversation
from backend.models.communications import OutboundMessage
from backend.models.operations import Lead
from backend.services.enquiry.followup_composer import FollowupComposer
from backend.services.enquiry.memory_service import MemoryService
from backend.tests.conftest import authenticated_client


TOKEN = "synthetic-automation-token"


class ScriptedFollowupAI:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def compose_followup(self, **prompt):
        self.calls.append(prompt)
        if isinstance(self.result, Exception):
            raise self.result
        return FollowupMessage(message=self.result)


@pytest.fixture
def ctx(app):
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    with app.app_context():
        conversation = db.session.get(Conversation, conversation_id)
        kb = app.extensions["knowledge_base"]

        class Context:
            def remember(self):
                app.extensions["repository"].add_message(
                    conversation, "user", "I'm Lis, keen on the dental assisting course but need childcare"
                )
                MemoryService().apply(
                    conversation,
                    [
                        MemoryFact(key="preferred_name", value="Lis"),
                        MemoryFact(key="course_interest", value="Basic Certificate in Dental Assisting"),
                        MemoryFact(key="constraint", value="arranging childcare"),
                    ],
                )
                db.session.commit()

            def composer(self, result):
                self.ai = ScriptedFollowupAI(result)
                return FollowupComposer(kb, ai_factory=lambda: self.ai)

        context = Context()
        context.kb = kb
        context.conversation = conversation
        yield context


PERSONAL = (
    "Hi Lis! Hope the childcare arrangements are going well 😊 Still keen on the "
    "Basic Certificate in Dental Assisting? Just reply if you have questions. "
    "Reply STOP to opt out of reminders."
)


def test_personalised_reminder_is_used_when_it_passes_the_checks(ctx):
    ctx.remember()
    composer = ctx.composer(PERSONAL)
    assert composer.compose(ctx.conversation, 0, 3) == PERSONAL
    prompt = ctx.ai.calls[0]
    assert prompt["template"] == ctx.kb.followup_template(0)
    assert "Preferred name: Lis" in prompt["profile"]
    assert "childcare" in prompt["conversation"]
    assert prompt["final"] is False


def test_final_step_is_flagged(ctx):
    ctx.remember()
    composer = ctx.composer(PERSONAL)
    composer.compose(ctx.conversation, 2, 3)
    assert ctx.ai.calls[0]["final"] is True


def test_nothing_to_personalise_sends_the_template_without_a_model_call(ctx):
    composer = ctx.composer(PERSONAL)
    assert composer.compose(ctx.conversation, 1, 3) == ctx.kb.followup_template(1)
    assert ctx.ai.calls == []


@pytest.mark.parametrize(
    "result",
    [
        # Invented offer.
        "Hi Lis! Enrol this week for S$399 instead of the usual fee. Reply STOP to opt out.",
        # Dropped the opt-out.
        "Hi Lis! Still keen on the dental assisting course? Just reply here.",
        # Internal detail.
        "Hi Lis! My database says you like dental assisting. Reply STOP to opt out.",
        # Valid output, but too long for a reminder (600-character cap).
        "Hi Lis! " + "Still keen? " * 50 + "Reply STOP to opt out.",
        AIServiceError("timeout"),
        AIConfigurationError("no key"),
    ],
)
def test_unsafe_or_failed_personalisation_falls_back_to_the_template(ctx, result):
    ctx.remember()
    composer = ctx.composer(result)
    assert composer.compose(ctx.conversation, 0, 3) == ctx.kb.followup_template(0)


def test_unconfigured_model_factory_falls_back_to_the_template(ctx):
    ctx.remember()

    def unconfigured():
        raise AIConfigurationError("The AI service is not configured.")

    composer = FollowupComposer(ctx.kb, ai_factory=unconfigured)
    assert composer.compose(ctx.conversation, 0, 3) == ctx.kb.followup_template(0)


def test_without_a_model_the_template_is_used(ctx):
    ctx.remember()
    assert FollowupComposer(ctx.kb).compose(ctx.conversation, 2, 3) == ctx.kb.followup_template(2)


def test_scheduler_sends_the_personalised_reminder(tmp_path, fake_ai):
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "followup-composer-secret",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / 'followups.db').as_posix()}",
            "AUTOMATION_API_TOKEN": TOKEN,
            "FOLLOWUP_HOURS": (48, 96, 144),
        },
        ai_service=fake_ai,
    )
    personal = "Hi again! Any questions about the course fee of S$600 nett? Reply STOP to opt out."
    fake_ai.compose_followup = lambda **prompt: FollowupMessage(message=personal)
    client = app.test_client()
    client.post(
        "/api/development/inbound",
        json={"id": "wa-1", "from": "6580000001", "body": "How much is the dental assisting course?"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    with app.app_context():
        app.extensions["process_inbox"]()
        lead = Lead.query.one()
        app.extensions["operations"].run_followups(now=lead.last_contact_at + timedelta(hours=49))
        body = (
            OutboundMessage.query.filter(OutboundMessage.purpose.like("lead_followup_%")).one().body
        )
    assert body == personal
