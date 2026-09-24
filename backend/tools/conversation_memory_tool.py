"""Authorised, privacy-safe conversation memory for the Enrollment Agent."""

from datetime import timezone
from typing import Literal

from crewai.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr

from backend.services.privacy import mask_nrics_in_text


class ConversationMemoryInput(BaseModel):
    limit: int | None = Field(default=None, ge=1, le=100)


class ConversationMemoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    timestamp: str


class ConversationMemoryOutput(BaseModel):
    authorised: bool
    conversation_id: str
    active_intent: str | None = None
    pending_followup: str | None = None
    enrollment_status: str | None = None
    messages: list[ConversationMemoryMessage] = Field(default_factory=list)


class ConversationMemoryTool(BaseTool):
    name: str = "authorised_conversation_memory"
    description: str = (
        "Retrieve recent chronological messages and workflow state only for the "
        "current authenticated participant's conversation. NRIC/FIN values are masked."
    )
    args_schema: type[BaseModel] = ConversationMemoryInput
    result_schema: type[BaseModel] = ConversationMemoryOutput

    _repository = PrivateAttr()
    _conversation_id: str = PrivateAttr()
    _user_id: str = PrivateAttr()
    _maximum_limit: int = PrivateAttr()

    def __init__(self, repository, conversation_id, user_id, maximum_limit):
        super().__init__()
        self._repository = repository
        self._conversation_id = conversation_id
        self._user_id = user_id
        self._maximum_limit = max(1, int(maximum_limit))

    def _run(self, limit=None):
        conversation = self._repository.get_for_user(
            self._conversation_id, self._user_id
        )
        if conversation is None:
            return ConversationMemoryOutput(
                authorised=False,
                conversation_id=self._conversation_id,
            )
        requested = self._maximum_limit if limit is None else int(limit)
        messages = self._repository.recent_messages(
            conversation, min(requested, self._maximum_limit)
        )
        return ConversationMemoryOutput(
            authorised=True,
            conversation_id=conversation.id,
            active_intent=conversation.active_intent,
            pending_followup=conversation.pending_followup,
            enrollment_status=(
                conversation.enrollment_draft.status
                if conversation.enrollment_draft
                else None
            ),
            messages=[
                ConversationMemoryMessage(
                    role=message.role,
                    content=mask_nrics_in_text(message.content),
                    timestamp=_iso_timestamp(message.created_at),
                )
                for message in messages
                if message.role in {"user", "assistant"}
            ],
        )


def _iso_timestamp(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


__all__ = [
    "ConversationMemoryInput",
    "ConversationMemoryOutput",
    "ConversationMemoryTool",
]
