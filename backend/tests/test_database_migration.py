"""Coverage for additive enrollment and participant-ownership migrations."""

import sqlite3

from sqlalchemy import inspect

from backend.app import create_app
from backend.extensions import db
from backend.models import Conversation, EnrollmentDraft
from backend.models.operations import CourseDate


def test_existing_database_data_survives_enrollment_field_migration(tmp_path, fake_ai):
    database_path = tmp_path / "existing.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE conversations (
                id VARCHAR(36) PRIMARY KEY,
                active_intent VARCHAR(32),
                status VARCHAR(32) NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            );
            CREATE TABLE enrollment_drafts (
                id VARCHAR(36) PRIMARY KEY,
                conversation_id VARCHAR(36) NOT NULL UNIQUE,
                course VARCHAR(255),
                full_name VARCHAR(255),
                nric VARCHAR(16),
                date_of_birth DATE,
                email VARCHAR(320),
                preferred_intake_date DATE,
                status VARCHAR(32) NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                confirmed_at DATETIME,
                FOREIGN KEY(conversation_id) REFERENCES conversations (id)
            );
            CREATE TABLE course_dates (
                id VARCHAR(36) PRIMARY KEY,
                date DATE NOT NULL UNIQUE,
                end_date DATE NOT NULL,
                label VARCHAR(255) NOT NULL,
                capacity INTEGER NOT NULL,
                status VARCHAR(24) NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            );
            CREATE TABLE master_invoice_exports (
                id VARCHAR(36) PRIMARY KEY,
                enrollment_id VARCHAR(36) NOT NULL UNIQUE,
                status VARCHAR(24) NOT NULL,
                attempts INTEGER NOT NULL,
                last_error VARCHAR(255),
                exported_at DATETIME,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                FOREIGN KEY(enrollment_id) REFERENCES enrollment_drafts (id)
            );
            INSERT INTO conversations VALUES (
                'existing-conversation', 'enrollment', 'active',
                '2026-09-01 00:00:00.000000', '2026-09-01 00:00:00.000000'
            );
            INSERT INTO enrollment_drafts VALUES (
                'existing-draft', 'existing-conversation',
                '2-Day Basic Certificate in Dental Assisting', 'Test Student',
                'S1234567D', '2000-05-15', 'test.student@example.com', '2026-10-10',
                'confirmed', '2026-09-01 00:00:00.000000',
                '2026-09-01 00:00:00.000000', NULL
            );
            INSERT INTO course_dates VALUES (
                'existing-intake', '2026-10-10', '2026-10-11', 'Weekend', 20,
                'open', '2026-09-01 00:00:00.000000', '2026-09-01 00:00:00.000000'
            );
            """
        )

    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database_path.as_posix()}",
        },
        ai_service=fake_ai,
    )

    with app.app_context():
        columns = {
            column["name"] for column in inspect(db.engine).get_columns("enrollment_drafts")
        }
        assert {
            "course_fee",
            "intake_id",
            "intake_start_date",
            "intake_end_date",
            "intake_is_demo",
            "allocated_intake_id",
            "intake_allocated_at",
            "mobile_number",
            "payment_method",
            "skillsfuture_amount",
            "paynow_amount",
            "validation_status",
            "pending_corrections",
            "validation_errors",
            "cancellation_requested_at",
            "cancelled_at",
            "cancellation_reason",
            "reschedule_requested_at",
            "rescheduled_at",
            "previous_intake_id",
            "refund_review_required",
        } <= columns
        conversation_columns = {
            column["name"] for column in inspect(db.engine).get_columns("conversations")
        }
        assert {
            "user_id",
            "pending_followup",
            "pending_action",
            "pending_enrollment_id",
            "pending_target_intake_id",
            "pending_source_intake_id",
            "pending_intake_options",
            "pending_action_requested_at",
        } <= conversation_columns
        message_columns = {
            column["name"] for column in inspect(db.engine).get_columns("messages")
        }
        assert {"image_url", "image_alt", "image_status"} <= message_columns
        export_columns = {
            column["name"]
            for column in inspect(db.engine).get_columns("master_invoice_exports")
        }
        assert {
            "export_key",
            "invoice_number",
            "invoice_sequence",
            "invoice_date",
            "workbook_row",
            "export_version",
            "content_hash",
            "last_attempted_at",
        } <= export_columns
        assert db.session.query(Conversation).count() == 1
        legacy_conversation = db.session.get(Conversation, "existing-conversation")
        assert legacy_conversation.user_id is None
        draft = db.session.get(EnrollmentDraft, "existing-draft")
        assert draft.full_name == "Test Student"
        assert draft.email == "test.student@example.com"
        assert draft.mobile_number is None
        assert draft.course_fee is None
        assert draft.intake_id is None
        assert draft.intake_start_date is None
        assert draft.intake_end_date is None
        assert draft.intake_is_demo is None
        assert draft.payment_method is None
        assert draft.skillsfuture_amount is None
        assert draft.paynow_amount is None
        assert draft.validation_status == "pending"
        assert draft.pending_corrections == []
        assert draft.validation_errors == {}
        assert draft.allocated_intake_id == "existing-intake"
        assert db.session.get(CourseDate, "existing-intake").participant_count == 1
