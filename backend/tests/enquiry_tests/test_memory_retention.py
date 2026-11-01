"""Retention removes idle Module A memory (decision D6, REQ-A08.8)."""

import json
from datetime import timedelta

from backend.app import create_app
from backend.extensions import db
from backend.models import Conversation, utc_now
from backend.models.memory import ConversationMemory, ParticipantMemory
from backend.services.retention_service import RetentionService


def _app(tmp_path, fake_ai):
    return create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "retention-test-secret",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / 'retention.db').as_posix()}",
            # The job also sweeps private storage, so keep it inside the test folder.
            "STORAGE_DIR": str(tmp_path / "storage"),
            "RETENTION_DAYS": 365,
        },
        ai_service=fake_ai,
    )


def test_idle_memory_and_expired_conversation_memory_are_removed(tmp_path, fake_ai):
    app = _app(tmp_path, fake_ai)
    old = utc_now() - timedelta(days=400)
    with app.app_context():
        expired = Conversation(created_at=old, updated_at=old)
        current = Conversation()
        db.session.add_all([expired, current])
        db.session.flush()
        db.session.add_all(
            [
                ConversationMemory(conversation_id=expired.id, summary="Asked about fees.", updated_at=old),
                ConversationMemory(conversation_id=current.id, summary="Asked about dates."),
                ParticipantMemory(subject_type="user", subject_id="idle", facts=json.dumps({}), updated_at=old),
                ParticipantMemory(subject_type="user", subject_id="active", facts=json.dumps({})),
            ]
        )
        db.session.commit()
        expired_id, current_id = expired.id, current.id

        preview = RetentionService().run(dry_run=True)
        assert preview["expired_memory_profiles"] == 1
        assert ParticipantMemory.query.count() == 2

        result = RetentionService().run(dry_run=False)
        assert result["expired_memory_profiles"] == 1
        assert [row.subject_id for row in ParticipantMemory.query.all()] == ["active"]
        assert db.session.get(Conversation, expired_id) is None
        assert db.session.get(ConversationMemory, expired_id) is None
        assert db.session.get(ConversationMemory, current_id) is not None
