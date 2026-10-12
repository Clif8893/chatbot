"""Deterministic helpers for grouped one- or two-course registrations."""

from datetime import datetime, time
from decimal import Decimal

from backend.extensions import db
from backend.models import EnrollmentCourse


ACTIVE_CHILD_STATUSES = {
    "collecting",
    "awaiting_confirmation",
    "confirmed",
    "enrolled",
    "invoice_sent",
    "awaiting_payment",
    "paid",
    "receipt_issued",
    "completed",
}


def ordered_courses(registration, *, include_cancelled=False):
    rows = sorted(registration.course_enrollments, key=lambda row: (row.position, row.id))
    if include_cancelled:
        return rows
    return [row for row in rows if row.status != "cancelled"]


def add_course(registration, course, catalogue):
    """Add a canonical course once, preserving participant-selected order."""
    course_id = course["id"]
    existing = next(
        (row for row in registration.course_enrollments if row.course_id == course_id),
        None,
    )
    if existing is not None:
        return existing, False
    active = ordered_courses(registration)
    if len(active) >= 2:
        raise ValueError("A registration can contain at most two different courses.")
    row = EnrollmentCourse(
        registration=registration,
        position=len(active) + 1,
        course_id=course_id,
        course_name=course["name"],
        course_fee=catalogue.course_fee_for(course),
        status="collecting",
    )
    db.session.add(row)
    db.session.flush()
    sync_registration(registration)
    return row, True


def remove_course(registration, row):
    if row.allocated_intake_id:
        raise ValueError("A confirmed course must be cancelled instead of removed.")
    db.session.delete(row)
    db.session.flush()
    remaining = ordered_courses(registration)
    for position, item in enumerate(remaining, start=1):
        item.position = position
    db.session.flush()
    sync_registration(registration)


def save_intake(row, intake, catalogue):
    course = catalogue.course_for(intake.get("course_id"))
    if course is None or course["id"] != row.course_id:
        raise ValueError("The selected intake does not belong to the selected course.")
    row.intake_id = intake["id"]
    row.intake_start_date = _date(intake["start_date"])
    row.intake_end_date = _date(intake["end_date"])
    row.intake_is_demo = bool(intake.get("is_demo"))
    row.updated_at = datetime.now().astimezone()
    sync_registration(row.registration)


def clear_intake(row):
    row.intake_id = None
    row.intake_start_date = None
    row.intake_end_date = None
    row.intake_is_demo = None
    sync_registration(row.registration)


def sync_registration(registration):
    """Mirror the first child into legacy columns and store the grouped total."""
    rows = ordered_courses(registration)
    if not rows:
        registration.course = None
        registration.course_id = None
        registration.course_fee = None
        registration.preferred_intake_date = None
        registration.intake_id = None
        registration.intake_start_date = None
        registration.intake_end_date = None
        registration.intake_is_demo = None
        registration.allocated_intake_id = None
        registration.intake_allocated_at = None
        return
    primary = rows[0]
    registration.course = primary.course_name
    registration.course_id = primary.course_id
    registration.course_fee = (
        None
        if any(row.course_fee is None for row in rows)
        else sum((Decimal(str(row.course_fee)) for row in rows), Decimal("0.00"))
    )
    registration.preferred_intake_date = primary.intake_start_date
    registration.intake_id = primary.intake_id
    registration.intake_start_date = primary.intake_start_date
    registration.intake_end_date = primary.intake_end_date
    registration.intake_is_demo = primary.intake_is_demo
    registration.allocated_intake_id = primary.allocated_intake_id
    registration.intake_allocated_at = primary.intake_allocated_at


def next_course_without_intake(registration):
    return next((row for row in ordered_courses(registration) if not row.intake_id), None)


def invoice_courses(registration):
    return [
        row
        for row in ordered_courses(registration)
        if row.status not in {"cancelled", "discarded"}
    ]


def total_fee(registration):
    if any(row.course_fee is None for row in invoice_courses(registration)):
        return None
    return sum(
        (Decimal(str(row.course_fee)) for row in invoice_courses(registration)),
        Decimal("0.00"),
    )


def courses_overlap(left, right):
    """Return true when two selected training ranges overlap in date and time."""
    if not all(
        (
            left.intake_start_date,
            left.intake_end_date,
            right.intake_start_date,
            right.intake_end_date,
        )
    ):
        return False
    if left.intake_end_date < right.intake_start_date:
        return False
    if right.intake_end_date < left.intake_start_date:
        return False
    # Current approved courses occupy their training days. If two date ranges
    # intersect, their standard daytime schedules overlap.
    return True


def display_range(row):
    start = row.intake_start_date
    end = row.intake_end_date or start
    if start is None:
        return "Awaiting selection"
    if start == end:
        return f"{start.day} {start.strftime('%B %Y')}"
    if start.year == end.year and start.month == end.month:
        return f"{start.day}–{end.day} {start.strftime('%B %Y')}"
    if start.year == end.year:
        return f"{start.day} {start.strftime('%B')}–{end.day} {end.strftime('%B %Y')}"
    return f"{start.day} {start.strftime('%B %Y')}–{end.day} {end.strftime('%B %Y')}"


def _date(value):
    from datetime import date

    return value if isinstance(value, date) else date.fromisoformat(str(value))


__all__ = [
    "ACTIVE_CHILD_STATUSES",
    "add_course",
    "clear_intake",
    "courses_overlap",
    "display_range",
    "invoice_courses",
    "next_course_without_intake",
    "ordered_courses",
    "remove_course",
    "save_intake",
    "sync_registration",
    "total_fee",
]
