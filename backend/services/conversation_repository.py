"""Persistence helpers for conversation state."""

from datetime import timedelta, timezone

from backend.extensions import db
from backend.models import Conversation, EnrollmentDraft, Message, utc_now


class ConversationRepository:
    def create(self, user_id=None):
        conversation = Conversation(user_id=user_id)
        db.session.add(conversation)
        db.session.commit()
        return conversation

    def get(self, conversation_id):
        return db.session.get(Conversation, conversation_id)

    def get_for_user(self, conversation_id, user_id):
        return Conversation.query.filter_by(
            id=conversation_id,
            user_id=user_id,
        ).one_or_none()

    def list_for_user(self, user_id, limit=50):
        return (
            Conversation.query.filter(
                Conversation.user_id == user_id,
                Conversation.status != "archived",
            )
            .order_by(Conversation.updated_at.desc())
            .limit(limit)
            .all()
        )

    @staticmethod
    def last_message(conversation):
        return (
            Message.query.filter_by(conversation_id=conversation.id)
            .order_by(Message.created_at.desc())
            .first()
        )

    def add_message(
        self,
        conversation,
        role,
        content,
        *,
        agent_name=None,
        detected_intent=None,
        classification_confidence=None,
        image_url=None,
        image_alt=None,
        image_status=None,
    ):
        created_at = utc_now()
        previous_times = [
            _as_utc(message.created_at)
            for message in conversation.messages
            if message.created_at is not None
        ]
        if previous_times and created_at <= max(previous_times):
            created_at = max(previous_times) + timedelta(microseconds=1)
        conversation.updated_at = created_at
        message = Message(
            conversation=conversation,
            role=role,
            content=content,
            agent_name=agent_name,
            detected_intent=detected_intent,
            classification_confidence=classification_confidence,
            image_url=image_url,
            image_alt=image_alt,
            image_status=image_status,
            created_at=created_at,
        )
        db.session.add(message)
        db.session.flush()
        return message

    def recent_messages(self, conversation, limit):
        recent = (
            Message.query.filter_by(conversation_id=conversation.id)
            .order_by(Message.created_at.desc())
            .limit(limit)
            .all()
        )
        return list(reversed(recent))

    def all_messages(self, conversation):
        return (
            Message.query.filter_by(conversation_id=conversation.id)
            .order_by(Message.created_at.asc())
            .all()
        )

    def get_or_create_draft(self, conversation):
        draft = conversation.enrollment_draft
        if draft is not None and draft.status == "discarded":
            archive_discarded_draft(conversation, draft)
            draft = None
        if draft is None:
            draft = EnrollmentDraft(conversation=conversation)
            db.session.add(draft)
            db.session.flush()
        return draft

    @staticmethod
    def update_draft(draft, values):
        for field, value in values.items():
            setattr(draft, field, value)
        draft.updated_at = utc_now()


def archive_discarded_draft(conversation, draft):
    """Move a discarded draft to an owner-scoped archive conversation."""
    if draft is None or draft.status != "discarded":
        return None
    archive = Conversation(
        user_id=conversation.user_id,
        status="archived",
        active_intent=None,
        pending_followup=None,
    )
    db.session.add(archive)
    db.session.flush()
    draft.conversation = archive
    db.session.flush()
    db.session.expire(conversation, ["enrollment_draft"])
    return archive


def _as_utc(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


__all__ = ["ConversationRepository", "archive_discarded_draft"]
