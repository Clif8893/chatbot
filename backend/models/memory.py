"""Module A participant memory: a cross-conversation profile and per-chat state."""

from backend.extensions import db
from backend.models import new_id, utc_now
from backend.services.security import EncryptedText


class ParticipantMemory(db.Model):
    """Whitelisted facts remembered about one participant across conversations.

    The subject is a participant account (``user``) or a hashed channel identity
    (``contact``). Facts are encrypted JSON: ``{key: [{value, conversation_id,
    recorded_at}]}``.
    """

    __tablename__ = "participant_memories"
    __table_args__ = (
        db.UniqueConstraint(
            "subject_type", "subject_id", name="uq_participant_memory_subject"
        ),
    )

    id = db.Column(db.String(36), primary_key=True, default=new_id)
    subject_type = db.Column(db.String(16), nullable=False)
    subject_id = db.Column(db.String(64), nullable=False)
    facts = db.Column(EncryptedText(), nullable=False, default="{}")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class ConversationMemory(db.Model):
    """Rolling summary and conduct count for one conversation."""

    __tablename__ = "conversation_memories"

    conversation_id = db.Column(
        db.String(36), db.ForeignKey("conversations.id"), primary_key=True
    )
    summary = db.Column(EncryptedText(), nullable=True)
    # Sequence number of the last message the summary covers.
    through_sequence = db.Column(db.Integer, nullable=False, default=0)
    # Messages up to this sequence were forgotten at the participant's request
    # and are never used as model context again.
    forgotten_through_sequence = db.Column(db.Integer, nullable=False, default=0)
    abuse_strikes = db.Column(db.Integer, nullable=False, default=0)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


__all__ = ["ConversationMemory", "ParticipantMemory"]
