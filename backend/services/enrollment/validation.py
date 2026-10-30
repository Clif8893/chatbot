"""Deterministic validation for enrollment data."""

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from dateutil import parser as date_parser
from email_validator import EmailNotValidError, validate_email


NRIC_FORMAT = re.compile(r"^[STFGM]\d{7}[A-Z]$", re.IGNORECASE)
MOBILE_FORMAT = re.compile(r"^[89]\d{7}$")
PLACEHOLDER_VALUES = {"test", "abc", "n/a", "na", "unknown", "none", "null"}
IDENTITY_WEIGHTS = (2, 7, 6, 5, 4, 3, 2)
IDENTITY_OFFSETS = {"S": 0, "T": 4, "F": 0, "G": 4, "M": 3}
IDENTITY_CHECKSUMS = {
    "S": "JZIHGFEDCBA",
    "T": "JZIHGFEDCBA",
    "F": "XWUTRQPNMLK",
    "G": "XWUTRQPNMLK",
    "M": "XWUTRQPNJLK",
}


FIELD_LABELS = {
    "course": "course",
    "full_name": "full name",
    "nric": "NRIC",
    "date_of_birth": "date of birth",
    "email": "email address",
    "preferred_intake_date": "preferred intake date",
    "mobile_number": "mobile number",
}


def parse_date(value, *, as_of_date=None, minimum_age=16, maximum_age=100):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        normalized = re.sub(r"(?i)(\d{1,2})(?:st|nd|rd|th)\b", r"\1", str(value))
        normalized = " ".join(normalized.strip().split())
        # Compact dates are common on mobile keyboards.  Their order is
        # deterministic here: DDMMYYYY, except an unmistakable leading year.
        if re.fullmatch(r"\d{8}", normalized):
            year_first = (
                normalized.startswith(("19", "20"))
                and 1 <= int(normalized[4:6]) <= 12
                and 1 <= int(normalized[6:8]) <= 31
            )
            date_format = "%Y%m%d" if year_first else "%d%m%Y"
            return datetime.strptime(normalized, date_format).date()
        if re.fullmatch(r"\d{6}", normalized):
            day, month, short_year = (
                int(normalized[:2]), int(normalized[2:4]), int(normalized[4:])
            )
            today = as_of_date or current_date_in()
            for year in (2000 + short_year, 1900 + short_year):
                try:
                    candidate = date(year, month, day)
                except ValueError:
                    continue
                if candidate < today and minimum_age <= calculate_age(candidate, today) <= maximum_age:
                    return candidate
            # Keep an out-of-range but valid date for the age validator to
            # explain. A malformed calendar date still fails parsing.
            return datetime.strptime(normalized, "%d%m%y").date()
        if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", normalized.strip()):
            return datetime.strptime(normalized.strip(), "%d/%m/%Y").date()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized.strip()):
            return date.fromisoformat(normalized.strip())
        return date_parser.parse(normalized, dayfirst=True, fuzzy=False).date()
    except (TypeError, ValueError, OverflowError):
        return None


def current_date_in(timezone_name="Asia/Singapore"):
    return datetime.now(ZoneInfo(timezone_name)).date()


def calculate_age(date_of_birth, as_of_date):
    return as_of_date.year - date_of_birth.year - (
        (as_of_date.month, as_of_date.day)
        < (date_of_birth.month, date_of_birth.day)
    )


def validate_field(
    field,
    value,
    catalogue,
    *,
    minimum_age=16,
    maximum_age=100,
    as_of_date=None,
    timezone_name="Asia/Singapore",
    mobile_country_code="65",
    course_value=None,
):
    """Return a normalized value and an optional user-facing error."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, f"Please provide your {FIELD_LABELS[field]}."

    if field == "course":
        course = catalogue.match_course(value)
        if course:
            return course["name"], None
        return None, (
            "Enrollment is currently available for the courses shown. "
            "Please choose one by number or name."
        )

    if field == "full_name":
        normalized = " ".join(str(value).split())
        parts = (
            normalized.replace("-", " ")
            .replace("'", " ")
            .replace("’", " ")
            .split()
        )
        if (
            len(normalized) < 2
            or len(normalized) > 255
            or normalized.casefold() in PLACEHOLDER_VALUES | {"and", "my", "and my"}
            or not parts
            or not all(part.isalpha() for part in parts)
            or re.search(r"(?i)\b(?:nric|email|mobile|date of birth|dob)\b", normalized)
        ):
            return None, (
                "Please enter the full name shown on the NRIC/FIN using letters, "
                "spaces, hyphens or apostrophes."
            )
        return normalized, None

    if field == "nric":
        if "*" in str(value) or "redacted" in str(value).casefold():
            return None, "Please enter the complete NRIC/FIN for validation. It will be masked after it is saved."
        normalized = str(value).replace(" ", "").upper()
        if not NRIC_FORMAT.fullmatch(normalized):
            return None, (
                "Please enter a valid Singapore NRIC/FIN format, for example "
                "one letter, seven digits and a final letter."
            )
        if not has_valid_identity_checksum(normalized):
            return None, (
                "Please enter a valid Singapore NRIC/FIN, including the correct "
                "final checksum letter."
            )
        return normalized, None

    if field == "date_of_birth":
        today = as_of_date or current_date_in(timezone_name)
        normalized = parse_date(
            value, as_of_date=today, minimum_age=minimum_age, maximum_age=maximum_age
        )
        if not normalized or normalized >= today:
            return None, _dob_error()
        age = calculate_age(normalized, today)
        if age < minimum_age or age > maximum_age:
            return None, _dob_error()
        return normalized, None

    if field == "email":
        supplied = str(value).strip()
        if supplied.casefold() in PLACEHOLDER_VALUES:
            return None, "Please enter a valid email address."
        try:
            normalized = validate_email(
                supplied, check_deliverability=False
            ).normalized
        except EmailNotValidError:
            return None, "Please enter a valid email address."
        return normalized, None

    if field == "preferred_intake_date":
        if not catalogue.has_intake_dates:
            return None, catalogue.no_intakes_message
        intake, error = catalogue.match_intake_selection(value, course_value)
        if error:
            return None, error
        return date.fromisoformat(intake["start_date"]), None

    if field == "mobile_number":
        normalized = re.sub(r"[\s()\-]", "", str(value))
        prefix = str(mobile_country_code).lstrip("+")
        if normalized.startswith(f"+{prefix}"):
            normalized = normalized[len(prefix) + 1:]
        elif normalized.startswith(prefix) and len(normalized) == 8 + len(prefix):
            normalized = normalized[len(prefix):]
        if not MOBILE_FORMAT.fullmatch(normalized):
            return None, (
                "Please enter a valid 8-digit Singapore mobile number starting "
                "with 8 or 9."
            )
        return normalized, None

    raise ValueError(f"Unsupported enrollment field: {field}")


def _dob_error():
    return (
        "Please check your date of birth. Applicants must provide a valid past "
        "date and meet the minimum enrollment age. You can type it naturally, "
        "for example 15/05/2000, 2000-05-15, or 15 May 2000."
    )


def has_valid_identity_checksum(value):
    normalized = str(value).replace(" ", "").upper()
    if not NRIC_FORMAT.fullmatch(normalized):
        return False
    weighted_sum = sum(
        int(digit) * weight
        for digit, weight in zip(normalized[1:8], IDENTITY_WEIGHTS)
    )
    weighted_sum += IDENTITY_OFFSETS[normalized[0]]
    expected = IDENTITY_CHECKSUMS[normalized[0]][weighted_sum % 11]
    return normalized[-1] == expected
