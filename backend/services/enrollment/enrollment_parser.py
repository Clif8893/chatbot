"""Deterministic parsing for known enrollment forms and structured values."""

import re

from backend.services.privacy import redact_for_classification


FIELD_LABEL_PATTERNS = {
    "full_name": r"full\s+name(?:\s+as\s+shown\s+on\s+(?:your\s+)?nric(?:/fin)?)?|name",
    "nric": r"nric(?:/fin)?|fin(?:\s+number)?",
    "date_of_birth": r"date\s+of\s+birth|d\.?o\.?b\.?,?|dob|born",
    "email": r"e-?mail(?:\s+address)?",
    "preferred_intake_date": r"preferred\s+(?:course\s+)?(?:intake|date)|course\s+date|intake",
    "mobile_number": r"mobile(?:\s+number)?|handphone(?:\s+number)?|phone(?:\s+number)?|hp",
}

_LABEL_ALTERNATION = "|".join(
    f"(?P<{field}>{pattern})" for field, pattern in FIELD_LABEL_PATTERNS.items()
)
_LABEL_PATTERN = re.compile(
    rf"(?ix)(?:(?:\d{{1,2}}\s*[.)]\s*)|(?<![A-Za-z]))"
    rf"(?:{_LABEL_ALTERNATION})\s*(?:is\b|[:=\-])?\s*"
)
_NRIC_PATTERN = re.compile(r"\b[STFGM]\s*\d(?:\s*\d){6}\s*[A-Z]\b", re.I)
_EMAIL_PATTERN = re.compile(r"\b[^\s,;:@]+@[^\s,;:@]+\.[A-Za-z]{2,}\b")
_MOBILE_PATTERN = re.compile(
    r"(?<!\d)(?:(?:\+?65)[\s-]?)?([89]\d{3}[\s-]?\d{4})(?!\d)"
)
_CORRECTION_PATTERN = re.compile(r"\b(?:change|correct|update|replace)\b", re.I)


def parse_enrollment_fields(message):
    """Extract only values grounded in a recognised label or safe structure."""
    text = _normalise_text(message)
    fields = _labelled_fields(text)
    lowered = text.casefold()

    nric_match = _NRIC_PATTERN.search(text)
    if nric_match:
        fields.setdefault("nric", re.sub(r"\s", "", nric_match.group(0)))
        name_prefix = text[: nric_match.start()]
        leading_name = name_prefix.strip(" ,;-\n\t")
        if (
            "full_name" not in fields
            and name_prefix.rstrip().endswith(",")
            and leading_name
            and len(leading_name) <= 255
            and all(
                part.replace("-", "").replace("'", "").isalpha()
                for part in leading_name.split()
            )
        ):
            fields["full_name"] = leading_name

    email_match = _EMAIL_PATTERN.search(text)
    if email_match:
        fields.setdefault("email", email_match.group(0))

    mobile_match = _MOBILE_PATTERN.search(text)
    if mobile_match:
        fields.setdefault("mobile_number", mobile_match.group(0))

    natural_name = re.search(
        r"(?i)^\s*i\s+am\s+([A-Za-z][A-Za-z '\-]*?)"
        r"(?=\s*(?:[,;\n]|\band\s+(?:my\s+)?(?:email|e-?mail|mobile|phone|handphone|nric|dob|date\s+of\s+birth)\b))",
        text,
    )
    if natural_name:
        fields.setdefault("full_name", natural_name.group(1).strip())

    if "next available intake" in lowered or "awaiting confirmation" in lowered:
        fields["preferred_intake_date"] = "Awaiting confirmation"

    return {field: _clean_value(field, value) for field, value in fields.items()}


def is_explicit_correction(message):
    return bool(_CORRECTION_PATTERN.search(message or ""))


def redact_identity_for_ai(message):
    """Keep identity values out of model extraction while retaining their label."""
    return redact_for_classification(message or "")


def _labelled_fields(text):
    matches = list(_LABEL_PATTERN.finditer(text))
    fields = {}
    for index, match in enumerate(matches):
        field = next(
            name for name in FIELD_LABEL_PATTERNS if match.groupdict().get(name)
        )
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = text[match.end() : end]
        value = re.sub(r"^[\s,;.]+|[\s,;.]+$", "", value)
        if value:
            fields[field] = value
    return fields


def _normalise_text(message):
    return re.sub(r"[\t\r]+", " ", str(message or "")).strip()


def _clean_value(field, value):
    cleaned = " ".join(str(value).strip().split())
    cleaned = re.sub(r"^(?:is\s+)", "", cleaned, flags=re.I)
    if field in {"full_name", "date_of_birth", "preferred_intake_date"}:
        cleaned = re.split(r"[,;]", cleaned, maxsplit=1)[0].strip()
    if field == "full_name":
        cleaned = re.sub(r"^\d+\s*[.)]\s*", "", cleaned)
        cleaned = re.split(r"[.\n]", cleaned, maxsplit=1)[0].strip()
        cleaned = re.sub(
            r"(?i)\s+(?:and\s+)?(?:my\s*)?$", "", cleaned
        ).strip()
        if cleaned.casefold() in {"and", "my", "and my"}:
            return ""
    if field == "nric":
        match = _NRIC_PATTERN.search(cleaned)
        return re.sub(r"\s", "", match.group(0)) if match else cleaned
    if field == "email":
        match = _EMAIL_PATTERN.search(cleaned)
        return match.group(0) if match else re.split(r"[,;]", cleaned, maxsplit=1)[0]
    if field == "mobile_number":
        match = _MOBILE_PATTERN.search(cleaned)
        return match.group(0) if match else re.split(r"[,;]", cleaned, maxsplit=1)[0]
    return cleaned


__all__ = [
    "is_explicit_correction",
    "parse_enrollment_fields",
    "redact_identity_for_ai",
]
