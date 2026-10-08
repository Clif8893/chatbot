"""Application-controlled, staged enrollment state machine."""

import re
from datetime import date
from decimal import Decimal

from backend.extensions import db
from backend.models import Conversation, EnrollmentDraft, utc_now
from backend.services.enrollment.enrollment_parser import (
    is_explicit_correction,
    parse_enrollment_fields,
    redact_identity_for_ai,
)
from backend.services.privacy import mask_nric
from backend.services.enrollment.validation import validate_field
from backend.tools.enrollment import build_enrollment_tools


IDENTITY_FIELDS = (
    "full_name",
    "nric",
    "date_of_birth",
    "email",
    "mobile_number",
)
ENROLLMENT_FIELDS = (
    "course",
    "preferred_intake_date",
    *IDENTITY_FIELDS,
)
FINAL_STATUSES = {"confirmed", "awaiting_course_date"}
INTAKE_SELECTION_FOLLOWUP = "intake_selection"


class EnrollmentService:
    def __init__(
        self,
        ai_service,
        catalogue,
        repository,
        context_limit=20,
        intake_service=None,
        validation_options=None,
    ):
        self.ai_service = ai_service
        self.catalogue = catalogue
        self.repository = repository
        self.context_limit = context_limit
        self.intake_service = intake_service
        self.validation_options = dict(validation_options or {})

    def handle(self, conversation, message, context, last_assistant_message=None):
        draft = self.repository.get_or_create_draft(conversation)
        self._hydrate_structured_intake(draft)
        tools = build_enrollment_tools(
            self.repository,
            self.catalogue,
            conversation,
            self.context_limit,
            self.missing_fields,
            self.validation_options,
        )
        extraction = self.ai_service.extract_enrollment(
            message=redact_identity_for_ai(message),
            context=context,
            draft_state=self._draft_state(draft),
            tools=tools,
        )
        extracted_fields = extraction.extracted_fields.model_dump(exclude_none=True)
        # Known forms and structured values are resolved deterministically. These
        # values take precedence over model interpretation and still pass through
        # the same validators below.
        parsed_fields = parse_enrollment_fields(message)
        extracted_fields.update(parsed_fields)
        for field, value in self._bare_stage_value(
            draft, message, last_assistant_message
        ).items():
            if field not in parsed_fields:
                extracted_fields[field] = value
        supplied_fields = self._newly_supplied_fields(
            draft, message, extracted_fields
        )
        correction_requested = (
            extraction.correction_requested or is_explicit_correction(message)
        )
        if _is_retired_payment_only(message):
            correction_requested = False

        if draft.status in FINAL_STATUSES and not supplied_fields:
            return self._already_confirmed_reply(draft), draft

        invalid = []
        saved = {}

        course_value = supplied_fields.pop("course", None)
        course_is_selected = self._course_is_selected(
            draft, message, extraction.confirmation, supplied_fields
        )
        if course_value is not None:
            normalized, error = validate_field(
                "course", course_value, self.catalogue, **self.validation_options
            )
            if error:
                invalid.append(("course", error))
            else:
                self._save_course(draft, normalized)
                saved["course"] = normalized
        elif draft.course is None and any(
            field in supplied_fields
            for field in (*IDENTITY_FIELDS, "preferred_intake_date")
        ):
            self._save_course(draft, self.catalogue.course_name)
            saved["course"] = draft.course
        elif draft.course is None and "this course" in message.casefold():
            self._save_course(draft, self.catalogue.course_name)
            saved["course"] = draft.course

        if draft.course is None:
            draft.status = "collecting"
            if invalid:
                return self._outcome_reply(draft, saved, invalid), draft
            return self._course_selection_reply(), draft

        intake_value = supplied_fields.get("preferred_intake_date")
        if (
            intake_value is not None
            and not self.catalogue.has_intake_dates
            and _is_awaiting_intake_confirmation(intake_value)
        ):
            supplied_fields.pop("preferred_intake_date")

        for field in ENROLLMENT_FIELDS:
            if field == "course" or field not in supplied_fields:
                continue
            normalized, error = validate_field(
                field,
                supplied_fields[field],
                self.catalogue,
                course_value=draft.course,
                **self.validation_options,
            )
            if error:
                invalid.append((field, error))
            else:
                saved[field] = normalized

        persisted_updates = {
            key: value for key, value in saved.items() if key != "course"
        }
        if persisted_updates:
            self.repository.update_draft(
                draft, persisted_updates
            )
        if "preferred_intake_date" in saved:
            selected, _ = self.catalogue.match_intake_selection(
                supplied_fields["preferred_intake_date"], draft.course
            )
            if selected:
                self._save_intake(draft, selected)
        self._update_validation_errors(draft, saved, invalid)
        db.session.flush()
        db.session.refresh(draft)

        if (saved or invalid) and draft.status in {
            "awaiting_confirmation",
            "awaiting_course_date",
            "confirmed",
            "cancelled",
        }:
            draft.status = "collecting"
            draft.confirmed_at = None

        stored_invalid = self._stored_validation_errors(draft)
        if stored_invalid:
            self._update_validation_errors(draft, {}, stored_invalid)

        active_invalid = list((draft.validation_errors or {}).items())
        if active_invalid:
            draft.status = "collecting"
            self._set_validation_state(
                draft, "needs_correction", [field for field, _ in active_invalid]
            )
            return self._outcome_reply(draft, saved, active_invalid), draft

        missing = self.missing_fields(draft, self.catalogue)
        if missing:
            draft.status = "collecting"
            self._set_validation_state(draft, "incomplete", [])
            return self._progress_reply(draft, saved, missing), draft

        duplicate_error = self._duplicate_enrollment_error(draft)
        if duplicate_error:
            draft.status = "duplicate_review"
            self._set_validation_state(draft, "staff_review", ["duplicate_enrollment"])
            return duplicate_error, draft

        if (
            extraction.confirmation == "confirm"
            and draft.status == "awaiting_confirmation"
        ):
            intake_error = self._selected_intake_error(draft)
            if intake_error:
                self._clear_intake(draft)
                draft.status = "collecting"
                return self._outcome_reply(
                    draft, {}, [("preferred_intake_date", intake_error)]
                ), draft
            if self.intake_service is not None and draft.intake_id:
                try:
                    self.intake_service.allocate(draft)
                except ValueError as error:
                    draft.status = "collecting"
                    self._set_validation_state(
                        draft, "needs_correction", ["preferred_intake_date"]
                    )
                    return self._outcome_reply(
                        draft, {}, [("preferred_intake_date", str(error))]
                    ), draft
            draft.status = (
                "confirmed"
                if draft.preferred_intake_date is not None
                else "awaiting_course_date"
            )
            draft.confirmed_at = utc_now()
            self._ensure_friendly_reference(draft)
            self._set_validation_state(draft, "valid", [])
            return self._confirmed_reply(draft), draft

        if extraction.confirmation == "reject":
            draft.status = "collecting"
            draft.confirmed_at = None
            return self.catalogue.enrollment_reply(
                "rejected", opening=self._natural_name_opening(draft, "No problem")
            ), draft

        if correction_requested and not supplied_fields:
            draft.status = "collecting"
            return self.catalogue.enrollment_reply(
                "correction_request", name_prefix=self._name_prefix(draft)
            ), draft

        draft.status = "awaiting_confirmation"
        self._set_validation_state(draft, "valid", [])
        summary = self._confirmation_summary(draft)
        if correction_requested and saved:
            summary = (
                self._correction_acknowledgement(draft, saved) + "\n\n" + summary
            )
        return summary, draft

    @classmethod
    def missing_fields(cls, draft, catalogue=None):
        if draft is None:
            return []
        if draft.course is None:
            return ["course"]
        if draft.course_fee is None:
            return ["course_fee"]
        if catalogue is not None and catalogue.has_intake_dates:
            if draft.preferred_intake_date is None:
                return ["preferred_intake_date"]

        identity_missing = [
            field for field in IDENTITY_FIELDS if getattr(draft, field) is None
        ]
        return identity_missing

    def next_step_prompt(self, draft):
        """Return a short reminder for deterministic greeting interruptions."""
        missing = self.missing_fields(draft, self.catalogue)
        if not missing:
            if draft.status == "awaiting_confirmation":
                return "Please review the summary and reply Confirm, Correct [field], or Cancel."
            return self._status_wording(draft)
        first = missing[0]
        if first == "course":
            return "Please choose a course by number or course name."
        if first == "course_fee":
            return "The course fee requires staff confirmation before enrolment can continue."
        if first == "preferred_intake_date":
            return "Please select one of the available course intakes."
        return f"I still need your {self.catalogue.enrollment_field_label(first).casefold()}."

    def progress_reply(self, draft):
        """Render authoritative enrollment progress without calling an LLM."""
        name = _first_name(draft.full_name)
        sections = [f"Hi {name} 👋" if name else "Hi 👋", "Here is your current enrollment progress:"]

        saved_items = []
        for field in IDENTITY_FIELDS:
            value = getattr(draft, field)
            if value is None:
                continue
            if field == "nric":
                display = mask_nric(value)
            elif field == "date_of_birth":
                display = _display_date(value)
            else:
                display = str(value)
            saved_items.append(
                f"- {self.catalogue.enrollment_field_label(field)}: {display}"
            )
        if draft.intake_id and draft.preferred_intake_date:
            saved_items.append(
                f"- {self.catalogue.enrollment_field_label('preferred_intake_date')}: "
                f"{self._intake_display(draft)}"
            )
        if saved_items:
            sections.append("✅ Saved Information\n\n" + "\n".join(saved_items))

        errors = dict(draft.validation_errors or {})
        if errors:
            error_items = "\n".join(
                f"- {self.catalogue.enrollment_field_label(field)}: {message}"
                for field, message in errors.items()
            )
            sections.append("⚠️ Needs Correction\n\n" + error_items)

        missing = [
            field for field in self.all_missing_fields(draft)
            if field not in errors
        ]
        if missing:
            missing_items = "\n".join(
                f"- {self.catalogue.enrollment_field_label(field)}"
                for field in missing
            )
            sections.append("📋 Still Needed\n\n" + missing_items)
        if "preferred_intake_date" in missing and self.catalogue.has_intake_dates:
            sections.append(self._intake_selection_reply(draft))

        if missing:
            sections.append(
                "You may send the remaining information together or one item at a time."
            )
        sections.append(self._status_wording(draft))
        return "\n\n".join(sections)

    def is_contextual_stage_reply(self, draft, message, last_assistant_message):
        if draft is None or not last_assistant_message:
            return False
        return bool(self._bare_stage_value(draft, message, last_assistant_message))

    def remember_intake_choices(self, conversation, draft):
        """Persist the exact numbered choices shown for this conversation."""
        if (
            conversation.pending_action is not None
            or draft is None
            or draft.course is None
            or draft.preferred_intake_date is not None
            or not self.catalogue.has_intake_dates
        ):
            if conversation.pending_followup == INTAKE_SELECTION_FOLLOWUP:
                conversation.pending_followup = None
                conversation.pending_intake_options = None
            return False
        conversation.pending_followup = INTAKE_SELECTION_FOLLOWUP
        conversation.pending_intake_options = [
            item["id"] for item in self.catalogue.selectable_intakes_for(draft.course)
        ]
        return bool(conversation.pending_intake_options)

    def handle_pending_intake_selection(self, conversation, message):
        """Resolve a clear choice against only the stored displayed intake list."""
        draft = conversation.enrollment_draft
        waiting = conversation.pending_followup == INTAKE_SELECTION_FOLLOWUP
        changing = bool(
            draft
            and draft.preferred_intake_date is not None
            and not draft.confirmed_at
            and _looks_like_intake_change(message)
        )
        if (
            not (waiting or changing)
            or conversation.pending_action is not None
            or not conversation.pending_intake_options
            or not _looks_like_pending_intake_selection(message)
        ):
            return None
        if draft is None or draft.course is None:
            conversation.pending_followup = None
            conversation.pending_intake_options = None
            return None

        option_ids = list(conversation.pending_intake_options)
        selected, error = self._match_pending_intake(option_ids, message, draft.course)
        if selected is None:
            self.remember_intake_choices(conversation, draft)
            return f"{error}\n\n{self._intake_selection_reply(draft)}", draft

        intake_error = self._intake_availability_error(selected, draft)
        if intake_error:
            self.remember_intake_choices(conversation, draft)
            return f"{intake_error}\n\n{self._intake_selection_reply(draft)}", draft

        self._save_intake(draft, selected)
        errors = dict(draft.validation_errors or {})
        errors.pop("preferred_intake_date", None)
        draft.validation_errors = errors
        draft.status = "collecting"
        draft.confirmed_at = None
        conversation.pending_followup = None
        conversation.pending_intake_options = option_ids
        db.session.flush()
        return (
            self._progress_reply(
                draft,
                {"preferred_intake_date": draft.preferred_intake_date},
                self.missing_fields(draft, self.catalogue),
            ),
            draft,
        )

    def _match_pending_intake(self, option_ids, message, course=None):
        normalized = " ".join(str(message or "").casefold().split())
        number_match = re.fullmatch(r"(?:option\s*)?(\d+)", normalized)
        if number_match:
            position = int(number_match.group(1))
            if 1 <= position <= len(option_ids):
                selected = self.catalogue.intake_for_id(option_ids[position - 1])
                if selected:
                    return selected, None
            return None, "Please choose one of the displayed intake numbers."

        ordinal_match = re.search(
            r"\b(?:the\s+)?(first|second|third)(?:\s+(?:option|intake|one))?\b",
            normalized,
        )
        if ordinal_match:
            position = {"first": 1, "second": 2, "third": 3}[
                ordinal_match.group(1)
            ]
            if position <= len(option_ids):
                selected = self.catalogue.intake_for_id(option_ids[position - 1])
                if selected:
                    return selected, None
            return None, "Please choose one of the displayed intake numbers."

        if "last" in normalized and "weekend" in normalized:
            weekends = [
                self.catalogue.intake_for_id(intake_id) for intake_id in option_ids
            ]
            weekends = [
                item
                for item in weekends
                if item and item.get("intake_type", "").casefold() == "weekend"
            ]
            if weekends:
                return weekends[-1], None

        selected, error = self.catalogue.match_intake_selection(message, course)
        if selected and selected["id"] in option_ids:
            return selected, None
        if selected:
            return None, "That choice was not in the intake list shown for this conversation."
        return None, error or "Please choose one of the displayed intake numbers or dates."

    def _intake_availability_error(self, intake, draft):
        if self.intake_service is not None:
            error = self.intake_service.allocation_error(intake["id"], draft)
            if error:
                return error
        status = intake.get("status")
        if status == "open":
            return None
        return (
            f"The {intake['display_date']} intake is {status or 'unavailable'}. "
            "Please choose another intake."
        )

    def all_missing_fields(self, draft):
        missing = []
        if draft.course is None:
            missing.append("course")
        if draft.course_fee is None:
            missing.append("course_fee")
        if self.catalogue.has_intake_dates and draft.preferred_intake_date is None:
            missing.append("preferred_intake_date")
        missing.extend(
            field for field in IDENTITY_FIELDS if getattr(draft, field) is None
        )
        return missing

    @staticmethod
    def _status_wording(draft):
        if draft.status == "confirmed":
            return "Your enrollment has been confirmed."
        if draft.status == "awaiting_course_date":
            return "Your details are confirmed, and the course date is awaiting staff confirmation."
        if draft.status == "awaiting_confirmation":
            return "All required details have been collected. Please review and confirm them."
        if draft.status == "cancelled":
            return "This enrollment has been cancelled."
        if draft.validation_errors:
            return "Your enrollment draft has been saved and requires the corrections shown above."
        return "Your enrollment draft has been saved, but some information is still required."

    def _course_selection_reply(self):
        primary = self.catalogue.course
        return (
            "I’d be happy to help you enrol 😊\n\n"
            "Please choose a course:\n\n"
            f"{self.catalogue.course_choices()}\n\n"
            "Please reply with the course number or course name.\n\n"
            "Enrollment Started\n\n"
            f"For the {primary['name']}:\n"
            f"- Course fee: {self.catalogue.course_fee_display_for(primary)}\n"
            f"- Duration: {primary['duration'].capitalize()}\n"
            "- Available intake: Awaiting staff confirmation\n\n"
            "Once you choose, I’ll collect the required details, including:\n"
            "Full name as shown on NRIC:\nNRIC/FIN:\nDate of birth:\nEmail address:\nMobile number:\n"
            "Preferred intake/date:"
        )

    def _selected_course_intro(self, course_value):
        course = self.catalogue.course_for(course_value) or self.catalogue.course
        code = course.get("code")
        lines = [
            "📚 Enrollment Started",
            "",
            f"- Course: {course['name']}",
        ]
        if code:
            lines.append(f"- Course code: {code}")
        lines.extend(
            (
                f"- Course fee: {self.catalogue.course_fee_display_for(course_value)}",
                f"- Duration: {course['duration'].capitalize()}",
                f"- Available intake: {', '.join(item['display_date'] for item in self.catalogue.selectable_intakes_for(course_value))}",
            )
        )
        return "\n".join(lines)

    def _progress_reply(self, draft, saved, missing):
        if missing == ["course_fee"]:
            return self._prepend_saved(
                saved, self.catalogue.enrollment_reply("course_fee_unavailable")
            )
        if missing == ["preferred_intake_date"]:
            return self._prepend_saved(
                saved,
                self._intake_selection_reply(draft),
            )
        if any(field in IDENTITY_FIELDS for field in missing):
            missing_identity = [field for field in missing if field in IDENTITY_FIELDS]
            if set(saved).issubset({"course", "preferred_intake_date"}):
                return self._prepend_saved(
                    saved,
                    self.catalogue.enrollment_reply(
                        "participant_details",
                        intake_notice=(
                            self.catalogue.no_intakes_message + "\n\n"
                            if not self.catalogue.has_intake_dates
                            else ""
                        ),
                        missing_template=self._initial_enrollment_template(draft),
                    ),
                )
            if saved:
                return self._outcome_reply(draft, saved, [])
            return self.catalogue.enrollment_reply(
                "participant_details",
                intake_notice=(
                    self.catalogue.no_intakes_message + "\n\n"
                    if not self.catalogue.has_intake_dates
                    else ""
                ),
                missing_template=self._participant_template(missing_identity),
            )
        return self._prepend_saved(saved, self._confirmation_summary(draft))

    def _outcome_reply(self, draft, saved, invalid):
        sections = []
        if saved:
            saved_items = "\n".join(
                f"- {self.catalogue.enrollment_field_label(field)}: "
                f"{self._safe_display(field, value)}"
                for field, value in saved.items()
            )
            sections.append(
                self.catalogue.enrollment_reply(
                    "outcome_saved", saved_items=saved_items
                )
            )
        if invalid:
            invalid_items = "\n".join(
                f"- {self.catalogue.enrollment_field_label(field)}: {error}"
                for field, error in invalid
            )
            sections.append(
                self.catalogue.enrollment_reply(
                    "outcome_invalid", invalid_items=invalid_items
                )
            )

        missing = self.missing_fields(draft, self.catalogue)
        invalid_fields = {field for field, _ in invalid}
        still_required = [field for field in missing if field not in invalid_fields]
        if still_required:
            missing_items = "\n".join(
                f"- {self.catalogue.enrollment_field_label(field)}"
                for field in still_required
            )
            sections.append(
                self.catalogue.enrollment_reply(
                    "outcome_missing", missing_items=missing_items
                )
            )
        if "preferred_intake_date" in still_required and self.catalogue.has_intake_dates:
            sections.append(self._intake_selection_reply(draft))

        if len(invalid) == 1 and invalid[0][0] == "nric":
            closing = self.catalogue.enrollment_reply("correct_nric_only")
        elif invalid and "preferred_intake_date" in still_required:
            closing = self.catalogue.enrollment_reply("correct_and_choose_intake")
        elif invalid:
            closing = self.catalogue.enrollment_reply("correct_fields")
        elif still_required and all(field in IDENTITY_FIELDS for field in still_required):
            closing = self.catalogue.enrollment_reply(
                "continue_participant_details",
                missing_template=self._participant_template(still_required),
            )
        elif still_required:
            closing = self.next_step_prompt(draft)
        else:
            closing = self._confirmation_summary(draft)
            draft.status = "awaiting_confirmation"
        return "\n\n".join([*sections, closing])

    def _intake_selection_reply(self, draft=None):
        return self.catalogue.enrollment_reply(
            "intake_selection",
            demo_heading="",
            intake_description="an available intake",
            intake_choices=self.catalogue.intake_choices(draft.course if draft else None),
            demo_footer="",
        )

    def _confirmation_summary(self, draft):
        course = self.catalogue.course_for(draft.course_id or draft.course) or self.catalogue.course
        code = course.get("code")
        return self.catalogue.enrollment_reply(
            "confirmation",
            course_name=draft.course,
            course_code_line=f"\n- Course code: {code}" if code else "",
            course_fee=self._course_fee_display(draft),
            duration=course["duration"].capitalize(),
            training_time=course["training_time"],
            intake_label="Preferred intake",
            preferred_intake=self._intake_display(draft),
            demo_note="",
            full_name=draft.full_name,
            masked_nric=mask_nric(draft.nric),
            date_of_birth=_display_date(draft.date_of_birth),
            masked_email=_mask_email(draft.email),
            masked_mobile=_mask_mobile(draft.mobile_number),
        )

    def _confirmed_reply(self, draft):
        name = _first_name(draft.full_name) or "there"
        if draft.status == "awaiting_course_date":
            return (
                f"Thank you, {name}! Your enrolment details have been recorded successfully ✅\n\n"
                f"🧾 Enrollment reference\nReference: {self._friendly_reference(draft)}\n\n"
                "Our team will contact you once the upcoming intake date and exact "
                "training venue are confirmed."
                + self._confirmed_next_actions()
            )
        intake = self._intake_display(draft)
        selected_intake = self.catalogue.intake_for_id(draft.intake_id) if draft.intake_id else None
        if selected_intake and selected_intake.get("intake_type"):
            intake += f" - {selected_intake['intake_type']}"
        reply = (
            f"Thank you, {name}! Your enrollment has been confirmed successfully ✅\n\n"
            f"📚 Course\n{draft.course}\n\n"
            f"📅 Intake\n{intake}\n\n"
            f"🧾 Enrollment reference\nReference: {self._friendly_reference(draft)}\n\n"
            "Your invoice is being prepared and will be emailed to your registered "
            "email address. You can also download it from Enrollment & invoice."
        )
        return reply + self._confirmed_next_actions()

    def _already_confirmed_reply(self, draft):
        if draft.status == "awaiting_course_date":
            return self.catalogue.enrollment_reply(
                "already_awaiting_course_date",
                first_name=_first_name(draft.full_name),
            )
        return self.catalogue.enrollment_reply(
            "already_confirmed", first_name=_first_name(draft.full_name)
        )

    def _save_course(self, draft, course_name):
        switching = draft.course is not None and draft.course != course_name
        course = self.catalogue.course_for(course_name)
        draft.course = course_name
        draft.course_id = course.get("id") if course else None
        draft.course_fee = self.catalogue.course_fee_for(course_name)
        if switching:
            self._clear_intake(draft)

    @staticmethod
    def _clear_intake(draft):
        draft.preferred_intake_date = None
        draft.intake_id = None
        draft.intake_start_date = None
        draft.intake_end_date = None
        draft.intake_is_demo = None

    def _save_intake(self, draft, intake):
        selected_course = self.catalogue.course_for(intake.get("course_id"))
        if selected_course is None:
            raise ValueError("The selected intake is not linked to an available course.")
        if draft.course_id and draft.course_id != selected_course["id"]:
            raise ValueError("The selected intake does not belong to the selected course.")
        draft.course_id = selected_course["id"]
        draft.course = selected_course["name"]
        draft.course_fee = self.catalogue.course_fee_for(selected_course)
        draft.intake_id = intake["id"]
        draft.intake_start_date = date.fromisoformat(intake["start_date"])
        draft.intake_end_date = date.fromisoformat(intake["end_date"])
        draft.intake_is_demo = bool(intake["is_demo"])
        draft.preferred_intake_date = draft.intake_start_date

    def _hydrate_structured_intake(self, draft):
        if draft.preferred_intake_date is None or draft.intake_start_date is not None:
            return
        intake = self.catalogue.intake_for_start_date(draft.preferred_intake_date)
        if intake:
            self._save_intake(draft, intake)

    def _selected_intake_error(self, draft):
        if draft.preferred_intake_date is None:
            return None
        if self.intake_service is not None and draft.intake_id:
            return self.intake_service.allocation_error(draft.intake_id, draft)
        intake = (
            self.catalogue.intake_for_id(draft.intake_id)
            if draft.intake_id
            else self.catalogue.intake_for_start_date(draft.preferred_intake_date)
        )
        if intake is None:
            return "The selected intake is no longer available. Please choose another intake."
        selected_course_id = self.catalogue.course_id_for(draft.course_id or draft.course)
        if selected_course_id and intake.get("course_id") != selected_course_id:
            return "The selected intake does not belong to the selected course. Please choose another intake."
        if intake["status"] == "closed":
            return f"The {intake['display_date']} intake is closed. Please choose another intake."
        if intake["status"] != "open":
            return f"The {intake['display_date']} intake is unavailable. Please choose another intake."
        return None

    def _intake_display(self, draft):
        if draft.intake_start_date and draft.intake_end_date:
            return _display_intake_range(
                draft.intake_start_date, draft.intake_end_date
            )
        return _display_date(draft.preferred_intake_date)

    def _course_is_selected(self, draft, message, confirmation, supplied):
        if draft.course is not None:
            return True
        lowered = " ".join(message.casefold().split())
        explicit = any(
            phrase in lowered
            for phrase in (
                "this course",
                "select the course",
                "choose the course",
                "enrol me in",
                "enroll me in",
                "want to enrol",
                "want to enroll",
                "want to apply",
                "sign me up",
                "like to join",
                "like to enrol",
                "like to enroll",
                self.catalogue.course_name.casefold(),
            )
        )
        # Supplying participant details after the only approved course was shown
        # also accepts that course, so the entered details are not discarded.
        has_details = any(
            field in supplied
            for field in (*IDENTITY_FIELDS, "preferred_intake_date")
        )
        return explicit or has_details or confirmation == "confirm"

    def _bare_stage_value(self, draft, message, last_assistant_message=None):
        """Map a safe bare reply to the one field the current stage expects."""
        stripped = message.strip()
        if draft.course is None and self.catalogue.match_course(stripped):
            return {"course": stripped}
        if (
            draft.course is not None
            and self.catalogue.has_intake_dates
            and (
                draft.preferred_intake_date is None
                or is_explicit_correction(message)
            )
        ):
            intake_cue = bool(
                re.search(
                    r"(?i)\b(?:intake|course\s+date|choose|select)\b", stripped
                )
                or re.fullmatch(r"\d+", stripped)
                or re.fullmatch(r"\d{4}-\d{2}-\d{2}", stripped)
                or re.fullmatch(
                    r"(?i)\d{1,2}(?:st|nd|rd|th)?(?:\s*[-–]\s*\d{1,2})?\s+"
                    r"(?:january|february|march|april|may|june|july|august|"
                    r"september|october|november|december)(?:\s+\d{4})?",
                    stripped,
                )
            )
            if intake_cue:
                return {"preferred_intake_date": stripped}
        missing = self.missing_fields(draft, self.catalogue)
        expected = missing[0] if missing else None
        if (
            expected in IDENTITY_FIELDS
            and _assistant_requested_field(last_assistant_message, expected)
            and _looks_like_bare_field_value(expected, stripped)
        ):
            return {expected: stripped}
        return {}

    def _newly_supplied_fields(self, draft, message, extracted_fields):
        supplied = {}
        pending_errors = draft.validation_errors or {}
        for field, value in extracted_fields.items():
            if field not in ENROLLMENT_FIELDS:
                continue
            if not EnrollmentService._usable_extracted_value(value):
                continue
            current_value = getattr(draft, field)
            if current_value is not None and EnrollmentService._values_equivalent(
                field, current_value, value
            ) and field not in pending_errors:
                continue
            if not self._message_supports_update(field, value, message):
                continue
            supplied[field] = value
        return supplied

    @staticmethod
    def _usable_extracted_value(value):
        if value is None:
            return False
        if not isinstance(value, str):
            return True
        normalized = value.strip()
        if not normalized:
            return False
        if "*" in normalized or "redacted" in normalized.casefold():
            return False
        return normalized.casefold() not in {"null", "none", "n/a", "unknown"}

    @staticmethod
    def _values_equivalent(field, current_value, extracted_value):
        if field in {"nric", "mobile_number"}:
            current = re.sub(r"\W", "", str(current_value)).casefold()
            extracted = re.sub(r"\W", "", str(extracted_value)).casefold()
            return current == extracted
        current = " ".join(str(current_value).casefold().split())
        extracted = " ".join(str(extracted_value).casefold().split())
        return current == extracted

    def _message_supports_update(self, field, value, message):
        lowered = message.casefold()
        value_text = " ".join(str(value).casefold().split())
        cues = {
            "course": ("course", "programme"),
            "full_name": ("name", "call me"),
            "nric": ("nric", "fin"),
            "date_of_birth": ("date of birth", "dob", "born"),
            "email": ("email", "e-mail"),
            "preferred_intake_date": ("intake", "course date", "preferred date"),
            "mobile_number": ("mobile", "phone", "handphone", "hp", "contact number"),
        }
        if field == "course":
            return self.catalogue.match_course(message) is not None or value_text in lowered or (
                "dental assist" in value_text and "dental assist" in lowered
            )
        if any(cue in lowered for cue in cues[field]):
            return True
        compact_message = re.sub(r"\W", "", lowered)
        compact_value = re.sub(r"\W", "", value_text)
        if field == "nric":
            return bool(
                re.fullmatch(r"[stfgm]\d{7}[a-z]", compact_value)
                and compact_value in compact_message
            )
        if field == "mobile_number":
            return len(compact_value) >= 8 and compact_value in compact_message
        if field == "email":
            return "@" in value_text and value_text in lowered
        if field in {"date_of_birth", "preferred_intake_date"}:
            if field == "preferred_intake_date" and (
                re.fullmatch(r"\s*\d+\s*", message)
                or re.search(r"(?i)\b(?:intake|course\s+date|choose|select)\b", message)
            ):
                return True
            return bool(
                re.search(
                    r"\b(?:19|20)\d{2}\b|\b(?:january|february|march|april|may|"
                    r"june|july|august|september|october|november|december)\b",
                    lowered,
                )
            )
        return len(value_text) >= 2 and value_text in lowered

    def _draft_state(self, draft):
        return {
            "course": draft.course,
            "course_fee": _amount_value(draft.course_fee),
            "full_name": "Provided" if draft.full_name else None,
            "nric": mask_nric(draft.nric),
            "date_of_birth": "Provided" if draft.date_of_birth else None,
            "email": _mask_email(draft.email) if draft.email else None,
            "preferred_intake_date": (
                draft.preferred_intake_date.isoformat()
                if draft.preferred_intake_date
                else None
            ),
            "intake_id": draft.intake_id,
            "intake_start_date": (
                draft.intake_start_date.isoformat()
                if draft.intake_start_date
                else None
            ),
            "intake_end_date": (
                draft.intake_end_date.isoformat()
                if draft.intake_end_date
                else None
            ),
            "intake_is_demo": draft.intake_is_demo,
            "mobile_number": _mask_mobile(draft.mobile_number) if draft.mobile_number else None,
            "status": draft.status,
            "validation_status": draft.validation_status,
            "pending_corrections": list(draft.pending_corrections or []),
            "validation_errors": dict(draft.validation_errors or {}),
        }

    def _participant_template(self, fields):
        return "\n".join(
            f"{self.catalogue.enrollment_field_label(field)}:" for field in fields
        )

    def _initial_enrollment_template(self, draft):
        intake = (
            self._intake_display(draft)
            if draft.preferred_intake_date
            else "Awaiting staff confirmation"
            if not self.catalogue.has_intake_dates
            else ""
        )
        values = {"preferred_intake_date": intake}
        fields = (
            "full_name",
            "nric",
            "date_of_birth",
            "email",
            "mobile_number",
            "preferred_intake_date",
        )
        return "\n".join(
            f"{index}. {self.catalogue.enrollment_field_label(field)}: "
            f"{values.get(field, '')}".rstrip()
            for index, field in enumerate(fields, start=1)
        )

    def _prepend_saved(self, saved, reply):
        if not saved:
            return reply
        course_started = "course" in saved
        selected = None
        if "preferred_intake_date" in saved:
            selected = self.catalogue.intake_for_start_date(
                saved["preferred_intake_date"]
            )
        display_saved = {
            field: value
            for field, value in saved.items()
            if field not in {"course", "preferred_intake_date"}
        }
        sections = [self._selected_course_intro(saved.get("course"))] if course_started else []
        items = "\n".join(
            f"- {self.catalogue.enrollment_field_label(field)}: "
            f"{self._safe_display(field, value)}"
            for field, value in display_saved.items()
        )
        if items:
            sections.append(
                self.catalogue.enrollment_reply("outcome_saved", saved_items=items)
            )
        if selected:
            sections.append(
                self.catalogue.enrollment_reply(
                    "intake_selected",
                    course_name=(
                        self.catalogue.course_for(selected.get("course_id")) or self.catalogue.course
                    )["name"],
                    display_date=selected["display_date"],
                    start_time=_display_time(selected["start_time"]),
                    end_time=_display_time(selected["end_time"]),
                    intake_type=selected["intake_type"],
                    availability=selected["status"].title(),
                )
            )
        return "\n\n".join([*sections, reply])

    def _stored_validation_errors(self, draft):
        errors = []
        for field in ENROLLMENT_FIELDS:
            if field == "preferred_intake_date":
                continue
            value = getattr(draft, field)
            if value is None:
                continue
            _, error = validate_field(
                field,
                value,
                self.catalogue,
                course_value=draft.course,
                **self.validation_options,
            )
            if error:
                errors.append((field, error))
        intake_error = self._selected_intake_error(draft)
        if intake_error and not any(
            field == "preferred_intake_date" for field, _ in errors
        ):
            errors.append(("preferred_intake_date", intake_error))
        return errors

    def _duplicate_enrollment_error(self, draft):
        if not draft.nric or not draft.course or not draft.intake_id:
            return None
        duplicate = (
            db.session.query(EnrollmentDraft.id)
            .join(Conversation, Conversation.id == EnrollmentDraft.conversation_id)
            .filter(
                EnrollmentDraft.id != draft.id,
                EnrollmentDraft.nric == draft.nric,
                EnrollmentDraft.course == draft.course,
                EnrollmentDraft.intake_id == draft.intake_id,
                EnrollmentDraft.status.in_(
                    (
                        "awaiting_confirmation",
                        "confirmed",
                        "enrolled",
                        "invoice_sent",
                        "awaiting_payment",
                        "paid",
                        "receipt_issued",
                    )
                ),
            )
            .first()
        )
        if duplicate is None:
            return None
        return (
            "It looks like an active enrolment already exists for this participant, "
            "course and intake. I have not created another confirmed enrolment. "
            "Please ask our staff to review the existing registration."
        )

    @staticmethod
    def _set_validation_state(draft, status, corrections):
        draft.validation_status = status
        draft.pending_corrections = list(dict.fromkeys(corrections))
        if status in {"valid", "cancelled"}:
            draft.validation_errors = {}

    @staticmethod
    def _update_validation_errors(draft, saved, invalid):
        errors = dict(draft.validation_errors or {})
        for field in saved:
            errors.pop(field, None)
        for field, message in invalid:
            errors[field] = message
        draft.validation_errors = errors

    def _safe_display(self, field, value):
        if field == "nric":
            return mask_nric(value)
        if field == "email":
            return _mask_email(value)
        if field == "mobile_number":
            return _mask_mobile(value)
        if field == "preferred_intake_date":
            intake = self.catalogue.intake_for_start_date(value)
            return intake["display_date"] if intake else _display_date(value)
        if field == "date_of_birth":
            return _display_date(value)
        return str(value)

    def _fee(self, draft):
        approved = self.catalogue.course_fee_for(draft.course_id or draft.course)
        return Decimal(draft.course_fee or approved or 0)

    def _course_fee_display(self, draft):
        if draft.course_fee is None:
            return "Awaiting staff confirmation"
        if draft.course_fee == self.catalogue.course_fee_for(draft.course_id or draft.course):
            return self.catalogue.course_fee_display_for(draft.course_id or draft.course)
        return _display_amount(draft.course_fee)

    @staticmethod
    def _friendly_reference(draft):
        if getattr(draft, "friendly_reference", None):
            return draft.friendly_reference
        year = (draft.confirmed_at or draft.created_at).year
        return f"ENR-{year}-{draft.id.replace('-', '')[:6].upper()}"

    @staticmethod
    def _ensure_friendly_reference(draft):
        if draft.friendly_reference:
            return draft.friendly_reference
        year = (draft.confirmed_at or draft.created_at).year
        prefix = f"ENR-{year}-"
        existing = db.session.execute(
            db.select(EnrollmentDraft.friendly_reference).where(
                EnrollmentDraft.friendly_reference.like(f"{prefix}%")
            )
        ).scalars()
        sequence = max(
            (
                int(value.rsplit("-", 1)[-1])
                for value in existing
                if value and value.rsplit("-", 1)[-1].isdigit()
            ),
            default=0,
        ) + 1
        draft.friendly_reference = f"{prefix}{sequence:06d}"
        db.session.flush()
        return draft.friendly_reference

    @staticmethod
    def _confirmed_next_actions():
        return (
            "\n\nNeed to make a change?\n\n"
            "- Reply “Change enrollment” to update eligible enrollment details.\n"
            "- Reply “Reschedule enrollment” to select another available intake.\n"
            "- Reply “Cancel enrollment” to request cancellation.\n"
            "- Reply “Resend invoice” if you need the invoice emailed again.\n"
            "- Reply “Enrollment status” to view your current status."
        )

    def _correction_acknowledgement(self, draft, corrected_fields):
        fields = [field for field in corrected_fields if field != "course"]
        name = _first_name(draft.full_name) or "there"
        if len(fields) == 1:
            label = self.catalogue.enrollment_field_label(fields[0]).casefold()
            return self.catalogue.enrollment_reply(
                "correction_single", first_name=name, field_label=label
            )
        return self.catalogue.enrollment_reply(
            "correction_multiple", first_name=name
        )

    @staticmethod
    def _name_prefix(draft):
        name = _first_name(draft.full_name)
        return f"{name}, " if name else ""

    @staticmethod
    def _natural_name_opening(draft, opening):
        name = _first_name(draft.full_name)
        return f"{opening}, {name}." if name else f"{opening}."


