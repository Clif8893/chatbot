"""Database-backed course-intake availability and place allocation."""

from datetime import datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update

from backend.extensions import db
from backend.models import EnrollmentCourse, EnrollmentDraft, utc_now
from backend.models.operations import CourseDate
from backend.services.enrollment.registration_group_service import sync_registration


DEFAULT_COURSE_ID = "dental-assisting-basic"


def singapore_today(timezone_name="Asia/Singapore"):
    return datetime.now(ZoneInfo(timezone_name)).date()


class IntakeService:
    """Read and mutate intake state without delegating business rules to the LLM."""

    def __init__(self, timezone_name="Asia/Singapore", current_date=None):
        self.timezone_name = timezone_name
        self.current_date = current_date

    @property
    def today(self):
        return self.current_date or singapore_today(self.timezone_name)

    def catalogue_items(self):
        return [self.to_catalogue_item(row) for row in CourseDate.query.order_by(CourseDate.date, CourseDate.id).all()]

    def to_catalogue_item(self, row):
        status = row.status.casefold()
        count = int(row.participant_count or 0)
        if row.date <= self.today:
            status = "historical"
        elif status == "open" and count >= row.capacity:
            status = "full"
        intake_type = row.label.strip() or (
            "Weekend" if row.date.weekday() >= 5 else "Weekday"
        )
        return {
            "id": row.id,
            "course_id": row.course_id or DEFAULT_COURSE_ID,
            "start_date": row.date.isoformat(),
            "end_date": row.end_date.isoformat(),
            "start_time": (row.start_time or time(9, 30)).strftime("%H:%M"),
            "end_time": (row.end_time or time(17, 30)).strftime("%H:%M"),
            "intake_type": intake_type,
            "display_date": _display_range(row.date, row.end_date),
            "capacity": row.capacity,
            "participant_count": count,
            "available_places": max(0, row.capacity - count),
            "status": status,
            "is_demo": bool(row.is_demo),
        }

    def seed_intakes(self, records):
        """Insert missing catalogue rows once; never overwrite staff-managed rows."""
        created = 0
        for item in records:
            existing = db.session.get(CourseDate, item["id"])
            if existing is None:
                existing = CourseDate.query.filter_by(
                    date=datetime.strptime(item["start_date"], "%Y-%m-%d").date()
                ).first()
            if existing is not None:
                continue
            db.session.add(
                CourseDate(
                    id=item["id"],
                    course_id=item.get("course_id", DEFAULT_COURSE_ID),
                    date=datetime.strptime(item["start_date"], "%Y-%m-%d").date(),
                    end_date=datetime.strptime(item["end_date"], "%Y-%m-%d").date(),
                    start_time=_parse_time(item.get("start_time", "09:30")),
                    end_time=_parse_time(item.get("end_time", "17:30")),
                    label=item.get("intake_type", item.get("label", "")),
                    capacity=int(item.get("capacity", 20)),
                    participant_count=int(item.get("participant_count", 0)),
                    status=item.get("status", "open"),
                    is_demo=bool(item.get("is_demo")),
                )
            )
            created += 1
        if created:
            db.session.commit()
        return created

    def seed_demo_intakes(self, records):
        return self.seed_intakes(records)

    def allocation_error(self, intake_id, excluding_draft=None):
        row = db.session.get(CourseDate, intake_id)
        if row is None:
            return "The selected intake is no longer available. Please choose another intake."
        item = self.to_catalogue_item(row)
        if excluding_draft and excluding_draft.allocated_intake_id == intake_id:
            return None
        messages = {
            "historical": "The selected intake has already started. Please choose a future intake.",
            "full": f"The {item['display_date']} intake is full. Please choose another intake.",
            "closed": f"The {item['display_date']} intake is closed. Please choose another intake.",
            "cancelled": f"The {item['display_date']} intake is cancelled. Please choose another intake.",
        }
        return None if item["status"] == "open" else messages.get(
            item["status"],
            f"The {item['display_date']} intake is unavailable. Please choose another intake.",
        )

    def allocate(self, draft):
        """Atomically allocate one place; repeated confirmation is idempotent."""
        target_id = draft.intake_id
        if target_id is None:
            return
        if draft.allocated_intake_id == target_id:
            return
        previous_id = draft.allocated_intake_id
        allocated_at = utc_now()
        expected_allocation = (
            EnrollmentDraft.allocated_intake_id.is_(None)
            if previous_id is None
            else EnrollmentDraft.allocated_intake_id == previous_id
        )
        with db.session.begin_nested():
            claim = db.session.execute(
                update(EnrollmentDraft)
                .where(
                    EnrollmentDraft.id == draft.id,
                    expected_allocation,
                )
                .values(
                    allocated_intake_id=target_id,
                    intake_allocated_at=allocated_at,
                )
                .execution_options(synchronize_session=False)
            )
            if claim.rowcount != 1:
                allocated = db.session.execute(
                    select(EnrollmentDraft.allocated_intake_id).where(
                        EnrollmentDraft.id == draft.id
                    )
                ).scalar_one()
                if allocated == target_id:
                    draft.allocated_intake_id = target_id
                    return
                raise ValueError("The intake allocation changed. Please review and confirm again.")
            statement = (
                update(CourseDate)
                .where(
                    CourseDate.id == target_id,
                    CourseDate.status == "open",
                    CourseDate.date > self.today,
                    func.coalesce(CourseDate.participant_count, 0) < CourseDate.capacity,
                )
                .values(participant_count=func.coalesce(CourseDate.participant_count, 0) + 1)
            )
            result = db.session.execute(statement)
            if result.rowcount != 1:
                raise ValueError(self.allocation_error(target_id, draft))
            if previous_id and previous_id != target_id:
                db.session.execute(
                    update(CourseDate)
                    .where(CourseDate.id == previous_id, CourseDate.participant_count > 0)
                    .values(participant_count=CourseDate.participant_count - 1)
                )
        draft.allocated_intake_id = target_id
        draft.intake_allocated_at = allocated_at
        db.session.flush()

    def release(self, draft):
        if not draft.allocated_intake_id:
            return
        previous_id = draft.allocated_intake_id
        with db.session.begin_nested():
            claim = db.session.execute(
                update(EnrollmentDraft)
                .where(
                    EnrollmentDraft.id == draft.id,
                    EnrollmentDraft.allocated_intake_id == previous_id,
                )
                .values(allocated_intake_id=None, intake_allocated_at=None)
                .execution_options(synchronize_session=False)
            )
            if claim.rowcount != 1:
                return
            db.session.execute(
                update(CourseDate)
                .where(
                    CourseDate.id == previous_id,
                    CourseDate.participant_count > 0,
                )
                .values(participant_count=CourseDate.participant_count - 1)
            )
        draft.allocated_intake_id = None
        draft.intake_allocated_at = None
        db.session.flush()

    def allocate_registration(self, registration, courses):
        """Atomically reserve every unallocated child course intake."""
        pending = [row for row in courses if row.intake_id and row.allocated_intake_id != row.intake_id]
        if not pending:
            sync_registration(registration)
            return
        for row in pending:
            error = self.allocation_error(row.intake_id, row)
            if error:
                raise ValueError(error)
            intake = db.session.get(CourseDate, row.intake_id)
            if intake is None or intake.course_id != row.course_id:
                raise ValueError(
                    "The selected intake does not belong to the selected course. "
                    "Please choose another intake."
                )

        allocated_at = utc_now()
        with db.session.begin_nested():
            for row in pending:
                previous_id = row.allocated_intake_id
                expected = (
                    EnrollmentCourse.allocated_intake_id.is_(None)
                    if previous_id is None
                    else EnrollmentCourse.allocated_intake_id == previous_id
                )
                claim = db.session.execute(
                    update(EnrollmentCourse)
                    .where(EnrollmentCourse.id == row.id, expected)
                    .values(
                        allocated_intake_id=row.intake_id,
                        intake_allocated_at=allocated_at,
                    )
                    .execution_options(synchronize_session=False)
                )
                if claim.rowcount != 1:
                    current = db.session.execute(
                        select(EnrollmentCourse.allocated_intake_id).where(
                            EnrollmentCourse.id == row.id
                        )
                    ).scalar_one()
                    if current == row.intake_id:
                        continue
                    raise ValueError(
                        "The intake allocation changed. Please review and confirm again."
                    )
                result = db.session.execute(
                    update(CourseDate)
                    .where(
                        CourseDate.id == row.intake_id,
                        CourseDate.course_id == row.course_id,
                        CourseDate.status == "open",
                        CourseDate.date > self.today,
                        func.coalesce(CourseDate.participant_count, 0)
                        < CourseDate.capacity,
                    )
                    .values(
                        participant_count=func.coalesce(
                            CourseDate.participant_count, 0
                        )
                        + 1
                    )
                )
                if result.rowcount != 1:
                    raise ValueError(self.allocation_error(row.intake_id, row))
                if previous_id and previous_id != row.intake_id:
                    db.session.execute(
                        update(CourseDate)
                        .where(
                            CourseDate.id == previous_id,
                            CourseDate.participant_count > 0,
                        )
                        .values(
                            participant_count=CourseDate.participant_count - 1
                        )
                    )
        for row in pending:
            row.allocated_intake_id = row.intake_id
            row.intake_allocated_at = allocated_at
        sync_registration(registration)
        db.session.flush()

    def release_course(self, course):
        """Release one child allocation once without affecting sibling courses."""
        if not course.allocated_intake_id:
            return
        previous_id = course.allocated_intake_id
        with db.session.begin_nested():
            claim = db.session.execute(
                update(EnrollmentCourse)
                .where(
                    EnrollmentCourse.id == course.id,
                    EnrollmentCourse.allocated_intake_id == previous_id,
                )
                .values(allocated_intake_id=None, intake_allocated_at=None)
                .execution_options(synchronize_session=False)
            )
            if claim.rowcount != 1:
                return
            db.session.execute(
                update(CourseDate)
                .where(
                    CourseDate.id == previous_id,
                    CourseDate.participant_count > 0,
                )
                .values(participant_count=CourseDate.participant_count - 1)
            )
        course.allocated_intake_id = None
        course.intake_allocated_at = None
        sync_registration(course.registration)
        db.session.flush()


def _parse_time(value):
    return datetime.strptime(str(value), "%H:%M").time()


def _display_range(start, end):
    if start == end:
        return f"{start.day} {start.strftime('%B %Y')}"
    if start.year == end.year and start.month == end.month:
        return f"{start.day}–{end.day} {start.strftime('%B %Y')}"
    if start.year == end.year:
        return f"{start.day} {start.strftime('%B')}–{end.day} {end.strftime('%B %Y')}"
    return f"{start.day} {start.strftime('%B %Y')}–{end.day} {end.strftime('%B %Y')}"


__all__ = ["DEFAULT_COURSE_ID", "IntakeService", "singapore_today"]
