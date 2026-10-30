"""Application-controlled, staged enrollment state machine."""

import re
from datetime import date
from decimal import Decimal

from sqlalchemy import and_, or_

from backend.extensions import db
from backend.agents.schemas import EnrollmentExtraction
from backend.models import Conversation, EnrollmentCourse, EnrollmentDraft, utc_now
from backend.services.enrollment.registration_group_service import (
    add_course,
    clear_intake as clear_course_intake,
    courses_overlap,
    display_range as display_course_range,
    next_course_without_intake,
    ordered_courses,
    remove_course,
    save_intake as save_course_intake,
    sync_registration,
    total_fee,
)
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
    "mobile_number",
    "email",
    "date_of_birth",
    # NRIC/FIN is intentionally last.  It is requested only after the ordinary
    # lead/contact fields, when official registration and invoicing can proceed.
    "nric",
)
ENROLLMENT_FIELDS = (
    "course",
    "preferred_intake_date",
    *IDENTITY_FIELDS,
)
FINAL_STATUSES = {"confirmed", "awaiting_course_date"}
INTAKE_SELECTION_FOLLOWUP = "intake_selection"
COURSE_SELECTION_FOLLOWUP = "course_selection"
COURSE_OPTIONS_KEY = "course_options"
POST_CONFIRMATION_ADD_COURSE_FOLLOWUP = "post_confirmation_add_course"
CORRECTION_FOLLOWUP_PREFIX = "enrollment_correction:"


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

    def handle(
        self,
        conversation,
        message,
        context,
        last_assistant_message=None,
        deterministic_confirmation=None,
    ):
        draft = conversation.enrollment_draft
        if draft is not None and draft.status == "discarded":
            draft = None
        retired_reply = self.redirect_retired_draft(conversation, draft)
        if retired_reply is not None:
            return retired_reply, draft
        if draft is None or not ordered_courses(draft):
            initial_selection = self._handle_initial_course_selection(
                conversation, draft, message
            )
            if initial_selection is not None:
                return initial_selection
            draft = conversation.enrollment_draft
        requested_correction = _requested_draft_correction_field(message)
        if (
            requested_correction is not None
            and draft is not None
            and not draft.confirmed_at
            and not _message_contains_replacement(requested_correction, message)
        ):
            conversation.pending_followup = (
                CORRECTION_FOLLOWUP_PREFIX + requested_correction
            )
            draft.status = "collecting"
            errors = dict(draft.validation_errors or {})
            errors.pop(requested_correction, None)
            draft.validation_errors = errors
            draft.pending_corrections = [requested_correction]
            draft.validation_status = "incomplete"
            return self.correction_prompt(draft, requested_correction), draft
        self._hydrate_structured_intake(draft)
        selection_reply = self._handle_course_selection(conversation, draft, message)
        if selection_reply is not None:
            return selection_reply, draft
        # The chat router resolves obvious intake choices before classification so
        # FAQs and casual interruptions can still be handled normally. If an
        # enrollment-classified reply reaches this point while the persisted
        # intake prompt is open, keep it inside that stage regardless of its
        # shape. This prevents a date-like invalid intake response from reaching
        # model extraction and being misinterpreted as a date of birth.
        pending_intake = self.handle_pending_intake_selection(
            conversation, message, enforce_pending_stage=True
        )
        if pending_intake is not None:
            return pending_intake
        if deterministic_confirmation in {"confirm", "reject"}:
            # The application already proved this is an exact answer to the open
            # confirmation prompt. The LLM must not confirm or reject a record.
            extraction = EnrollmentExtraction(confirmation=deterministic_confirmation)
        elif self.is_pending_correction_answer(conversation, message):
            # A value for the specific field the participant chose to change
            # is parsed and validated locally, without model interpretation.
            extraction = EnrollmentExtraction()
        else:
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
        # Course ownership is deterministic. A model-produced course cannot
        # create a selection without evidence from the current participant turn.
        extracted_fields.pop("course", None)
        # Known forms and structured values are resolved deterministically. These
        # values take precedence over model interpretation and still pass through
        # the same validators below.
        parsed_fields = parse_enrollment_fields(message)
        pending_correction = self.pending_correction_field(conversation)
        correction_value = (
            _unwrap_stage_answer(pending_correction, message)
            if pending_correction is not None else None
        )
        if (
            pending_correction is not None
            and pending_correction not in parsed_fields
            and _looks_like_bare_field_value(pending_correction, correction_value)
        ):
            parsed_fields[pending_correction] = correction_value
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

        supplied_fields.pop("course", None)

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
            if pending_correction is not None:
                conversation.pending_followup = (
                    CORRECTION_FOLLOWUP_PREFIX + pending_correction
                )
            return self._outcome_reply(draft, saved, active_invalid), draft

        if pending_correction is not None and pending_correction in saved:
            conversation.pending_followup = None
            draft.pending_corrections = []

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
            courses = ordered_courses(draft)
            if self._overlapping_course_pair(courses):
                second = self._overlapping_course_pair(courses)[1]
                clear_course_intake(second)
                draft.status = "collecting"
                return (
                    "The selected course intakes overlap. Please choose a different "
                    f"intake for {second.course_name}.\n\n"
                    + self._intake_selection_reply(draft, second),
                    draft,
                )
            if self.intake_service is not None and courses:
                try:
                    self.intake_service.allocate_registration(draft, courses)
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
            for course in courses:
                course.status = draft.status
                course.confirmed_at = draft.confirmed_at
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
        courses = ordered_courses(draft)
        if not courses and draft.course is None:
            return ["course"]
        if (courses and any(row.course_fee is None for row in courses)) or (
            not courses and draft.course_fee is None
        ):
            return ["course_fee"]
        if catalogue is not None and catalogue.has_intake_dates:
            if courses and any(row.intake_id is None for row in courses):
                return ["preferred_intake_date"]
            if not courses and draft.preferred_intake_date is None:
                return ["preferred_intake_date"]

        identity_missing = [
            field for field in IDENTITY_FIELDS if getattr(draft, field) is None
        ]
        return identity_missing

    def next_step_prompt(self, draft):
        """Ask exactly one question, based only on the persisted draft state."""
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
        questions = {
            "full_name": "May I have your full name as shown on your official identification?",
            "mobile_number": "May I have your 8-digit Singapore mobile number?",
            "email": "May I have your email address?",
            "date_of_birth": "May I have your date of birth?",
            "nric": (
                "For official registration and invoice preparation, may I have "
                "your NRIC/FIN? It is required to prepare the official record and "
                "will be masked whenever it is displayed."
            ),
        }
        return questions[first]

    @staticmethod
    def pending_correction_field(conversation):
        value = str(conversation.pending_followup or "")
        if not value.startswith(CORRECTION_FOLLOWUP_PREFIX):
            return None
        field = value.removeprefix(CORRECTION_FOLLOWUP_PREFIX)
        return field if field in IDENTITY_FIELDS else None

    def is_pending_correction_answer(self, conversation, message):
        field = self.pending_correction_field(conversation)
        return bool(
            field is not None
            and _looks_like_bare_field_value(
                field, _unwrap_stage_answer(field, message)
            )
        )

    def correction_prompt(self, draft, field):
        if field == "mobile_number":
            ending = (
                str(draft.mobile_number)[-4:]
                if draft is not None and draft.mobile_number
                else None
            )
            current = f" Your current number ends in {ending}." if ending else ""
            return (
                f"Sure — I can update your mobile number.{current} Please send the "
                "new 8-digit Singapore mobile number, or say ‘Keep current number’ "
                "if you do not want to change it."
            )
        label = self.catalogue.enrollment_field_label(field).casefold()
        current = self._safe_display(field, getattr(draft, field, None))
        current_note = f" The saved value is {current}." if current else ""
        examples = {"nric": " It will be masked after validation."}
        return (
            f"Sure - I can update your {label}.{current_note} Please send the new "
            f"{label}.{examples.get(field, '')} Or say 'Keep current' to leave it unchanged."
        )

    def draft_question_reply(self, conversation, draft, message):
        """Answer questions about this draft without handing identity data to AI."""
        if draft is None or draft.status in {"discarded", "cancelled"}:
            return None
        lookup = _draft_detail_lookup(message)
        if lookup == "all":
            return "details", self.progress_reply(draft)
        if lookup is not None:
            label = self.catalogue.enrollment_field_label(lookup)
            value = self._draft_field_value(draft, lookup)
            if value is None:
                answer = f"You haven't provided your {label} yet."
            else:
                answer = self._draft_field_recall(lookup, value)
            return "detail", answer + "\n\n" + self._interruption_followup(
                conversation, draft
            )

        guidance_field, guidance_kind = _field_guidance_request(
            message,
            expected_field=self.pending_correction_field(conversation)
            or self._next_identity_field(draft),
        )
        if guidance_field is None:
            return None
        guidance = self._field_guidance(guidance_field, guidance_kind)
        return "guidance", guidance + "\n\n" + self._interruption_followup(
            conversation, draft
        )

    def _next_identity_field(self, draft):
        return next(
            (
                field
                for field in self.missing_fields(draft, self.catalogue)
                if field in IDENTITY_FIELDS
            ),
            None,
        )

    def _interruption_followup(self, conversation, draft):
        correction = self.pending_correction_field(conversation)
        if correction is not None:
            return self.correction_prompt(draft, correction)
        missing = self.missing_fields(draft, self.catalogue)
        if missing:
            return "To continue: " + self.next_step_prompt(draft)
        if draft.status == "awaiting_confirmation":
            return (
                "Your particulars are complete. Reply 'Confirm enrollment' to submit, "
                "or 'Change [field]' to update something."
            )
        return self._status_wording(draft)

    @staticmethod
    def _draft_field_value(draft, field):
        if field == "course":
            return draft.course
        if field == "preferred_intake_date":
            return draft.preferred_intake_date
        return getattr(draft, field, None)

    def _draft_field_recall(self, field, value):
        if field == "mobile_number":
            return f"For privacy, I can confirm that your saved mobile number ends in {str(value)[-4:]}."
        if field == "nric":
            return f"Your saved NRIC/FIN is {mask_nric(value)}."
        if field == "email":
            return f"Your saved email address is {_mask_email(value)}."
        if field == "date_of_birth":
            return f"Your saved date of birth is {_display_date(value)}."
        if field == "preferred_intake_date":
            return f"Your selected intake is {self._intake_display_from_value(value)}."
        if field == "course":
            return f"Your selected course is {value}."
        return f"Your saved full name is {value}."

    def _intake_display_from_value(self, value):
        intake = self.catalogue.intake_for_start_date(value)
        return intake["display_date"] if intake else _display_date(value)

    @staticmethod
    def _field_guidance(field, kind):
        format_guidance = {
            "full_name": (
                "Enter your full name as shown on your official identification. "
                "Spaces, hyphens and apostrophes are accepted."
            ),
            "mobile_number": (
                "Enter an 8-digit Singapore mobile number starting with 8 or 9. "
                "Spaces, hyphens and an optional +65 are accepted."
            ),
            "email": "Enter a complete email address, for example name@example.com.",
            "date_of_birth": (
                "You can enter your date of birth in common formats such as "
                "20/04/2000, 2000-04-20, 20 April 2000, or April 20th, 2000. "
                "I will normalize it before showing the summary."
            ),
            "nric": (
                "Enter the complete NRIC/FIN: one starting letter, seven digits, "
                "and the final letter. It will be masked after validation."
            ),
        }
        why_guidance = {
            "full_name": "Your full name is needed for the official enrollment record.",
            "mobile_number": "Your mobile number is a required enrollment contact detail.",
            "email": "Your email address is a required enrollment contact detail.",
            "date_of_birth": (
                "Your date of birth is required to validate the enrollment age and "
                "prepare the official record."
            ),
            "nric": (
                "Your NRIC/FIN is collected only at the final particulars stage for "
                "official registration and invoice preparation, and it is masked in chat."
            ),
        }
        return (why_guidance if kind == "why" else format_guidance)[field]

    def intake_prompt_for_ids(self, option_ids, course_value):
        choices = [
            self.catalogue.intake_for_id(intake_id) for intake_id in option_ids
        ]
        choices = [item for item in choices if item is not None]
        course = self.catalogue.course_for(course_value)
        lines = [
            "I’ve saved the details you provided.",
            "",
            f"Please choose the exact intake for {(course or {}).get('name', 'the course')}:",
            "",
        ]
        lines.extend(
            f"{index}. {item['display_date']} — {item['intake_type']}"
            for index, item in enumerate(choices, start=1)
        )
        lines.extend(["", "Reply with the intake number or dates you prefer."])
        return "\n".join(lines)

    def keep_current_correction_value(self, conversation, draft):
        field = self.pending_correction_field(conversation)
        conversation.pending_followup = None
        draft.pending_corrections = []
        if field:
            errors = dict(draft.validation_errors or {})
            errors.pop(field, None)
            draft.validation_errors = errors
        missing = self.missing_fields(draft, self.catalogue)
        if not missing and not draft.validation_errors:
            draft.status = "awaiting_confirmation"
            self._set_validation_state(draft, "valid", [])
            return "No problem — I kept your current details.\n\n" + self._confirmation_summary(draft)
        draft.status = "collecting"
        return "No problem — I kept your current details.\n\n" + self.progress_reply(draft)

    def begin_from_confirmed_interest(self, conversation, course_value, intake_id=None):
        """Create the enrollment draft only after the participant accepts an offer.

        Course and intake values come from choices previously rendered from the
        approved catalogue. No identity value is accepted through this transition.
        """
        course = self.catalogue.course_for(course_value)
        if course is None:
            raise ValueError("The selected course is no longer available.")
        draft = self.repository.get_or_create_draft(conversation)
        rows = ordered_courses(draft)
        target = next((row for row in rows if row.course_id == course["id"]), None)
        if target is None:
            if rows:
                raise ValueError(
                    "The selected course conflicts with the enrollment already in progress."
                )
            target, _ = add_course(draft, course, self.catalogue)
        if intake_id:
            intake = self.catalogue.intake_for_id(intake_id)
            if intake is None or intake.get("course_id") != course["id"]:
                raise ValueError("The selected intake is no longer available.")
            if target.intake_id and target.intake_id != intake_id:
                raise ValueError(
                    "The selected intake conflicts with the enrollment already in progress."
                )
            if not target.intake_id:
                save_course_intake(target, intake, self.catalogue)
        draft.status = "collecting"
        draft.confirmed_at = None
        self._set_validation_state(draft, "incomplete", [])
        sync_registration(draft)
        db.session.flush()
        return draft

    def continuation_reply(self, draft):
        """Continue a saved lead/draft naturally at its next missing field."""
        name = _first_name(draft.full_name)
        opening = f"Great, {name}." if name else "Great."
        course = f" for the {draft.course}" if draft.course else ""
        return (
            f"{opening} I can help you continue your enrollment{course}. "
            f"{self.next_step_prompt(draft)}"
        )

    def progress_reply(self, draft):
        """Render authoritative enrollment progress without calling an LLM."""
        name = _first_name(draft.full_name)
        sections = [f"Hi {name} 👋" if name else "Hi 👋", "Here is your current enrollment progress:"]

        saved_items = []
        for row in ordered_courses(draft):
            saved_items.append(f"- Course {row.position}: {row.course_name}")
            if row.intake_id:
                saved_items.append(
                    f"  Intake: {display_course_range(row)}"
                )
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
        if not ordered_courses(draft) and draft.intake_id and draft.preferred_intake_date:
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
            sections.append(self.next_step_prompt(draft))
        else:
            sections.append(self._status_wording(draft))
        return "\n\n".join(sections)

    def is_contextual_stage_reply(self, draft, message, last_assistant_message):
        if draft is None or not last_assistant_message:
            return False
        return bool(self._bare_stage_value(draft, message, last_assistant_message))

    def remember_course_choices(self, conversation):
        """Persist the exact ordered course list displayed in this conversation."""
        if conversation.pending_action is not None:
            return False
        option_ids = [course["id"] for course in self.catalogue.courses]
        data = dict(conversation.pending_change_data or {})
        data[COURSE_OPTIONS_KEY] = option_ids
        conversation.pending_change_data = data
        conversation.pending_followup = COURSE_SELECTION_FOLLOWUP
        return bool(option_ids)

    def is_waiting_for_course(self, conversation):
        return bool(
            conversation.pending_followup == COURSE_SELECTION_FOLLOWUP
            and self._displayed_course_ids(conversation)
        )

    def handle_pending_course_selection(self, conversation, message):
        """Resolve course replies only against this conversation's displayed list."""
        if not self.is_waiting_for_course(conversation):
            return None
        if _looks_like_course_question(message):
            return None
        selected, duplicate, error, attempted = (
            self.catalogue.resolve_displayed_course_selection(
                message, self._displayed_course_ids(conversation)
            )
        )
        if not attempted and not _is_continue_enrollment(message):
            return None
        if duplicate or not selected:
            self.remember_course_choices(conversation)
            return self._course_selection_reply(error), conversation.enrollment_draft
        return self._save_initial_course_selection(conversation, selected)

    def _handle_initial_course_selection(self, conversation, draft, message):
        option_ids = self._displayed_course_ids(conversation) or [
            course["id"] for course in self.catalogue.courses
        ]
        selected, duplicate, error, attempted = (
            self.catalogue.resolve_displayed_course_selection(message, option_ids)
        )
        if duplicate or not selected:
            self.remember_course_choices(conversation)
            if draft is not None:
                draft.status = "collecting"
            return self._course_selection_reply(error if attempted else None), draft
        return self._save_initial_course_selection(conversation, selected)

    def _save_initial_course_selection(self, conversation, selected):
        draft = self.repository.get_or_create_draft(conversation)
        retired_rows = [
            row
            for row in draft.course_enrollments
            if row.status == "retired_unavailable"
        ]
        for row in retired_rows:
            db.session.delete(row)
        if retired_rows:
            db.session.flush()
        existing = {row.course_id for row in ordered_courses(draft)}
        for course in selected:
            if course["id"] not in existing:
                add_course(draft, course, self.catalogue)
                existing.add(course["id"])
        draft.status = "collecting"
        draft.confirmed_at = None
        self._set_validation_state(draft, "incomplete", [])
        self._clear_course_choices(conversation)
        db.session.flush()
        return self._course_selection_progress(draft), draft

    def redirect_retired_draft(self, conversation, draft):
        """Keep particulars but require a fresh choice for a retired draft course."""
        if draft is None or draft.confirmed_at:
            return None
        retired = [
            row
            for row in draft.course_enrollments
            if row.status != "retired_unavailable"
            and self.catalogue.course_for(row.course_id) is None
        ]
        if not retired:
            return None
        for row in retired:
            if row.allocated_intake_id and self.intake_service is not None:
                self.intake_service.release_course(row)
            row.status = "retired_unavailable"
        sync_registration(draft)
        draft.status = "collecting"
        draft.confirmed_at = None
        self._set_validation_state(draft, "incomplete", ["course"])
        conversation.pending_intake_options = None
        self.remember_course_choices(conversation)
        return (
            "Your previously selected course is no longer available in the current "
            "catalogue. Your valid participant particulars have been kept safely.\n\n"
            + self._course_selection_reply()
        )

    @staticmethod
    def _displayed_course_ids(conversation):
        values = dict(conversation.pending_change_data or {}).get(COURSE_OPTIONS_KEY)
        return list(values) if isinstance(values, list) else []

    @staticmethod
    def _clear_course_choices(conversation):
        if conversation.pending_followup == COURSE_SELECTION_FOLLOWUP:
            conversation.pending_followup = None
        data = dict(conversation.pending_change_data or {})
        data.pop(COURSE_OPTIONS_KEY, None)
        conversation.pending_change_data = data or None

    def remember_intake_choices(self, conversation, draft):
        """Persist the exact numbered choices shown for this conversation."""
        target = next_course_without_intake(draft) if draft is not None else None
        if (
            conversation.pending_action is not None
            or draft is None
            or draft.course is None
            or target is None
            or not self.catalogue.has_intake_dates
        ):
            if conversation.pending_followup == INTAKE_SELECTION_FOLLOWUP:
                conversation.pending_followup = None
                conversation.pending_enrollment_id = None
                conversation.pending_intake_options = None
            return False
        conversation.pending_followup = INTAKE_SELECTION_FOLLOWUP
        conversation.pending_enrollment_id = target.id
        conversation.pending_intake_options = [
            item["id"]
            for item in self.catalogue.selectable_intakes_for(target.course_id)
        ]
        return bool(conversation.pending_intake_options)

    def handle_pending_intake_selection(
        self, conversation, message, *, enforce_pending_stage=False
    ):
        """Resolve a clear choice against only the stored displayed intake list."""
        draft = conversation.enrollment_draft
        target = (
            db.session.get(EnrollmentCourse, conversation.pending_enrollment_id)
            if conversation.pending_enrollment_id
            else next_course_without_intake(draft) if draft else None
        )
        waiting = conversation.pending_followup == INTAKE_SELECTION_FOLLOWUP
        changing = bool(
            draft
            and draft.preferred_intake_date is not None
            and not draft.confirmed_at
            and _looks_like_intake_change(message)
        )
        if target is None and changing:
            courses = ordered_courses(draft)
            target = courses[0] if courses else None
        if (
            not (waiting or changing)
            or conversation.pending_action is not None
            or not conversation.pending_intake_options
            or (
                not (enforce_pending_stage and waiting)
                and not _looks_like_pending_intake_selection(message)
            )
        ):
            return None
        if draft is None or target is None:
            conversation.pending_followup = None
            conversation.pending_enrollment_id = None
            conversation.pending_intake_options = None
            return None

        option_ids = list(conversation.pending_intake_options)
        selected, error = self._match_pending_intake(
            option_ids, message, target.course_id
        )
        if selected is None:
            self.remember_intake_choices(conversation, draft)
            return f"{error}\n\n{self._intake_selection_reply(draft, target)}", draft

        intake_error = self._intake_availability_error(selected, target)
        if intake_error:
            self.remember_intake_choices(conversation, draft)
            return f"{intake_error}\n\n{self._intake_selection_reply(draft, target)}", draft

        save_course_intake(target, selected, self.catalogue)
        overlap = self._overlapping_course_pair(ordered_courses(draft))
        if overlap:
            clear_course_intake(target)
            self.remember_intake_choices(conversation, draft)
            return (
                "That intake overlaps with your other selected course. Please "
                f"choose a different intake for {target.course_name}.\n\n"
                f"{self._intake_selection_reply(draft, target)}",
                draft,
            )
        errors = dict(draft.validation_errors or {})
        errors.pop("preferred_intake_date", None)
        draft.validation_errors = errors
        draft.status = "collecting"
        draft.confirmed_at = None
        conversation.pending_followup = None
        conversation.pending_enrollment_id = None
        # Retain the exact displayed choices after selection so a later natural
        # correction (for example, "change to option 3") remains deterministic.
        conversation.pending_intake_options = option_ids
        db.session.flush()
        next_target = next_course_without_intake(draft)
        if next_target is not None:
            self.remember_intake_choices(conversation, draft)
            return (
                f"Saved {target.course_name} intake: {display_course_range(target)}.\n\n"
                f"Now choose an intake for Course {next_target.position}: "
                f"{next_target.course_name}.\n\n"
                f"{self._intake_selection_reply(draft, next_target)}",
                draft,
            )
        return (
            self._progress_reply(
                draft,
                {"preferred_intake_date": target.intake_start_date},
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
        courses = ordered_courses(draft)
        if not courses and draft.course is None:
            missing.append("course")
        if (courses and any(row.course_fee is None for row in courses)) or (
            not courses and draft.course_fee is None
        ):
            missing.append("course_fee")
        if self.catalogue.has_intake_dates and (
            (courses and any(row.intake_id is None for row in courses))
            or (not courses and draft.preferred_intake_date is None)
        ):
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

    def _handle_course_selection(self, conversation, draft, message):
        """Apply course choices deterministically before CrewAI extraction."""
        text = " ".join(str(message or "").casefold().split())
        rows = ordered_courses(draft)
        if len(rows) > 1 and re.search(
            r"\b(?:remove (?:the )?second course|only want one course|course 1 only)\b",
            text,
        ):
            remove_course(draft, rows[1])
            conversation.pending_enrollment_id = None
            conversation.pending_intake_options = None
            return "Removed the second course.\n\n" + self._course_selection_progress(draft)

        replacement = re.search(
            r"\b(?:change|replace|correct)\s+(?:the\s+)?(?:course\s*)?([12])\b",
            text,
        )
        if replacement and rows:
            position = int(replacement.group(1))
            selected_replacements, duplicate = self.catalogue.match_course_selection(message)
            replacement_course = next(
                (
                    item
                    for item in reversed(selected_replacements)
                    if item["id"] != rows[position - 1].course_id
                ),
                None,
            ) if position <= len(rows) else None
            if duplicate or replacement_course is None:
                return (
                    "You have already selected that course. Please choose a different "
                    "course or continue with your current selection."
                )
            if any(
                item.position != position and item.course_id == replacement_course["id"]
                for item in rows
            ):
                return (
                    "You have already selected that course. Please choose a different "
                    "course or continue with your current selection."
                )
            target = rows[position - 1]
            target.course_id = replacement_course["id"]
            target.course_name = replacement_course["name"]
            target.course_fee = self.catalogue.course_fee_for(replacement_course)
            clear_course_intake(target)
            sync_registration(draft)
            conversation.pending_enrollment_id = None
            conversation.pending_intake_options = None
            return f"Updated Course {position}.\n\n" + self._course_selection_progress(draft)

        if rows and re.search(
            r"\b(?:this course only|course only|only one course|continue with (?:this|that) course)\b",
            text,
        ):
            return self._course_selection_progress(draft)

        add_other = bool(
            rows
            and re.search(
                r"\b(?:add|include|also take|also enrol in|also enroll in)\b.*\bother\b",
                text,
            )
        )
        selected, duplicate = self.catalogue.match_course_selection(message)
        if add_other:
            selected = [
                course
                for course in self.catalogue.courses
                if course["id"] not in {row.course_id for row in rows}
            ][:1]
            duplicate = False
        if not selected and not duplicate:
            return None
        if duplicate:
            return (
                "You have already selected that course. Please choose a different "
                "course or continue with your current selection.\n\n"
                + self._course_selection_progress(draft)
                if rows
                else "You cannot select the same course twice. Please choose one or "
                "two different courses.\n\n" + self._course_selection_reply()
            )

        existing_ids = {row.course_id for row in rows}
        both_request = bool(re.search(r"\b(?:both|all)\b", text))
        if rows and not both_request and not add_other and all(
            course["id"] in existing_ids for course in selected
        ):
            return (
                "You have already selected that course. Please choose a different "
                "course or continue with your current selection.\n\n"
                + self._course_selection_progress(draft)
            )

        conflicts = self._active_course_conflicts(conversation, draft, selected)
        allowed = [course for course in selected if course["id"] not in conflicts]
        for course in allowed:
            if course["id"] not in existing_ids:
                try:
                    add_course(draft, course, self.catalogue)
                    existing_ids.add(course["id"])
                except ValueError:
                    break
        draft.status = "collecting"
        draft.confirmed_at = None
        sync_registration(draft)
        db.session.flush()

        prefix = ""
        if conflicts:
            names = ", ".join(
                course["name"] for course in selected if course["id"] in conflicts
            )
            prefix = (
                f"You already have an active or completed enrollment for {names}. "
                "That course was not added. You may choose a different course.\n\n"
            )
        if not ordered_courses(draft):
            return prefix + self._course_selection_reply()
        return prefix + self._course_selection_progress(draft)

    def _active_course_conflicts(self, conversation, draft, selected):
        ids = [course["id"] for course in selected]
        if not ids or not conversation.user_id:
            return set()
        active = (
            db.session.query(EnrollmentCourse.course_id)
            .join(EnrollmentDraft, EnrollmentDraft.id == EnrollmentCourse.enrollment_id)
            .join(Conversation, Conversation.id == EnrollmentDraft.conversation_id)
            .filter(
                Conversation.user_id == conversation.user_id,
                EnrollmentDraft.id != draft.id,
                EnrollmentCourse.course_id.in_(ids),
                EnrollmentCourse.status.in_(
                    (
                        "awaiting_confirmation",
                        "confirmed",
                        "enrolled",
                        "invoice_sent",
                        "awaiting_payment",
                        "paid",
                        "receipt_issued",
                        "completed",
                    )
                ),
            )
            .all()
        )
        return {course_id for (course_id,) in active}

    def _course_selection_progress(self, draft):
        rows = ordered_courses(draft)
        lines = ["📚 Selected Courses", ""]
        for row in rows:
            lines.append(
                f"{row.position}. {row.course_name} — "
                f"{self.catalogue.course_fee_display_for(row.course_id)}"
            )
        target = next_course_without_intake(draft)
        if target is not None:
            lines.extend(
                [
                    "",
                    f"Choose an intake for Course {target.position}: {target.course_name}.",
                    "",
                    self._intake_selection_reply(draft, target),
                ]
            )
        return "\n".join(lines)

    @staticmethod
    def _overlapping_course_pair(courses):
        for index, left in enumerate(courses):
            for right in courses[index + 1 :]:
                if courses_overlap(left, right):
                    return left, right
        return None

    def _course_selection_reply(self, reason=None):
        prefix = f"{reason}\n\n" if reason else ""
        return (
            prefix
            + "I’d be happy to help you enrol 😊\n\n"
            "📚 Please choose a course:\n\n"
            f"{self.catalogue.course_choices()}\n\n"
            "Reply with the course number, course name, or both."
        )

    def _selected_course_intro(self, course_value):
        course = self.catalogue.course_for(course_value)
        if course is None:
            return ""
        code = course.get("code")
        lines = [
            "You selected:",
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
            if set(saved).issubset({"course", "preferred_intake_date"}):
                reply = self._prepend_saved(saved, self.next_step_prompt(draft))
                if (
                    "preferred_intake_date" in saved
                    and next_course_without_intake(draft) is None
                ):
                    return "📚 Enrollment Started\n\n" + reply
                return reply
            if saved:
                return self._prepend_saved(saved, self.next_step_prompt(draft))
            return self.next_step_prompt(draft)
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
        if (
            len(invalid) == 1
            and invalid[0][0] == "nric"
            and missing
            and missing[0] == "nric"
        ):
            closing = self.catalogue.enrollment_reply("correct_nric_only")
        elif invalid:
            first_invalid = invalid[0][0]
            closing = (
                self._intake_selection_reply(draft)
                if first_invalid == "preferred_intake_date"
                else self.next_step_prompt(draft)
            )
        elif still_required:
            closing = (
                self._intake_selection_reply(draft)
                if still_required[0] == "preferred_intake_date"
                else self.next_step_prompt(draft)
            )
        else:
            closing = self._confirmation_summary(draft)
            draft.status = "awaiting_confirmation"
            for course in ordered_courses(draft):
                course.status = "awaiting_confirmation"
        return "\n\n".join([*sections, closing])

    def _intake_selection_reply(self, draft=None, course_row=None):
        target = course_row or (next_course_without_intake(draft) if draft else None)
        course_value = target.course_id if target is not None else draft.course if draft else None
        if not self.catalogue.selectable_intakes_for(course_value):
            return self.catalogue.no_intakes_message
        return self.catalogue.enrollment_reply(
            "intake_selection",
            demo_heading="",
            intake_description="an available intake",
            intake_choices=self.catalogue.intake_choices(course_value),
            demo_footer="",
        )

    def _confirmation_summary(self, draft):
        courses = ordered_courses(draft)
        if len(courses) > 1:
            sections = [
                "Please review both course enrollments before final submission.",
                "",
                "Participant",
                "",
                f"- Full name: {draft.full_name}",
                f"- NRIC/FIN: {mask_nric(draft.nric)}",
                f"- Date of birth: {_display_date(draft.date_of_birth)}",
                f"- Email: {_mask_email(draft.email)}",
                f"- Mobile: {_mask_mobile(draft.mobile_number)}",
            ]
            for row in courses:
                course = self.catalogue.course_for(row.course_id) or {}
                sections.extend(
                    [
                        "",
                        f"Course {row.position}",
                        "",
                        f"- Course: {row.course_name}",
                        f"- Duration: {course.get('duration') or 'Awaiting staff confirmation'}",
                        f"- Intake: {display_course_range(row)}",
                        f"- Course fee: {self.catalogue.course_fee_display_for(row.course_id)}",
                    ]
                )
            sections.extend(
                [
                    "",
                    "Invoice total",
                    "",
                    f"- Total amount due: {_display_amount(total_fee(draft))} nett",
                    "",
                    "Reply “Confirm” to submit both enrollments, “Correct "
                    "[field/course/intake]: [new value]” to make a change, or "
                    "“Cancel” to stop.",
                ]
            )
            return "\n".join(sections)
        course = self.catalogue.course_for(draft.course_id or draft.course) or self.catalogue.course
        code = course.get("code")
        return self.catalogue.enrollment_reply(
            "confirmation",
            course_name=draft.course,
            course_code_line=f"\n- Course code: {code}" if code else "",
            course_fee=self._course_fee_display(draft),
            duration=course["duration"].capitalize(),
            training_time=course.get("training_time") or "Awaiting staff confirmation",
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
        courses = ordered_courses(draft)
        if draft.status == "awaiting_course_date":
            return (
                f"Thank you, {name}! Your enrolment details have been recorded successfully ✅\n\n"
                f"🧾 Enrollment reference\nReference: {self._friendly_reference(draft)}\n\n"
                "Our team will contact you once the upcoming intake date and exact "
                "training venue are confirmed."
                + self._post_confirmation_course_prompt(draft)
                + self._confirmed_next_actions()
            )
        if len(courses) > 1:
            lines = [
                f"Thank you, {name}! Both course enrollments have been confirmed successfully ✅",
                "",
            ]
            for row in courses:
                lines.extend(
                    [
                        f"📚 Course {row.position}",
                        row.course_name,
                        f"📅 Intake: {display_course_range(row)}",
                        "",
                    ]
                )
            lines.extend(
                [
                    "🧾 Enrollment reference",
                    f"Reference: {self._friendly_reference(draft)}",
                    "",
                    "One combined invoice is being prepared and will be emailed "
                    "to your registered email address.",
                ]
            )
            return (
                "\n".join(lines)
                + "\n\nYou are enrolled in both available courses. Is there anything else I can help you with today?"
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
        return reply + self._post_confirmation_course_prompt(draft) + self._confirmed_next_actions()

    def can_offer_another_course(self, draft):
        active_ids = {row.course_id for row in ordered_courses(draft)}
        if len(active_ids) >= 2:
            return False
        return any(
            course["id"] not in active_ids
            and bool(self.catalogue.selectable_intakes_for(course["id"]))
            for course in self.catalogue.courses
        )

    def _post_confirmation_course_prompt(self, draft):
        if not self.can_offer_another_course(draft):
            return ""
        return (
            "\n\nIs that all, or would you like to enrol in another course?\n\n"
            "Reply “Add another course” to view the remaining course, or “That’s all” to finish."
        )

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
        course = self.catalogue.course_for(course_name)
        if course is None:
            return
        rows = ordered_courses(draft)
        if not rows:
            add_course(draft, course, self.catalogue)
            return
        primary = rows[0]
        if primary.course_id == course["id"]:
            sync_registration(draft)
            return
        if len(rows) > 1 and any(row.course_id == course["id"] for row in rows[1:]):
            return
        primary.course_id = course["id"]
        primary.course_name = course["name"]
        primary.course_fee = self.catalogue.course_fee_for(course)
        clear_course_intake(primary)
        sync_registration(draft)

    @staticmethod
    def _clear_intake(draft):
        rows = ordered_courses(draft)
        if rows:
            clear_course_intake(rows[0])
            return
        draft.preferred_intake_date = None
        draft.intake_id = None
        draft.intake_start_date = None
        draft.intake_end_date = None
        draft.intake_is_demo = None

    def _save_intake(self, draft, intake):
        rows = ordered_courses(draft)
        target = next_course_without_intake(draft) or (rows[0] if rows else None)
        if target is None:
            course = self.catalogue.course_for(intake.get("course_id"))
            target, _ = add_course(draft, course, self.catalogue)
        save_course_intake(target, intake, self.catalogue)

    def _hydrate_structured_intake(self, draft):
        rows = ordered_courses(draft)
        if rows:
            sync_registration(draft)
            return
        if draft.preferred_intake_date is None or draft.intake_start_date is not None:
            return
        intake = self.catalogue.intake_for_start_date(draft.preferred_intake_date)
        if intake:
            self._save_intake(draft, intake)

    def _selected_intake_error(self, draft):
        courses = ordered_courses(draft)
        if courses:
            for row in courses:
                if row.intake_id is None:
                    continue
                if self.intake_service is not None:
                    error = self.intake_service.allocation_error(row.intake_id, row)
                    if error:
                        return error
                intake = self.catalogue.intake_for_id(row.intake_id)
                if intake is None or intake.get("course_id") != row.course_id:
                    return (
                        "The selected intake does not belong to the selected course. "
                        "Please choose another intake."
                    )
            return None
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
        # Once an intake has been selected, an unmistakable identity value is
        # safe to accept out of order. This is especially useful for DOB because
        # phones, emails and NRIC/FIN values are already recognised structurally.
        if (
            "date_of_birth" in missing
            and draft.preferred_intake_date is not None
        ):
            unwrapped_dob = _unwrap_stage_answer("date_of_birth", stripped)
            if _looks_like_bare_field_value("date_of_birth", unwrapped_dob):
                return {"date_of_birth": unwrapped_dob}
        if (
            expected in IDENTITY_FIELDS
            and _assistant_requested_field(last_assistant_message, expected)
            and _looks_like_bare_field_value(
                expected, _unwrap_stage_answer(expected, stripped)
            )
        ):
            return {expected: _unwrap_stage_answer(expected, stripped)}
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
            if field == "date_of_birth" and _looks_like_bare_field_value(
                field, value_text
            ) and value_text in lowered:
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
        courses = ordered_courses(draft)
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
            "selected_courses": [
                {
                    "position": row.position,
                    "course_id": row.course_id,
                    "intake_selected": bool(row.intake_id),
                    "status": row.status,
                }
                for row in courses
            ],
            "pending_stage": (
                f"course_{next_course_without_intake(draft).position}_intake"
                if next_course_without_intake(draft)
                else "participant_details"
                if self.missing_fields(draft, self.catalogue)
                else "confirmation"
            ),
        }

    def _participant_template(self, fields):
        return "\n".join(
            f"{self.catalogue.enrollment_field_label(field)}:" for field in fields
        )

    def _initial_enrollment_template(self, draft):
        fields = [
            "full_name",
            "nric",
            "date_of_birth",
            "email",
            "mobile_number",
        ]
        return "\n".join(
            f"{index}. {self.catalogue.enrollment_field_label(field)}:"
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
        courses = ordered_courses(draft)
        # A phone number or NRIC/FIN may legitimately be shared/reused (for
        # example, a family contact number or a returning participant). Neither
        # field is a unique enrollment key by itself. Only the combined identity
        # plus the same course and intake is treated as a duplicate.
        if not courses or not draft.nric or not draft.mobile_number:
            return None
        active_statuses = (
            "awaiting_confirmation",
            "confirmed",
            "enrolled",
            "invoice_sent",
            "awaiting_payment",
            "paid",
            "receipt_issued",
            "completed",
        )
        course_intakes = [
            and_(
                EnrollmentCourse.course_id == row.course_id,
                EnrollmentCourse.intake_id == row.intake_id,
            )
            for row in courses
            if row.intake_id
        ]
        duplicate = None
        if course_intakes:
            duplicate = (
                db.session.query(EnrollmentCourse.id)
                .join(
                    EnrollmentDraft,
                    EnrollmentDraft.id == EnrollmentCourse.enrollment_id,
                )
                .filter(
                    EnrollmentCourse.enrollment_id != draft.id,
                    EnrollmentDraft.conversation_id != draft.conversation_id,
                    EnrollmentDraft.nric == draft.nric,
                    EnrollmentDraft.mobile_number == draft.mobile_number,
                    EnrollmentDraft.status.in_(active_statuses),
                    EnrollmentCourse.status.notin_(
                        {"cancelled", "retired_unavailable"}
                    ),
                    or_(*course_intakes),
                )
                .first()
            )
        if duplicate is None:
            duplicate = EnrollmentDraft.query.filter(
                EnrollmentDraft.id != draft.id,
                EnrollmentDraft.nric == draft.nric,
                EnrollmentDraft.mobile_number == draft.mobile_number,
                or_(
                    *[
                        and_(
                            EnrollmentDraft.course_id == row.course_id,
                            EnrollmentDraft.intake_id == row.intake_id,
                        )
                        for row in courses
                        if row.intake_id
                    ]
                ),
                EnrollmentDraft.status.in_(active_statuses),
            ).first() if course_intakes else None
        if duplicate is None:
            return None
        return (
            "It looks like an active enrolment with the same NRIC/FIN, mobile number, "
            "course and intake already exists. I have not created an exact duplicate. "
            "Please reopen the existing enrollment or ask staff to review it."
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
        return f"S${amount:,.0f}"
    return f"S${amount:,.2f}"


def _display_time(value):
    if value in (None, ""):
        return "Awaiting staff confirmation"
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


def _is_continue_enrollment(message):
    normalized = " ".join(str(message or "").casefold().split()).strip(" .!?")
    return normalized in {
        "continue enrollment",
        "continue enrolment",
        "continue my enrollment",
        "continue my enrolment",
        "resume enrollment",
        "resume enrolment",
    }


def _looks_like_course_question(message):
    normalized = " ".join(str(message or "").casefold().split())
    if _is_continue_enrollment(normalized):
        return False
    if any(
        phrase in normalized
        for phrase in ("want to enrol", "want to enroll", "sign me up", "register me")
    ):
        return False
    question_opening = re.match(
        r"^(?:what|when|where|which|why|how|can|could|is|are|do|does|tell me)\b",
        normalized,
    )
    fact_terms = (
        "fee", "cost", "duration", "time", "date", "intake", "venue",
        "location", "skillsfuture", "payment", "about",
    )
    return bool(
        "?" in normalized
        or (question_opening and any(term in normalized for term in fact_terms))
    )


def _looks_like_pending_intake_selection(message):
    normalized = " ".join(str(message or "").casefold().split())
    if re.search(
        r"\b(?:not sure|unsure|don t know|do not know|can t decide|cannot decide|"
        r"need time|decide later|continue later|ask something else)\b",
        normalized,
    ):
        # Hesitation is conversation, not evidence selecting an intake record.
        return False
    if re.search(
        r"\b(?:full name|name is|nric|fin|date of birth|dob|born|email|"
        r"mobile|phone|contact number)\b",
        normalized,
    ):
        # Participant details cannot be collected until the displayed intake
        # choice has been resolved. Treat them as an invalid intake response so
        # the deterministic stage gate re-prompts without saving any identity.
        return True
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
    change_cue = bool(
        re.search(r"\b(?:actually|change|switch|instead|use|prefer)\b", normalized)
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
            or re.fullmatch(r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}", normalized)
            or re.fullmatch(r"\d{1,2}\s+\d{1,2}\s+\d{2,4}", normalized)
            or re.fullmatch(r"\d{6}|\d{8}", normalized)
            or bool(
                re.search(
                    r"\b(?:jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\b",
                    lowered,
                )
            )
        )
    if field == "email":
        return bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", normalized))
    if field == "mobile_number":
        return bool(re.fullmatch(r"(?:\+?65[ -]?)?[689]\d{7}", normalized))
    return False


def _requested_draft_correction_field(message):
    text = " ".join(str(message or "").casefold().split())
    if not re.search(
        r"\b(?:change|correct|update|replace|edit|fix)\b", text
    ):
        return None
    fields = (
        ("mobile_number", r"\b(?:mobile|phone|handphone|contact)\s*(?:number)?\b"),
        ("email", r"\be-?mail(?:\s+address)?\b"),
        ("date_of_birth", r"\b(?:date\s+of\s+birth|birth\s*date|birthday|bday|d\.?o\.?b\.?)\b"),
        ("nric", r"\b(?:nric|fin)(?:\s+number)?\b"),
        ("full_name", r"\b(?:full\s+name|name)\b"),
    )
    for field, pattern in fields:
        if re.search(pattern, text):
            return field
    return None


def _message_contains_replacement(field, message):
    parsed = parse_enrollment_fields(message)
    if field in parsed and _looks_like_bare_field_value(field, parsed[field]):
        return True
    patterns = {
        "mobile_number": r"(?<!\d)(?:(?:\+?65)[\s-]?)?[89]\d{3}[\s-]?\d{4}(?!\d)",
        "email": r"\b[^\s,;:@]+@[^\s,;:@]+\.[A-Za-z]{2,}\b",
        "nric": r"\b[STFGM]\s*\d(?:\s*\d){6}\s*[A-Z]\b",
    }
    return bool(re.search(patterns[field], str(message or ""), re.I)) if field in patterns else False


def _unwrap_stage_answer(field, value):
    """Remove harmless conversational wrappers from a prompted field answer."""
    text = " ".join(str(value or "").strip().split()).strip(" .")
    common = r"(?:it\s+is|it'?s|mine\s+is|here(?:'s|\s+is)|sure|okay|ok)"
    text = re.sub(rf"(?i)^\s*{common}\s*[:,-]?\s*", "", text).strip()
    prefixes = {
        "full_name": r"(?i)^(?:my\s+name\s+is|i\s+am|i'?m|call\s+me)\s+",
        "mobile_number": r"(?i)^(?:my\s+)?(?:mobile|phone|number)(?:\s+number)?\s*(?:is|:)\s*",
        "email": r"(?i)^(?:my\s+)?e-?mail(?:\s+address)?\s*(?:is|:)\s*",
        "date_of_birth": r"(?i)^(?:my\s+)?(?:date\s+of\s+birth|birthday|dob)\s*(?:is|:)\s*",
        "nric": r"(?i)^(?:my\s+)?(?:nric|fin)(?:\s+number)?\s*(?:is|:)\s*",
    }
    return re.sub(prefixes.get(field, r"$^"), "", text).strip(" .")


_DRAFT_FIELD_PATTERNS = {
    "full_name": r"\b(?:full\s+name|name)\b",
    "mobile_number": r"\b(?:mobile|phone|handphone|contact)(?:\s+number)?\b|\bnumber\b",
    "email": r"\be-?mail(?:\s+address)?\b",
    "date_of_birth": r"\b(?:date\s+of\s+birth|birth\s*date|birthday|bday|d\.?o\.?b\.?)\b",
    "nric": r"\b(?:nric|fin)(?:\s+number)?\b",
    "preferred_intake_date": r"\b(?:selected\s+)?(?:intake|course\s+date)\b",
    "course": r"\b(?:selected\s+)?(?:course|programme)\b",
}


def _mentioned_draft_field(message):
    text = str(message or "").casefold()
    for field, pattern in _DRAFT_FIELD_PATTERNS.items():
        if re.search(pattern, text):
            return field
    return None


def _draft_detail_lookup(message):
    text = " ".join(str(message or "").casefold().split())
    all_details = bool(
        re.search(
            r"\b(?:show|list|repeat|tell|remind)\s+(?:me\s+)?(?:what\s+)?"
            r"(?:details|information|info|particulars)\b",
            text,
        )
        or re.search(
            r"\bwhat\s+(?:details|information|info|particulars)\s+"
            r"(?:do\s+you\s+have|have\s+i\s+(?:given|provided)|did\s+i\s+(?:give|provide))\b",
            text,
        )
    )
    if all_details:
        return "all"
    memory_cue = bool(
        re.search(
            r"\b(?:did\s+i\s+(?:give|provide|enter|type|send|use)|"
            r"have\s+i\s+(?:already\s+)?(?:given|provided|entered|sent)|"
            r"do\s+you\s+have|did\s+you\s+save|you\s+saved|"
            r"is\s+(?:already\s+)?saved|on\s+(?:file|record)|"
            r"what\s+(?:is|was)\s+my|which\s+.+\s+did\s+i|"
            r"what\s+.+\s+did\s+i|repeat\s+my|remind\s+me\s+(?:of|what))\b",
            text,
        )
        or re.search(
            r"\b(?:is|was|has|have)\b.{0,50}\b(?:changed|updated|corrected)\b|"
            r"\bdid\s+you\s+(?:change|update|correct)\b",
            text,
        )
    )
    if not memory_cue:
        return None
    return _mentioned_draft_field(text)


def _field_guidance_request(message, expected_field=None):
    text = " ".join(str(message or "").casefold().split())
    field = _mentioned_draft_field(text)
    why = bool(
        re.search(r"\bwhy\b", text)
        and re.search(r"\b(?:need|want|ask|collect|required|provide|give)\b", text)
    )
    format_question = bool(
        re.search(
            r"\b(?:what|which)\b[^?]{0,50}\bformat\b|"
            r"\bhow\b[^?]{0,50}\bformat\b|"
            r"\bhow\s+(?:do|should|can)\s+i\s+(?:enter|type|write|send|give)\b|"
            r"\bwhat\s+should\s+i\s+(?:enter|type|write|send|give)\b|"
            r"\bcan\s+i\s+(?:enter|type|write)\b",
            text,
        )
    )
    if not (why or format_question):
        return None, None
    if field not in IDENTITY_FIELDS:
        field = expected_field if expected_field in IDENTITY_FIELDS else None
    return field, "why" if why else "format"


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