def _display_date(value):
    if value is None:
        return "Awaiting staff confirmation"
    return f"{value.day} {value.strftime('%B %Y')}"


def _display_intake_range(start, end):
    if start == end:
        return _display_date(start)
    if start.year == end.year and start.month == end.month:
        return f"{start.day}–{end.day} {start.strftime('%B %Y')}"
    if start.year == end.year:
        return f"{start.day} {start.strftime('%B')}–{end.day} {end.strftime('%B %Y')}"
    return (
        f"{start.day} {start.strftime('%B %Y')}–"
        f"{end.day} {end.strftime('%B %Y')}"
    )


def _display_amount(value):
    amount = Decimal(value or 0)
    if amount == amount.to_integral():
        return f"S${amount:.0f}"
    return f"S${amount:.2f}"


def _display_time(value):
    hour, minute = (int(part) for part in str(value).split(":")[:2])
    suffix = "am" if hour < 12 else "pm"
    display_hour = hour % 12 or 12
    return f"{display_hour}:{minute:02d}{suffix}"


def _amount_value(value):
    return f"{Decimal(value):.2f}" if value is not None else None


def _is_awaiting_intake_confirmation(value):
    normalized = " ".join(str(value).casefold().split())
    return normalized in {
        "awaiting confirmation",
        "awaiting staff confirmation",
        "next available intake",
        "next available",
        "to be confirmed",
        "tbc",
    }


