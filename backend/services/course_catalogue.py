"""Provider for company-supplied course and enrollment information."""

import json
import re
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path


CATALOGUE_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "course_information.json"
)

MONEY_PATTERN = re.compile(r"(?:S\$|\$)\s*([0-9][0-9,]*(?:\.\d{1,2})?)", re.I)
UEN_PATTERN = re.compile(r"\b\d{9}[A-Z]\b", re.I)
CALENDAR_DATE_PATTERN = re.compile(
    r"\b(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}(?:st|nd|rd|th)?\s+"
    r"(?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+\d{4}|(?:January|February|March|April|May|June|"
    r"July|August|September|October|November|December)\s+\d{4})\b",
    re.I,
)


class CourseCatalogue:
    """Load editable company course information from disk."""

    def __init__(self, path=CATALOGUE_PATH, enable_demo_intakes=False):
        with Path(path).open(encoding="utf-8") as catalogue_file:
            self._data = json.load(catalogue_file)
        self.enable_demo_intakes = bool(enable_demo_intakes)
        self._intake_provider = None
        self._validate_intakes()

    def bind_intake_provider(self, provider):
        """Use the portal database as runtime intake source of truth."""
        self._intake_provider = provider

    @property
    def course(self):
        return self._data["course"]

    @property
    def courses(self):
        return [self.course, *self._data.get("additional_courses", [])]

    @property
    def retired_course_ids(self):
        return set(self._data.get("content_status", {}).get("retired_course_ids", []))

    def course_for(self, value):
        if isinstance(value, dict):
            return value
        supplied = str(value or "").casefold().strip()
        if supplied == "basic-dental-assisting":
            supplied = "dental-assisting-basic"
        if not supplied:
            return None
        for course in self.courses:
            aliases = {
                str(course.get("id", "")).casefold(),
                str(course.get("code", "")).casefold(),
                str(course.get("name", "")).casefold(),
            }
            aliases.discard("")
            if supplied in aliases or (
                "dental assist" in supplied and "dental assist" in course["name"].casefold()
            ) or (
                "front desk" in supplied and "front desk" in course["name"].casefold()
            ):
                return course
        return None

    def match_course(self, value):
        supplied = str(value or "").strip()
        if supplied in {str(index) for index in range(1, len(self.courses) + 1)}:
            return self.courses[int(supplied) - 1]
        return self.course_for(supplied)

    def match_course_selection(self, value):
        """Return canonical courses in mentioned order and flag duplicates."""
        text = " ".join(str(value or "").casefold().split())
        if not text:
            return [], False
        if re.search(r"\b(?:both|all)\s+(?:of\s+the\s+)?courses?\b", text) or text == "both":
            return list(self.courses), False

        mentions = []
        # Bare numbers are course choices only when the whole message has the
        # shape of a selection. This prevents numbered participant forms, dates,
        # phone numbers and amounts from silently adding courses.
        numeric_choice = re.fullmatch(
            r"(?:(?:course|option)\s*)?[1-9](?:\s*(?:,|and|&)\s*(?:(?:course|option)\s*)?[1-9])*",
            text,
        )
        if not numeric_choice and ":" not in text and "\n" not in str(value or ""):
            numeric_choice = len(text) < 100 and any(
                marker in text
                for marker in ("course", "dental assist", "front desk", "twice")
            )
        if numeric_choice:
            # A digit in an approved name such as "1-Day" or "2-Day" is not
            # an option number. Only standalone selection numbers qualify.
            for match in re.finditer(r"(?<![\d-])([1-9])(?![\d-])", text):
                position = int(match.group(1))
                if 1 <= position <= len(self.courses):
                    mentions.append((match.start(), self.courses[position - 1]))
        for course in self.courses:
            aliases = [course.get("name", ""), course.get("code", "")]
            if course["id"] == "dental-assisting-basic":
                aliases.extend(("dental assisting", "dental assistant"))
            if course["id"] == "dental-clinic-front-desk-operations":
                aliases.extend(("front desk", "front desk operations"))
            found = []
            for alias in aliases:
                alias_text = str(alias).casefold().strip()
                if alias_text:
                    found.extend(match.start() for match in re.finditer(re.escape(alias_text), text))
            if found:
                mentions.append((min(found), course))

        mentions.sort(key=lambda item: item[0])
        ids = [course["id"] for _, course in mentions]
        duplicate = len(ids) != len(set(ids)) or bool(
            re.search(r"\b(?:twice|two times|again|duplicate)\b", text) and ids
        )
        unique = []
        seen = set()
        for _, course in mentions:
            if course["id"] not in seen:
                unique.append(course)
                seen.add(course["id"])
        return unique[:2], duplicate

    def resolve_displayed_course_selection(self, value, course_ids):
        """Resolve one or two choices against the exact persisted course list.

        The final boolean indicates whether the message attempted a course
        choice. Unrelated text remains available to normal FAQ routing.
        """
        displayed = [
            course
            for course_id in course_ids or []
            if (course := self.course_for(course_id)) is not None
        ]
        if not displayed:
            return [], False, "The displayed course choices are no longer available.", True

        text = " ".join(str(value or "").casefold().split()).strip(" .!?")
        if not text:
            return [], False, None, False
        if re.fullmatch(r"(?:both|all)(?:\s+(?:of\s+the\s+)?courses?)?", text):
            return list(displayed[:2]), False, None, True

        ordinal = re.fullmatch(
            r"(?:the\s+)?(first|second|third)(?:\s+(?:course|option|one))?",
            text,
        )
        if ordinal:
            position = {"first": 1, "second": 2, "third": 3}[ordinal.group(1)]
            if position <= len(displayed):
                return [displayed[position - 1]], False, None, True
            return [], False, "Please choose one of the displayed course numbers.", True

        selected, duplicate = self.match_course_selection(value)
        displayed_ids = {course["id"] for course in displayed}
        if duplicate:
            return [], True, "You cannot select the same course twice.", True
        if selected:
            if any(course["id"] not in displayed_ids for course in selected):
                return [], False, "Please choose only from the displayed courses.", True
            return selected, False, None, True

        numeric_attempt = re.search(r"\b(?:course|option)\s*\d+\b", text) or re.fullmatch(
            r"\d+(?:\s*(?:,|and|&)\s*\d+)*", text
        )
        if numeric_attempt:
            return [], False, "Please choose one of the displayed course numbers.", True

        generic = {
            "a", "an", "the", "course", "courses", "programme", "programmes",
            "dental", "day", "this", "that", "enrol", "enroll", "register",
            "join", "want", "like", "please", "me", "i", "to", "in", "for",
        }
        supplied_words = set(re.findall(r"[a-z0-9]+", text)) - generic
        if not supplied_words:
            return [], False, None, False
        scores = []
        for course in displayed:
            course_words = set(
                re.findall(
                    r"[a-z0-9]+",
                    " ".join(
                        str(course.get(key, "")).casefold()
                        for key in ("id", "code", "name")
                    ),
                )
            ) - generic
            scores.append((len(supplied_words & course_words), course))
        best = max(score for score, _ in scores)
        if best:
            matches = [course for score, course in scores if score == best]
            if len(matches) > 1:
                return [], False, "That description could refer to more than one course.", True
            return [], False, "Please use the course number or full course name.", True
        return [], False, None, False

    def course_choices(self):
        return "\n".join(
            f"{index}. {course['name']} — {self.course_fee_display_for(course)}"
            for index, course in enumerate(self.courses, start=1)
        )

    def focused_course_reply(self, course_value, topic):
        """Return one verified course fact without unrelated catalogue content."""
        course = self.course_for(course_value)
        if course is None:
            return self.course_clarification_reply()
        name = course["name"]
        if topic == "fee":
            return f"The {name} is {self.course_fee_display_for(course)}."
        if topic == "time":
            duration = course.get("duration", "the scheduled training period")
            training_time = course.get("training_time")
            if not training_time:
                return (
                    f"The {name} runs for {duration.casefold()}. "
                    "The training hours have not been confirmed; our team will need to confirm them for you."
                )
            return f"The {name} runs from {training_time} ({duration})."
        if topic == "venue":
            return course.get("venue", {}).get("customer_reply") or self._data["reply_style"]["staff_confirmation_message"]
        if topic == "paynow":
            return course.get("paynow", {}).get("customer_reply") or self._data["reply_style"]["staff_confirmation_message"]
        if topic == "payment":
            options = course.get("payment_options", [])
            if options:
                return f"Payment options for {name}: " + "; ".join(options) + "."
            return self._data["reply_style"]["staff_confirmation_message"]
        return self.course_overview_reply(course)

    def course_clarification_reply(self):
        return "Which course would you like to check?\n\n" + "\n".join(
            f"{index}. {course['name']}"
            for index, course in enumerate(self.courses, start=1)
        )

    def course_fee_for(self, value):
        course = self.course_for(value)
        if course is None:
            return self.course_fee if value in (None, "") else None
        amount = course.get("fee", {}).get("amount")
        return Decimal(str(amount)) if amount not in (None, "") else None

    def course_fee_display_for(self, value):
        course = self.course_for(value)
        if course is None:
            return self.course_fee_display if value in (None, "") else "Awaiting staff confirmation"
        return course.get("fee", {}).get("display") or "Awaiting staff confirmation"

    def course_id_for(self, value):
        course = self.course_for(value)
        return course.get("id") if course else None

    @property
    def course_name(self):
        return self.course["name"]

    @property
    def course_fee(self):
        amount = self.course.get("fee", {}).get("amount")
        if amount in (None, ""):
            return None
        return Decimal(str(amount))

    @property
    def course_fee_display(self):
        return self.course.get("fee", {}).get("display") or (
            f"{self.course.get('fee', {}).get('currency', 'SGD')} "
            f"{self.course_fee:.2f}"
            if self.course_fee is not None
            else "Awaiting staff confirmation"
        )

    @property
    def skillsfuture_eligibility_display(self):
        funding = self.course.get("skillsfuture", {})
        if not funding:
            return "Awaiting staff confirmation"
        if funding.get("basic_tier_accepted") and not funding.get(
            "mid_career_tier_accepted"
        ):
            return (
                "Basic-tier SkillsFuture Credits are accepted; Mid-Career "
                "SkillsFuture Credits cannot be used."
            )
        return funding.get("customer_reply") or "Awaiting staff confirmation"

    @property
    def approved_intakes(self):
        return [
            self._normalise_intake(item, is_demo=False, index=index)
            for course in self.courses
            for index, item in enumerate(course.get("intakes", []), start=1)
        ]

    @property
    def demo_intakes(self):
        return [
            self._normalise_intake(item, is_demo=True, index=index)
            for course in self.courses
            for index, item in enumerate(course.get("demo_intakes", []), start=1)
        ]

    @property
    def intakes(self):
        if self._intake_provider is not None:
            items = [self._normalise_intake(item, bool(item.get("is_demo")), index)
                     for index, item in enumerate(self._intake_provider(), start=1)]
            return [item for item in items if self.enable_demo_intakes or not item["is_demo"]]
        return [
            *self.approved_intakes,
            *(self.demo_intakes if self.enable_demo_intakes else []),
        ]

    @property
    def selectable_intakes(self):
        return [item for item in self.intakes if item["status"] == "open"]

    def selectable_intakes_for(self, course_value):
        course_id = self.course_id_for(course_value)
        return [
            item
            for item in self.selectable_intakes
            if item.get("course_id") == course_id
        ]

    @property
    def intake_dates(self):
        return [item["start_date"] for item in self.selectable_intakes]

    @property
    def has_intake_dates(self):
        return bool(self.intake_dates)

    @property
    def has_demo_intakes(self):
        return any(item["is_demo"] for item in self.selectable_intakes)

    @property
    def demo_intake_notice(self):
        return self.course.get(
            "demo_intake_notice",
            "Demo intake dates — prototype only. These dates require staff confirmation.",
        )

    @property
    def course_selection_intake_display(self):
        if not self.has_intake_dates:
            return "Awaiting staff confirmation"
        if self.has_demo_intakes:
            return "Available dates shown below"
        return ", ".join(item["display_date"] for item in self.selectable_intakes)

    def intake_choices(self, course_value=None):
        choices = (
            self.selectable_intakes_for(course_value)
            if course_value
            else self.selectable_intakes
        )
        return "\n".join(
            f"{index}. {item['display_date']} — {item['intake_type']}"
            for index, item in enumerate(choices, start=1)
        )

    def intake_for_start_date(self, value):
        key = value.isoformat() if isinstance(value, date) else str(value or "")
        return next(
            (item for item in self.intakes if item["start_date"] == key), None
        )

    def intake_for_id(self, intake_id):
        return next(
            (item for item in self.intakes if item["id"] == intake_id), None
        )

    def match_intake_selection(self, value, course_value=None):
        """Resolve a numbered, dated or natural-language intake selection."""
        if not self.intakes:
            return None, self.no_intakes_message

        supplied = _normalise_intake_text(value)
        available = (
            self.selectable_intakes_for(course_value)
            if course_value
            else self.selectable_intakes
        )
        number = re.fullmatch(r"\d+", supplied)
        if number:
            position = int(number.group())
            if 1 <= position <= len(available):
                return available[position - 1], None
            return None, self._intake_clarification(
                "Please choose one of the available intake numbers."
            )

        ordinal = re.search(
            r"\b(first|second|third)\s+(?:available\s+)?(?:intake|option)\b",
            supplied,
        )
        if ordinal:
            position = {"first": 1, "second": 2, "third": 3}[ordinal.group(1)]
            if position <= len(available):
                return available[position - 1], None

        exact = []
        eligible_intakes = (
            [item for item in self.intakes if item.get("course_id") == self.course_id_for(course_value)]
            if course_value
            else self.intakes
        )
        for item in eligible_intakes:
            aliases = {
                _normalise_intake_text(item["id"]),
                _normalise_intake_text(item["start_date"]),
                _normalise_intake_text(item["display_date"]),
                _normalise_intake_text(
                    f"{item['start_date']} to {item['end_date']}"
                ),
            }
            if supplied in aliases or any(alias and alias in supplied for alias in aliases):
                exact.append(item)

        matches = exact or self._natural_intake_matches(supplied, eligible_intakes)
        unique = {item["id"]: item for item in matches}
        if len(unique) > 1:
            return None, self._intake_clarification(
                "That choice could refer to more than one intake."
            )
        if len(unique) == 1:
            selected = next(iter(unique.values()))
            if selected["status"] == "closed":
                return None, self._intake_clarification(
                    f"The {selected['display_date']} intake is closed."
                )
            if selected["status"] == "full":
                return None, self._intake_clarification(
                    f"The {selected['display_date']} intake is full."
                )
            if selected["status"] == "cancelled":
                return None, self._intake_clarification(
                    f"The {selected['display_date']} intake is cancelled."
                )
            if selected["status"] == "historical":
                return None, self._intake_clarification(
                    f"The {selected['display_date']} intake has already started."
                )
            if selected["status"] != "open":
                return None, self._intake_clarification(
                    f"The {selected['display_date']} intake is unavailable."
                )
            return selected, None
        return None, self._intake_clarification(
            "That date is not one of the available intakes."
        )

    def _natural_intake_matches(self, supplied, intakes=None):
        month_names = (
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        )
        mentioned_months = {
            index for index, name in enumerate(month_names, start=1) if name in supplied
        }
        mentioned_types = {
            kind for kind in ("weekend", "weekday") if kind in supplied
        }
        if not mentioned_months and not mentioned_types:
            return []
        day_match = re.search(r"\b([0-3]?\d)(?:st|nd|rd|th)?\b", supplied)
        year_match = re.search(r"\b(20\d{2})\b", supplied)
        day = int(day_match.group(1)) if day_match else None
        year = int(year_match.group(1)) if year_match else None
        matches = []
        for item in intakes or self.intakes:
            start = date.fromisoformat(item["start_date"])
            if mentioned_months and start.month not in mentioned_months:
                continue
            if mentioned_types and item["intake_type"].casefold() not in mentioned_types:
                continue
            if day is not None and start.day != day:
                continue
            if year is not None and start.year != year:
                continue
            matches.append(item)
        return matches

    def _intake_clarification(self, reason):
        if not self.selectable_intakes:
            return f"{reason} No open intake is currently available. Staff confirmation is required."
        return f"{reason}\n\nAvailable choices:\n{self.intake_choices()}"

    def _validate_intakes(self):
        ids = set()
        for item in [*self.approved_intakes, *self.demo_intakes]:
            if not item["id"] or item["id"] in ids:
                raise ValueError("Every intake must have a unique stable ID.")
            ids.add(item["id"])
            start = date.fromisoformat(item["start_date"])
            end = date.fromisoformat(item["end_date"])
            course = self.course_for(item.get("course_id"))
            if course is None:
                raise ValueError("Every intake must belong to a current canonical course.")
            one_day = str(course.get("duration", "")).casefold().startswith("one")
            expected_end = start if one_day else start + timedelta(days=1)
            if end != expected_end:
                raise ValueError(
                    "Intake dates must match the configured course duration."
                )
            if item in self.demo_intakes and not item["is_demo"]:
                raise ValueError("Every demo intake must be marked is_demo=true.")

    @staticmethod
    def _normalise_intake(item, is_demo, index):
        start = item.get("start_date") or item.get("date")
        if not start:
            raise ValueError("Every intake must provide a start date.")
        end = item.get("end_date") or start
        start_date = date.fromisoformat(str(start))
        end_date = date.fromisoformat(str(end))
        return {
            "id": item.get("id") or f"approved-{start_date.isoformat()}-{index}",
            "course_id": item.get("course_id", "dental-assisting-basic"),
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "display_date": item.get("display_date")
            or item.get("display")
            or _display_intake_range(start_date, end_date),
            "status": str(item.get("status", "open")).casefold(),
            "is_demo": bool(item.get("is_demo", is_demo)),
            "start_time": item.get("start_time"),
            "end_time": item.get("end_time"),
            "intake_type": item.get("intake_type") or item.get("label") or (
                "Weekend" if start_date.weekday() >= 5 else "Weekday"
            ),
            "capacity": int(item.get("capacity", 20)),
            "participant_count": int(item.get("participant_count", 0)),
            "available_places": int(item.get("available_places", max(0, int(item.get("capacity", 20)) - int(item.get("participant_count", 0))))),
        }

    @property
    def no_intakes_message(self):
        return self.course["no_intakes_message"]

    @property
    def staff_confirmation_message(self):
        return self._data["reply_style"]["staff_confirmation_message"]

    @property
    def venue_customer_reply(self):
        return self.course["venue"]["customer_reply"]

    @property
    def venue_confirmation_required(self):
        return self.course["venue"]["confirmation_required"]

    @property
    def payment_question(self):
        return self.course.get("enrollment", {}).get("payment_question", "")

    def payment_question_for(self, course_value):
        course = self.course_for(course_value) or self.course
        return course.get("enrollment", {}).get(
            "payment_question", self.payment_question
        )

    def skillsfuture_display_for(self, course_value):
        course = self.course_for(course_value)
        if course is None:
            return "Awaiting staff confirmation"
        return course.get("skillsfuture", {}).get(
            "customer_reply", "Awaiting staff confirmation"
        )

    @property
    def enrollment_templates(self):
        return self.course["enrollment"]["reply_templates"]

    def enrollment_reply(self, template_name, **values):
        """Render centrally maintained enrolment wording with validated values."""
        return self.enrollment_templates[template_name].format(**values)

    def enrollment_field_label(self, field):
        return self.course["enrollment"]["field_labels"].get(
            field, field.replace("_", " ").title()
        )

    @property
    def enrollment_faq_resume_prompt(self):
        return self.enrollment_templates["faq_resume"]

    def agent_context(self):
        context = deepcopy(self._data)
        for course in [context["course"], *context.get("additional_courses", [])]:
            course_id = course.get("id")
            course["intakes"] = [
                item for item in self.intakes if item.get("course_id") == course_id
            ]
            course.pop("demo_intakes", None)
            course.pop("demo_intake_notice", None)
        context["demo_intakes_enabled"] = self.enable_demo_intakes
        return json.dumps(context, indent=2, ensure_ascii=False)

    def approved_faq_reply(self, topics):
        """Compose only staff-approved FAQ copy selected by the FAQ agent."""
        answers = {
            item["topic"].casefold(): item["answer"] for item in self._data["faqs"]
        }
        selected = []
        for topic in topics or []:
            normalized_topic = str(topic).strip().casefold()
            if normalized_topic == "course dates":
                answer = self._course_dates_reply()
            else:
                answer = answers.get(normalized_topic)
            if answer and answer not in selected:
                selected.append(answer)
        return "\n\n".join(selected) if selected else self.staff_confirmation_message

    def course_overview_reply(self, course_value):
        course = self.course_for(course_value)
        if not course:
            return self.staff_confirmation_message
        lines = [
            f"📚 {course['name']}",
            "",
            f"- Course fee: {course['fee']['display']}",
            f"- Duration: {course['duration']}",
        ]
        if course.get("training_time"):
            lines.append(f"- Training hours: {course['training_time']}")
        else:
            lines.append("- Training hours: Awaiting staff confirmation")
        intakes = self.selectable_intakes_for(course["id"])
        if intakes:
            lines.append(
                "- Upcoming intakes: "
                + "; ".join(
                    f"{item['display_date']} — {item['intake_type']}"
                    for item in intakes
                )
            )
        lines.append(
            "- Venue: "
            + course.get("venue", {}).get(
                "customer_reply", self.staff_confirmation_message
            )
        )
        return "\n".join(lines)

    def _course_dates_reply(self):
        if not self.has_intake_dates:
            return self.no_intakes_message
        sections = ["📅 Available Intake Dates"]
        for course in self.courses:
            sections.extend(["", course["name"]])
            sections.extend(
                f"{index}. {item['display_date']} — {item['intake_type']} — Open"
                for index, item in enumerate(
                    self.selectable_intakes_for(course["id"]), start=1
                )
            )
        return "\n".join(sections)

    def ground_faq_answer(self, answer):
        """Reject high-risk numeric claims that are absent from approved content."""
        candidate = (answer or "").strip()
        if not candidate:
            return self.staff_confirmation_message

        approved_content = self.agent_context()
        allowed_money = {
            amount
            for amount in (_money_value(match) for match in MONEY_PATTERN.findall(approved_content))
            if amount is not None
        }
        if self.course_fee is not None:
            allowed_money.add(self.course_fee)
        claimed_money = {
            amount
            for amount in (_money_value(match) for match in MONEY_PATTERN.findall(candidate))
            if amount is not None
        }
        if not claimed_money.issubset(allowed_money):
            return self.staff_confirmation_message

        approved_uens = {value.upper() for value in UEN_PATTERN.findall(approved_content)}
        claimed_uens = {value.upper() for value in UEN_PATTERN.findall(candidate)}
        if not claimed_uens.issubset(approved_uens):
            return self.staff_confirmation_message

        if not self.has_intake_dates and CALENDAR_DATE_PATTERN.search(candidate):
            return self.staff_confirmation_message

        return candidate

    def public_summary(self):
        return deepcopy(self._data)


def _money_value(value):
    try:
        return Decimal(value.replace(",", "")).quantize(Decimal("0.01"))
    except (InvalidOperation, AttributeError):
        return None


def _normalise_intake_text(value):
    return " ".join(
        str(value or "")
        .casefold()
        .replace("–", "-")
        .replace("—", "-")
        .replace(",", " ")
        .split()
    ).strip(" .")


def _display_intake_range(start, end):
    if start == end:
        return f"{start.day} {start.strftime('%B %Y')}"
    if start.year == end.year and start.month == end.month:
        return f"{start.day}–{end.day} {start.strftime('%B %Y')}"
    if start.year == end.year:
        return f"{start.day} {start.strftime('%B')}–{end.day} {end.strftime('%B %Y')}"
    return (
        f"{start.day} {start.strftime('%B %Y')}–"
        f"{end.day} {end.strftime('%B %Y')}"
    )
