"""Lead reminders personalised from participant memory (REQ-A10).

The approved template for the step is always the fallback: it is sent unchanged
when there is nothing to personalise, the model is unavailable, or the
personalised text fails the fact guard or drops the STOP opt-out.
"""

from backend.agents.schemas import AIServiceError, EnquiryTurn
from backend.models import Message
from backend.services.enquiry import fact_guard
from backend.services.enquiry.fact_guard import GuardContext
from backend.services.enquiry.memory_service import MemoryService


MAX_REMINDER_CHARS = 600
RECENT_MESSAGE_LIMIT = 10


class FollowupComposer:
    def __init__(self, knowledge_base, ai_factory=None, memory_service=None):
        """``ai_factory`` returns the AI service when called, or raises when unconfigured."""
        self.kb = knowledge_base
        self.ai_factory = ai_factory
        self.memory = memory_service or MemoryService()

    def compose(self, conversation, step, total_steps):
        template = self.kb.followup_template(step)
        if self.ai_factory is None:
            return template
        recent = list(
            reversed(
                Message.query.filter_by(conversation_id=conversation.id)
                .order_by(Message.sequence.desc())
                .limit(RECENT_MESSAGE_LIMIT)
                .all()
            )
        )
        context = self.memory.build_context(conversation, recent, "")
        if not context.profile and not context.conversation:
            return template
        try:
            draft = self.ai_factory().compose_followup(
                template=template,
                profile=context.profile_block(),
                conversation=context.conversation_block(),
                approved_content=self.kb.render(),
                final=step >= total_steps - 1,
            )
        except AIServiceError:
            return template
        text = draft.message.strip()
        if not self._acceptable(text, context):
            return template
        return text

    def _acceptable(self, text, context):
        if "STOP" not in text or len(text) > MAX_REMINDER_CHARS:
            return False
        guard_context = GuardContext(
            allowed=self.kb.fact_allow_list(),
            kb_ids=self.kb.entry_ids(),
            participant=context.participant_facts,
            course_fees=tuple(self.kb.course_fees()),
        )
        # Checked like a conversational reply: approved facts only, no internals.
        reminder = EnquiryTurn(reply=text, response_type="small_talk")
        return not fact_guard.check(reminder, guard_context)


__all__ = ["FollowupComposer"]