def _looks_like_pending_intake_selection(message):
    normalized = " ".join(str(message or "").casefold().split())
    if re.fullmatch(r"(?:option\s*)?\d+", normalized):
        return True
    if re.search(
        r"\b(?:first|second|third)\s+(?:option|intake|one)\b", normalized
    ):
        return True
    if "last weekend" in normalized:
        return True
    if re.search(
        r"\b(?:full\s+name|nric|fin|date\s+of\s+birth|dob|email|e-mail|"
        r"mobile|phone|handphone|payment|paynow|pay\s+now|skillsfuture|utap)\b",
        normalized,
    ):
        return False
    if any(
        term in normalized for term in ("choose", "select", "intake", "course date")
    ):
        return True
    if len(normalized.split()) <= 5 and re.search(
        r"\b(?:january|february|march|april|may|june|july|august|"
        r"september|october|november|december)\b",
        normalized,
    ):
        return True
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized))


def _looks_like_intake_change(message):
    """Recognise a contextual change to one of the displayed intake choices."""
    normalized = " ".join(str(message or "").casefold().split())
    change_cue = any(
        cue in normalized
        for cue in ("actually", "change", "switch", "instead", "use", "prefer")
    )
    choice_cue = bool(
        re.search(
            r"\b(?:option\s*)?\d+\b|\b(?:first|second|third|last)\s+"
            r"(?:option|intake|one|weekend)\b",
            normalized,
        )
    )
    return change_cue and choice_cue


