"""Small additive migrations for databases created before migration tooling existed."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import inspect, text

from backend.extensions import db


ENROLLMENT_DRAFT_COLUMNS = {
    "friendly_reference": "VARCHAR(32)",
    "course_id": "VARCHAR(64)",
    "course_fee": "NUMERIC(10, 2)",
    "intake_id": "VARCHAR(64)",
    "intake_start_date": "DATE",
    "intake_end_date": "DATE",
    "intake_is_demo": "BOOLEAN",
    "allocated_intake_id": "VARCHAR(64)",
    "intake_allocated_at": "TIMESTAMP",
    "mobile_number": "VARCHAR(32)",
    "payment_method": "VARCHAR(32)",
    "skillsfuture_amount": "NUMERIC(10, 2)",
    "paynow_amount": "NUMERIC(10, 2)",
    "validation_status": "VARCHAR(32) DEFAULT 'pending' NOT NULL",
    "pending_corrections": "JSON DEFAULT '[]' NOT NULL",
    "validation_errors": "JSON DEFAULT '{}' NOT NULL",
    "cancellation_requested_at": "TIMESTAMP",
    "cancelled_at": "TIMESTAMP",
    "cancellation_reason": "TEXT",
    "reschedule_requested_at": "TIMESTAMP",
    "rescheduled_at": "TIMESTAMP",
    "previous_intake_id": "VARCHAR(64)",
    "refund_review_required": "BOOLEAN DEFAULT FALSE NOT NULL",
}

COURSE_DATE_COLUMNS = {
    "course_id": "VARCHAR(64) DEFAULT 'dental-assisting-basic' NOT NULL",
    "start_time": "TIME DEFAULT '09:30:00' NOT NULL",
    "end_time": "TIME DEFAULT '17:30:00' NOT NULL",
    "participant_count": "INTEGER DEFAULT 0 NOT NULL",
    "is_demo": "BOOLEAN DEFAULT FALSE NOT NULL",
}

CONVERSATION_COLUMNS = {
    "user_id": "VARCHAR(36) REFERENCES users(id)",
    "pending_followup": "VARCHAR(32)",
    "pending_action": "VARCHAR(64)",
    "pending_enrollment_id": "VARCHAR(36)",
    "pending_target_intake_id": "VARCHAR(64)",
    "pending_source_intake_id": "VARCHAR(64)",
    "pending_intake_options": "JSON",
    "pending_change_data": "JSON",
    "pending_action_requested_at": "TIMESTAMP",
}

MESSAGE_COLUMNS = {
    "image_url": "VARCHAR(500)",
    "image_alt": "VARCHAR(500)",
    "image_status": "VARCHAR(64)",
    "sequence": "INTEGER DEFAULT 0 NOT NULL",
    "outcome_status": "VARCHAR(32)",
    "error_category": "VARCHAR(64)",
}

MASTER_INVOICE_EXPORT_COLUMNS = {
    "export_key": "VARCHAR(64)",
    "invoice_number": "VARCHAR(64)",
    "invoice_sequence": "INTEGER",
    "invoice_date": "DATE",
    "workbook_row": "INTEGER",
    "export_version": "INTEGER DEFAULT 0 NOT NULL",
    "content_hash": "VARCHAR(64)",
    "last_attempted_at": "TIMESTAMP",
}

OUTBOUND_MESSAGE_COLUMNS = {
    "enrollment_id": "VARCHAR(36) REFERENCES enrollment_drafts(id)",
    "invoice_generation_id": "VARCHAR(36) REFERENCES invoice_generations(id)",
    "html_body": "TEXT",
    "email_type": "VARCHAR(64)",
    "provider": "VARCHAR(32)",
    "invoice_number": "VARCHAR(64)",
    "idempotency_key": "VARCHAR(255)",
    "document_sha256": "VARCHAR(64)",
    "resend_count": "INTEGER DEFAULT 0 NOT NULL",
    "failure_category": "VARCHAR(64)",
    "queued_at": "TIMESTAMP",
    "last_attempt_at": "TIMESTAMP",
    "updated_at": "TIMESTAMP",
}


def apply_additive_migrations():
    """Add new nullable enrollment fields without replacing existing data."""
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())

    with db.engine.begin() as connection:
        if "enrollment_drafts" in tables:
            existing = {
                column["name"]
                for column in inspector.get_columns("enrollment_drafts")
            }
            for name, column_type in ENROLLMENT_DRAFT_COLUMNS.items():
                if name not in existing:
                    connection.execute(
                        text(
                            f"ALTER TABLE enrollment_drafts ADD COLUMN "
                            f"{name} {column_type}"
                        )
                    )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_enrollment_drafts_intake_id "
                    "ON enrollment_drafts (intake_id)"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_enrollment_drafts_course_id "
                    "ON enrollment_drafts (course_id)"
                )
            )
            connection.execute(
                text(
                    "UPDATE enrollment_drafts SET course_id = CASE "
                    "WHEN lower(course) LIKE '%infection control%' "
                    "THEN 'dental-infection-control' "
                    "WHEN course IS NOT NULL THEN 'dental-assisting-basic' "
                    "ELSE course_id END WHERE course_id IS NULL"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_enrollment_drafts_allocated_intake_id "
                    "ON enrollment_drafts (allocated_intake_id)"
                )
            )
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "ix_enrollment_drafts_friendly_reference "
                    "ON enrollment_drafts (friendly_reference)"
                )
            )

        if "conversations" in tables:
            existing = {
                column["name"] for column in inspector.get_columns("conversations")
            }
            for name, column_type in CONVERSATION_COLUMNS.items():
                if name not in existing:
                    connection.execute(
                        text(
                            f"ALTER TABLE conversations ADD COLUMN "
                            f"{name} {column_type}"
                        )
                    )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_conversations_user_id "
                    "ON conversations (user_id)"
                )
            )

        if "messages" in tables:
            existing = {
                column["name"] for column in inspector.get_columns("messages")
            }
            for name, column_type in MESSAGE_COLUMNS.items():
                if name not in existing:
                    connection.execute(
                        text(
                            f"ALTER TABLE messages ADD COLUMN "
                            f"{name} {column_type}"
                        )
                    )
            connection.execute(
                text(
                    "WITH ordered AS ("
                    "SELECT id, ROW_NUMBER() OVER (PARTITION BY conversation_id "
                    "ORDER BY created_at, id) AS ordered_sequence FROM messages"
                    ") UPDATE messages SET sequence = (SELECT ordered_sequence "
                    "FROM ordered WHERE ordered.id = messages.id) "
                    "WHERE sequence IS NULL OR sequence = 0"
                )
            )
            connection.execute(
                text(
                    "UPDATE messages SET outcome_status = 'historical' "
                    "WHERE outcome_status IS NULL"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_messages_conversation_sequence "
                    "ON messages (conversation_id, sequence)"
                )
            )

        if "course_dates" in tables:
            existing = {
                column["name"] for column in inspector.get_columns("course_dates")
            }
            for name, column_type in COURSE_DATE_COLUMNS.items():
                if name not in existing:
                    connection.execute(
                        text(f"ALTER TABLE course_dates ADD COLUMN {name} {column_type}")
                    )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_course_dates_course_id "
                    "ON course_dates (course_id)"
                )
            )
            connection.execute(
                text(
                    "UPDATE course_dates SET course_id = 'dental-assisting-basic' "
                    "WHERE course_id = 'basic-dental-assisting'"
                )
            )
            if "enrollment_drafts" in tables:
                active_statuses = (
                    "'confirmed','enrolled','invoice_sent','awaiting_payment',"
                    "'paid','receipt_issued'"
                )
                connection.execute(
                    text(
                        "UPDATE enrollment_drafts SET allocated_intake_id = ("
                        "SELECT course_dates.id FROM course_dates "
                        "WHERE course_dates.date = enrollment_drafts.preferred_intake_date"
                        ") WHERE allocated_intake_id IS NULL "
                        "AND preferred_intake_date IS NOT NULL "
                        f"AND status IN ({active_statuses}) "
                        "AND EXISTS (SELECT 1 FROM course_dates WHERE "
                        "course_dates.date = enrollment_drafts.preferred_intake_date)"
                    )
                )
                connection.execute(
                    text(
                        "UPDATE enrollment_drafts SET intake_allocated_at = confirmed_at "
                        "WHERE allocated_intake_id IS NOT NULL "
                        "AND intake_allocated_at IS NULL"
                    )
                )
                count_query = (
                    "SELECT COUNT(*) FROM enrollment_drafts "
                    "WHERE enrollment_drafts.preferred_intake_date = course_dates.date "
                    f"AND enrollment_drafts.status IN ({active_statuses})"
                )
                connection.execute(
                    text(
                        f"UPDATE course_dates SET participant_count = ({count_query}) "
                        f"WHERE participant_count < ({count_query})"
                    )
                )

        if "master_invoice_exports" in tables:
            existing = {
                column["name"]
                for column in inspector.get_columns("master_invoice_exports")
            }
            for name, column_type in MASTER_INVOICE_EXPORT_COLUMNS.items():
                if name not in existing:
                    connection.execute(
                        text(
                            "ALTER TABLE master_invoice_exports ADD COLUMN "
                            f"{name} {column_type}"
                        )
                    )
            connection.execute(
                text(
                    "UPDATE master_invoice_exports SET export_key = enrollment_id "
                    "WHERE export_key IS NULL"
                )
            )
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "ix_master_invoice_exports_export_key "
                    "ON master_invoice_exports (export_key)"
                )
            )
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "ix_master_invoice_exports_invoice_number "
                    "ON master_invoice_exports (invoice_number)"
                )
            )
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "ix_master_invoice_exports_invoice_sequence "
                    "ON master_invoice_exports (invoice_sequence)"
                )
            )

        if "outbound_messages" in tables:
            existing = {
                column["name"]
                for column in inspector.get_columns("outbound_messages")
            }
            for name, column_type in OUTBOUND_MESSAGE_COLUMNS.items():
                if name not in existing:
                    connection.execute(
                        text(
                            "ALTER TABLE outbound_messages ADD COLUMN "
                            f"{name} {column_type}"
                        )
                    )
            connection.execute(
                text(
                    "UPDATE outbound_messages SET queued_at = created_at "
                    "WHERE queued_at IS NULL"
                )
            )
            connection.execute(
                text(
                    "UPDATE outbound_messages SET updated_at = created_at "
                    "WHERE updated_at IS NULL"
                )
            )
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "ix_outbound_messages_idempotency_key "
                    "ON outbound_messages (idempotency_key)"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_outbound_messages_enrollment_id "
                    "ON outbound_messages (enrollment_id)"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS "
                    "ix_outbound_messages_invoice_generation_id "
                    "ON outbound_messages (invoice_generation_id)"
                )
            )


def backup_sqlite_before_schema_sync():
    """Create a consistent copy before create_all or additive ALTER statements."""
    url = db.engine.url
    if url.get_backend_name() != "sqlite":
        return None
    database = url.database
    if not database or database == ":memory:":
        return None
    source = Path(database).resolve()
    if not source.is_file() or source.stat().st_size == 0:
        return None

    inspector = inspect(db.engine)
    existing_tables = set(inspector.get_table_names())
    if not existing_tables:
        return None
    expected_tables = set(db.metadata.tables)
    schema_change_needed = bool(expected_tables - existing_tables)
    if not schema_change_needed:
        for table_name in existing_tables & expected_tables:
            actual = {column["name"] for column in inspector.get_columns(table_name)}
            expected = set(db.metadata.tables[table_name].columns.keys())
            if expected - actual:
                schema_change_needed = True
                break
    if not schema_change_needed:
        return None

    backup_dir = source.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = backup_dir / f"{source.stem}-pre-migration-{stamp}{source.suffix}"
    with sqlite3.connect(source) as source_db, sqlite3.connect(destination) as backup_db:
        source_db.backup(backup_db)
    return destination


__all__ = ["apply_additive_migrations", "backup_sqlite_before_schema_sync"]
