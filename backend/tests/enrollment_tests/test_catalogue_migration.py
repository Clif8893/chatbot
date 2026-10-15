"""Safe catalogue replacement and historical-record compatibility regressions."""

from datetime import date
from decimal import Decimal

from backend.app import create_app
from backend.extensions import db
from backend.models import Conversation, EnrollmentCourse, EnrollmentDraft, utc_now
from backend.models.operations import CourseDate
from backend.tests.conftest import authenticated_client, send


OLD_ID = "dental-infection-control"
OLD_NAME = "1-Day Dental Infection Control & Sterilisation Workshop"


def migration_app(tmp_path, fake_ai, name="catalogue-migration.db"):
    return create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "catalogue-migration-test",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / name).as_posix()}",
            "INVOICE_GENERATION_ENABLED": False,
        },
        ai_service=fake_ai,
    )


def test_retired_pending_course_redirects_without_losing_valid_particulars(tmp_path, fake_ai):
    app = migration_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    with app.app_context():
        conversation = db.session.get(Conversation, conversation_id)
        draft = EnrollmentDraft(
            conversation=conversation,
            course_id=OLD_ID,
            course=OLD_NAME,
            course_fee=Decimal("350"),
            full_name="Test Student",
            nric="S1234567D",
            date_of_birth=date(2000, 5, 15),
            email="test.student@example.com",
            mobile_number="91234567",
            status="collecting",
        )
        db.session.add(draft)
        db.session.flush()
        db.session.add(EnrollmentCourse(
            registration=draft,
            position=1,
            course_id=OLD_ID,
            course_name=OLD_NAME,
            course_fee=Decimal("350"),
            status="collecting",
        ))
        db.session.commit()

    payload = send(client, conversation_id, "Continue my enrollment").get_json()
    content = payload["message"]["content"]
    assert "no longer available" in content
    assert "Basic Certificate in Dental Assisting" in content
    assert "Certificate in Dental Clinic Front Desk Operations" in content
    assert OLD_NAME not in content
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.full_name == "Test Student"
        assert draft.nric == "S1234567D"
        assert draft.email == "test.student@example.com"
        assert draft.course is None and draft.course_id is None
        assert draft.course_enrollments[0].status == "retired_unavailable"


def test_historical_confirmed_course_record_remains_unchanged_and_loadable(tmp_path, fake_ai):
    app = migration_app(tmp_path, fake_ai, "historical.db")
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    with app.app_context():
        conversation = db.session.get(Conversation, conversation_id)
        draft = EnrollmentDraft(
            conversation=conversation,
            course_id=OLD_ID,
            course=OLD_NAME,
            course_fee=Decimal("350"),
            full_name="Historical Student",
            nric="S1234567D",
            date_of_birth=date(2000, 5, 15),
            email="historical@example.com",
            mobile_number="91234567",
            preferred_intake_date=date(2026, 10, 18),
            intake_start_date=date(2026, 10, 18),
            intake_end_date=date(2026, 10, 18),
            status="confirmed",
            validation_status="valid",
            confirmed_at=utc_now(),
        )
        db.session.add(draft)
        db.session.flush()
        db.session.add(EnrollmentCourse(
            registration=draft,
            position=1,
            course_id=OLD_ID,
            course_name=OLD_NAME,
            course_fee=Decimal("350"),
            intake_start_date=date(2026, 10, 18),
            intake_end_date=date(2026, 10, 18),
            status="confirmed",
            confirmed_at=draft.confirmed_at,
        ))
        db.session.commit()

    status = send(client, conversation_id, "Enrollment status").get_json()["message"]["content"]
    assert "No saved enrollment was found" in status
    assert OLD_NAME not in status
    assert "S1234567D" not in status
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.course_id == OLD_ID
        assert draft.course == OLD_NAME
        assert draft.course_fee == Decimal("350")
        assert draft.course_enrollments[0].course_name == OLD_NAME


def test_seed_sync_retires_old_intake_without_deleting_or_resetting_count(tmp_path, fake_ai):
    name = "retire-intake.db"
    app = migration_app(tmp_path, fake_ai, name)
    with app.app_context():
        db.session.add(CourseDate(
            id="historical-infection-control-2026-10-18",
            course_id=OLD_ID,
            date=date(2026, 10, 18),
            end_date=date(2026, 10, 18),
            label="Sunday",
            capacity=20,
            participant_count=7,
            status="open",
            is_demo=True,
        ))
        db.session.commit()

    restarted = migration_app(tmp_path, fake_ai, name)
    with restarted.app_context():
        row = db.session.get(CourseDate, "historical-infection-control-2026-10-18")
        assert row is not None
        assert row.status == "retired"
        assert row.participant_count == 7
        assert CourseDate.query.filter_by(course_id="dental-assisting-basic").count() == 3
        assert CourseDate.query.filter_by(course_id="dental-clinic-front-desk-operations").count() == 3