def _assistant_requested_field(last_assistant_message, field):
    """Check the latest assistant turn, rather than old chat, for a field prompt."""
    lowered = str(last_assistant_message or "").casefold()
    field_terms = {
        "full_name": ("full name", "name as shown"),
        "nric": ("nric", "fin"),
        "date_of_birth": ("date of birth", "dob", "born"),
        "email": ("email", "e-mail"),
        "mobile_number": ("mobile", "phone", "handphone", "contact number"),
    }
    return any(term in lowered for term in field_terms.get(field, ()))


def _is_retired_payment_only(message):
    """Ignore retired payment edits unless the same turn supplies an enrollment field."""
    text = str(message or "").casefold()
    has_payment = bool(
        re.search(r"\b(?:pay|payment|paynow|skillsfuture|utap|credit|allocation)\b", text)
    )
    has_enrollment_field = bool(
        re.search(
            r"\b(?:full\s+name|nric|fin|date\s+of\s+birth|dob|email|e-mail|"
            r"mobile|phone|handphone|intake|course\s+date)\b",
            text,
        )
    )
    return has_payment and not has_enrollment_field


def _looks_like_bare_field_value(field, value):
    """Accept only unambiguous short values for the currently requested field."""
    normalized = " ".join(str(value or "").strip().split())
    lowered = normalized.casefold()
    if not normalized or "?" in normalized or len(normalized) > 120:
        return False
    if lowered in {
        "continue",
        "okay continue",
        "anything else",
        "what next",
        "yes",
        "no",
        "confirm",
        "cancel",
        "discard draft",
        "keep draft",
        "maybe later",
        "not now",
        "wait first",
        "i changed my mind",
    }:
        return False
    if field == "full_name":
        if re.search(
            r"(?i)\b(?:pay|payment|paynow|skillsfuture|utap|credit|amount|invoice)\b",
            normalized,
        ):
            return False
        return bool(
            re.fullmatch(r"[A-Za-z][A-Za-z' -]{1,79}", normalized)
            and 2 <= len(normalized.split()) <= 6
        )
    if field == "nric":
        return bool(re.fullmatch(r"(?i)[STFGM]\d{7}[A-Z]", normalized))
    if field == "date_of_birth":
        return bool(
            re.search(
                r"\b(?:19|20)\d{2}\b|\b(?:january|february|march|april|may|"
                r"june|july|august|september|october|november|december)\b",
                lowered,
            )
            or re.fullmatch(r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", normalized)
        )
    if field == "email":
        return bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", normalized))
    if field == "mobile_number":
        return bool(re.fullmatch(r"(?:\+?65[ -]?)?[689]\d{7}", normalized))
    return False


def _mask_email(value):
    local, separator, domain = str(value or "").partition("@")
    if not separator:
        return "Protected"
    visible = local[:1] if local else ""
    return f"{visible}***@{domain}"


def _mask_mobile(value):
    text = str(value or "")
    return f"****{text[-4:]}" if len(text) >= 4 else "Protected"


def _first_name(full_name):
    return full_name.split()[0] if full_name else None
