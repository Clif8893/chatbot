"""Deterministic participant cancellation, rescheduling and status workflows."""

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from backend.extensions import db
from backend.models import Conversation, EnrollmentCourse, EnrollmentDraft, utc_now
from backend.models.communications import OutboundMessage
from backend.models.finance import (
    FinanceCase,
    InvoiceGeneration,
    MasterInvoiceExport,
    Payment,
)
from backend.services.invoice_email_service import InvoiceEmailError, ResendCooldownError
from backend.models.operations import AuditEvent, CourseDate, StaffEscalation
from backend.services.conversation_repository import archive_discarded_draft
from backend.services.inquiry_intents import is_career_question
from backend.services.privacy import mask_nric, mask_nrics_in_text
from backend.services.enrollment.enrollment_parser import parse_enrollment_fields
from backend.services.enrollment.validation import validate_field
from backend.services.enrollment.registration_group_service import (
    add_course,
    clear_intake as clear_course_intake,
    display_range as display_course_range,
    ordered_courses,
    save_intake,
    sync_registration,
    total_fee,
)


AWAITING_CANCELLATION = "awaiting_cancellation_confirmation"
AWAITING_DRAFT_DISCARD = "awaiting_draft_discard_confirmation"
AWAITING_RESCHEDULE_SELECTION = "awaiting_reschedule_selection"
AWAITING_RESCHEDULE_CONFIRMATION = "awaiting_reschedule_confirmation"
AWAITING_INVOICE_RESEND = "awaiting_invoice_resend_confirmation"
AWAITING_CHANGE_SELECTION = "change_enrollment_menu"
LEGACY_AWAITING_CHANGE_SELECTION = "awaiting_change_selection"
AWAITING_CHANGE_CONFIRMATION = "awaiting_change_confirmation"
AWAITING_RESCHEDULE_COURSE_SELECTION = "awaiting_intake_change_course_selection"
AWAITING_COURSE_CHANGE_SELECTION = "awaiting_course_change_selection"
AWAITING_COURSE_CHANGE_INTAKE = "awaiting_course_change_intake"
AWAITING_COURSE_CANCELLATION_SELECTION = "cancel_course_selection"
LEGACY_COURSE_CANCELLATION_SELECTION = "awaiting_course_cancellation_selection"
AWAITING_COURSE_CANCELLATION_CONFIRMATION = "awaiting_course_cancellation_confirmation"
AWAITING_COURSE_ADDITION_INTAKE = "awaiting_course_addition_intake"
AWAITING_COURSE_ADDITION_CONFIRMATION = "awaiting_course_addition_confirmation"
POST_CONFIRMATION_ADD_COURSE = "post_confirmation_add_course"

ACTIVE_STATUSES = {
    "confirmed",
    "awaiting_course_date",
    "enrolled",
    "invoice_sent",
    "awaiting_payment",
    "paid",
    "receipt_issued",
}
FINISHED_STATUSES = {"completed", "finished"}


@dataclass
class ManagementResult:
    reply: str
    draft: EnrollmentDraft | None
    action: str


class EnrollmentManagementService:
    """Own participant-managed enrollment changes without delegating to an LLM."""

    def __init__(
        self,
        catalogue,
        intake_service,
        master_invoice_service=None,
        invoice_email_service=None,
        invoice_template_service=None,
        enrollment_resolver=None,
    ):
        self.catalogue = catalogue
        self.intakes = intake_service
        self.master_invoice = master_invoice_service
        self.invoice_email = invoice_email_service
        self.invoice_templates = invoice_template_service
        self.resolver = enrollment_resolver

    def handle(self, conversation, message):
        """Return a deterministic result, or ``None`` for normal chat routing."""
        text = " ".join((message or "").strip().split())
        if not text:
            return None

        if (
            conversation.pending_action_requested_at
            and _as_utc(conversation.pending_action_requested_at)
            < utc_now() - timedelta(minutes=30)
        ):
            if conversation.pending_action in {
                AWAITING_COURSE_ADDITION_INTAKE,
                AWAITING_COURSE_ADDITION_CONFIRMATION,
            }:
                self._abandon_course_addition(
                    conversation, self._pending_owned_draft(conversation)
                )
            self._clear_pending(conversation)
            db.session.commit()

        state = conversation.pending_action
        if state in {
            AWAITING_COURSE_CANCELLATION_SELECTION,
            LEGACY_COURSE_CANCELLATION_SELECTION,
        }:
            if _keeps_enrollment(text):
                draft = self._pending_owned_draft(conversation)
                self._clear_pending(conversation)
                if draft is not None:
                    self._audit(draft, "course_cancellation_kept", "Participant kept both courses")
                db.session.commit()
                return ManagementResult(
                    "No problem — both course enrollments remain unchanged.",
                    draft,
                    "course_cancellation_kept",
                )
            pending_draft = self._pending_owned_draft(conversation)
            if (
                pending_draft is not None
                and self._match_grouped_course(
                    pending_draft,
                    text,
                    (conversation.pending_change_data or {}).get(
                        "cancellable_course_ids"
                    ),
                ) is not None
            ) or (
                is_cancellation_request(text)
                or _is_numeric_menu_attempt(text)
                or _both_courses_choice(text)
            ):
                return self._select_course_cancellation(conversation, text)

        if state == AWAITING_COURSE_CANCELLATION_CONFIRMATION:
            if _keeps_enrollment(text):
                draft = self._pending_owned_draft(conversation)
                self._clear_pending(conversation)
                if draft is not None:
                    self._audit(draft, "course_cancellation_kept", "Participant kept both courses")
                db.session.commit()
                return ManagementResult(
                    "No problem — both course enrollments remain unchanged.",
                    draft,
                    "course_cancellation_kept",
                )
            if _confirms_cancellation(text):
                return self._confirm_course_cancellation(conversation)

        if state == AWAITING_COURSE_ADDITION_INTAKE:
            if _keeps_enrollment(text):
                draft = self._pending_owned_draft(conversation)
                self._abandon_course_addition(conversation, draft)
                self._clear_pending(conversation)
                if draft is not None:
                    self._audit(
                        draft,
                        "course_addition_abandoned",
                        "Participant kept the existing confirmed course",
                    )
                db.session.commit()
                return ManagementResult(
                    "No problem — your confirmed enrollment remains unchanged.",
                    draft,
                    "course_addition_kept",
                )
            if _looks_like_intake_selection(text):
                return self._select_course_addition_intake(conversation, text)

        if state == AWAITING_COURSE_ADDITION_CONFIRMATION:
            if _keeps_enrollment(text):
                draft = self._pending_owned_draft(conversation)
                self._abandon_course_addition(conversation, draft)
                self._clear_pending(conversation)
                if draft is not None:
                    self._audit(
                        draft,
                        "course_addition_abandoned",
                        "Participant kept the existing confirmed course",
                    )
                db.session.commit()
                return ManagementResult(
                    "No problem — your confirmed enrollment remains unchanged.",
                    draft,
                    "course_addition_kept",
                )
            if _confirms_course_addition(text):
                return self._confirm_course_addition(conversation)

        if state is None and conversation.pending_followup == POST_CONFIRMATION_ADD_COURSE:
            if _accepts_additional_course(text):
                draft = self._current_or_single(conversation)
                conversation.pending_followup = None
                if draft is not None:
                    self._audit(
                        draft,
                        "post_confirmation_add_course_accepted",
                        "Participant accepted the remaining-course offer",
                    )
                db.session.commit()
                return self._request_course_addition(
                    conversation, "add the other course"
                )
            if _declines_additional_course(text):
                draft = self._current_or_single(conversation)
                conversation.pending_followup = None
                if draft is not None:
                    self._audit(
                        draft,
                        "post_confirmation_add_course_declined",
                        "Participant declined the remaining-course offer",
                    )
                db.session.commit()
                return ManagementResult(
                    "No problem — your confirmed enrollment is complete. Is there anything else I can help you with today?",
                    draft,
                    "course_addition_declined",
                )

        if state == AWAITING_INVOICE_RESEND:
            if _keeps_invoice(text):
                draft = self._pending_owned_draft(conversation)
                self._clear_pending(conversation)
                db.session.commit()
                return ManagementResult(
                    "No problem — the invoice will not be resent.",
                    draft,
                    "invoice_resend_kept",
                )
            if _confirms_resend(text):
                return self._confirm_invoice_resend(conversation)

        # A persisted change state owns replies before any general enrollment,
        # intake, course, or FAQ interpretation. This ordering is especially
        # important for numeric menu choices.
        if state == AWAITING_CHANGE_CONFIRMATION:
            if _keeps_enrollment(text):
                draft = self._pending_owned_draft(conversation)
                self._clear_pending(conversation)
                db.session.commit()
                return ManagementResult(
                    "No problem — your enrollment remains unchanged.",
                    draft,
                    "change_kept",
                )
            if _confirms_change(text):
                return self._confirm_change(conversation)

        if state == AWAITING_RESCHEDULE_CONFIRMATION:
            if _keeps_current_intake(text) or _keeps_enrollment(text):
                draft = self._pending_owned_draft(conversation)
                self._clear_pending(conversation)
                db.session.commit()
                return ManagementResult(
                    "No problem — your current intake has been kept unchanged.",
                    draft,
                    "reschedule_kept",
                )
            if _confirms_reschedule(text) or _confirms_change(text):
                return self._confirm_reschedule(conversation)
            if is_reschedule_request(text) or _looks_like_intake_selection(text):
                return self._select_reschedule_target(conversation, text)

        if state in {AWAITING_CHANGE_SELECTION, LEGACY_AWAITING_CHANGE_SELECTION}:
            if _keeps_enrollment(text):
                draft = self._pending_owned_draft(conversation)
                self._clear_pending(conversation)
                db.session.commit()
                return ManagementResult(
                    "No problem — your enrollment remains unchanged.",
                    draft,
                    "change_kept",
                )
            option = _change_option(text)
            if option == "intake":
                draft = self._pending_owned_draft(conversation)
                self._clear_pending(conversation)
                return self._request_reschedule(conversation, "", draft=draft)
            if option == "course":
                self._clear_pending(conversation)
                return self._request_course_change(conversation, "")
            if option == "cancel":
                self._clear_pending(conversation)
                return self._request_cancellation(conversation, text)
            if option == "participant":
                draft = self._pending_owned_draft(conversation)
                return ManagementResult(
                    "Please provide the participant detail you want to update, for "
                    "example “Change my email to name@example.com”. I’ll validate it "
                    "and ask you to confirm before applying it.",
                    draft,
                    "change_value_required",
                )
            if _is_numeric_menu_attempt(text):
                return ManagementResult(
                    self._change_menu_reply(),
                    self._pending_owned_draft(conversation),
                    "change_selection_required",
                )
            direct = self._prepare_confirmed_change(conversation, text)
            if direct is not None:
                return direct

        if state == AWAITING_COURSE_CHANGE_SELECTION:
            if _keeps_enrollment(text):
                draft = self._pending_owned_draft(conversation)
                self._clear_pending(conversation)
                db.session.commit()
                return ManagementResult(
                    "No problem — your enrollment remains unchanged.",
                    draft,
                    "change_kept",
                )
            if _looks_like_course_selection(self.catalogue, text):
                return self._select_course_change(conversation, text)

        if state == AWAITING_COURSE_CHANGE_INTAKE:
            if _keeps_enrollment(text):
                draft = self._pending_owned_draft(conversation)
                self._clear_pending(conversation)
                db.session.commit()
                return ManagementResult(
                    "No problem — your enrollment remains unchanged.",
                    draft,
                    "change_kept",
                )
            if _looks_like_intake_selection(text):
                return self._select_course_change_intake(conversation, text)

        if state == AWAITING_RESCHEDULE_COURSE_SELECTION:
            if _keeps_enrollment(text):
                draft = self._pending_owned_draft(conversation)
                self._clear_pending(conversation)
                db.session.commit()
                return ManagementResult(
                    "No problem — your enrollment remains unchanged.",
                    draft,
                    "change_kept",
                )
            if _looks_like_course_selection(self.catalogue, text):
                return self._select_reschedule_course(conversation, text)
        if state == AWAITING_CANCELLATION:
            if _keeps_enrollment(text):
                draft = self._pending_owned_draft(conversation)
                self._clear_pending(conversation)
                db.session.commit()
                return ManagementResult(
                    "No problem — your enrollment has been kept unchanged.",
                    draft,
                    "cancellation_kept",
                )
            if _confirms_cancellation(text):
                return self._confirm_cancellation(conversation)
            if is_reschedule_request(text):
                self._clear_pending(conversation)
                return self._request_reschedule(conversation, text)

        if state == AWAITING_DRAFT_DISCARD:
            if _keeps_enrollment(text) or _keeps_draft(text):
                draft = self._pending_owned_draft(conversation)
                self._clear_pending(conversation)
                db.session.commit()
                return ManagementResult(
                    "No problem — your saved enrollment draft is still available.",
                    draft,
                    "draft_kept",
                )
            if _confirms_draft_discard(text):
                return self._discard_draft(conversation)

        if state is None and _explicit_discard_command(text):
            draft = conversation.enrollment_draft
            if draft is None or draft.status == "discarded":
                self._clear_pending(conversation)
                db.session.commit()
                return ManagementResult(
                    "No active enrollment draft was found. No change was made.",
                    None,
                    "draft_already_discarded",
                )
            if draft.confirmed_at or draft.status in ACTIVE_STATUSES:
                return ManagementResult(
                    "A confirmed enrollment cannot be removed with Discard draft. "
                    "Please request enrollment cancellation instead.",
                    draft,
                    "confirmed_draft_not_discarded",
                )
            return self._request_cancellation(conversation, text)

        if state == AWAITING_RESCHEDULE_SELECTION:
            if _keeps_current_intake(text) or _keeps_enrollment(text):
                draft = self._pending_owned_draft(conversation)
                self._clear_pending(conversation)
                db.session.commit()
                return ManagementResult(
                    "No problem — your current intake has been kept unchanged.",
                    draft,
                    "reschedule_kept",
                )
            if is_cancellation_request(text):
                self._clear_pending(conversation)
                return self._request_cancellation(conversation, text)
            if _looks_like_intake_selection(text):
                return self._select_reschedule_target(conversation, text)

        # Explicit participant-managed changes take priority over status, active
        # enrollment collection, catalogue matching, and AI fallback routing.
        if is_cancellation_request(text):
            return self._request_cancellation(conversation, text)
        if is_reschedule_request(text):
            return self._request_reschedule(conversation, text)
        if is_course_change_request(text):
            return self._request_course_change(conversation, text)
        if is_specific_change_request(text):
            prepared = self._prepare_confirmed_change(conversation, text)
            if prepared is not None:
                return prepared
        if is_general_change_request(text):
            return self._request_change(conversation, text)

        if is_status_query(text):
            status_draft = self._status_response_draft(conversation)
            if status_draft is not None:
                self._audit(
                    status_draft,
                    "enrollment_status_viewed",
                    "Participant requested filtered current-catalogue enrollment status",
                )
                db.session.commit()
            reply = self.status_reply(conversation)
            if conversation.pending_action in {
                AWAITING_COURSE_ADDITION_INTAKE,
                AWAITING_COURSE_ADDITION_CONFIRMATION,
            }:
                pending = conversation.pending_change_data or {}
                pending_course = self.catalogue.course_for(pending.get("course_id"))
                pending_intake = self.catalogue.intake_for_id(pending.get("intake_id"))
                if pending_course is not None:
                    reply += f"\n\n- Pending addition: {pending_course['name']}"
                    if pending_intake is not None:
                        target = db.session.get(CourseDate, pending_intake["id"])
                        if target is not None:
                            reply += f" — {self._row_display(target)}"
                    reply += " — Awaiting your confirmation"
            if conversation.pending_action in {
                AWAITING_COURSE_CANCELLATION_SELECTION,
                LEGACY_COURSE_CANCELLATION_SELECTION,
                AWAITING_COURSE_CANCELLATION_CONFIRMATION,
            }:
                reply += (
                    "\n\n- Cancellation request: Awaiting your confirmation; "
                    "all course registrations remain unchanged."
                )
            return ManagementResult(
                reply,
                status_draft,
                "status_lookup",
            )
        if is_participant_training_query(text):
            context = self.resolver.resolve(conversation, text)
            return ManagementResult(
                self.training_details_reply(conversation, text, context),
                context.enrollment,
                "training_details_lookup",
            )
        if is_participant_fee_query(text):
            context = self.resolver.resolve(conversation, text)
            return ManagementResult(
                self.participant_fee_reply(context),
                context.enrollment,
                "participant_fee_lookup",
            )
        if _confirmation_without_pending(text):
            return ManagementResult(
                "There is no cancellation, intake change, detail change, or invoice resend awaiting confirmation.",
                self._current_or_single(conversation),
                "no_pending_action",
            )
        if is_course_addition_request(text):
            result = self._request_course_addition(conversation, text)
            if result is not None:
                return result
        if is_invoice_resend_request(text):
            return self._request_invoice_resend(conversation, text)
        return None

    def _request_change(self, conversation, message=""):
        draft, multiple_reply = self._action_target(conversation, message)
        if multiple_reply:
            return ManagementResult(multiple_reply, None, "selection_required")
        if draft is None or not draft.confirmed_at or draft.status not in ACTIVE_STATUSES:
            return ManagementResult(
                "Please complete and confirm your enrollment before requesting a confirmed-enrollment change.",
                draft,
                "change_unavailable",
            )
        self._set_pending(conversation, AWAITING_CHANGE_SELECTION, draft)
        db.session.commit()
        return ManagementResult(
            self._change_menu_reply(),
            draft,
            "change_selection_required",
        )

    @staticmethod
    def _change_menu_reply():
        return (
            "I can help you update your enrollment.\n\n"
            "What would you like to change?\n\n"
            "1. Participant details\n2. Course\n3. Intake date\n"
            "4. Cancel enrollment\n\n"
            "Please reply with the option number or describe the change."
        )

    def _request_invoice_resend(self, conversation, message=""):
        draft, multiple_reply = self._action_target(conversation, message)
        if multiple_reply:
            return ManagementResult(multiple_reply, None, "selection_required")
        if draft is not None and draft.status == "cancelled":
            return ManagementResult(
                "An invoice for a cancelled enrollment cannot be resent. Please contact staff if you need help.",
                draft,
                "invoice_resend_cancelled",
            )
        if draft is None or not draft.confirmed_at or draft.status not in ACTIVE_STATUSES:
            return ManagementResult(
                "I could not find a confirmed enrollment with an invoice to resend.",
                draft,
                "invoice_resend_unavailable",
            )
        if not draft.email:
            return ManagementResult(
                "A validated enrollment email is not available. Staff confirmation is required.",
                draft,
                "invoice_resend_unavailable",
            )
        self._set_pending(conversation, AWAITING_INVOICE_RESEND, draft)
        db.session.commit()
        return ManagementResult(
            f"Your latest invoice will be resent to {_mask_email(draft.email)}.\n\n"
            "Please reply “Confirm resend” to continue, or “Keep invoice” if you do not want another email.",
            draft,
            "invoice_resend_confirmation_required",
        )

    def _request_course_change(self, conversation, message):
        draft, multiple_reply = self._action_target(conversation, message)
        if multiple_reply:
            return ManagementResult(multiple_reply, None, "selection_required")
        if draft is None or not draft.confirmed_at or draft.status not in ACTIVE_STATUSES:
            return ManagementResult(
                "Please complete and confirm your enrollment before requesting a course change.",
                draft,
                "change_unavailable",
            )
        requested = self.catalogue.match_course(message)
        if requested is not None:
            return self._stage_course_change(conversation, draft, requested)
        self._set_pending(conversation, AWAITING_COURSE_CHANGE_SELECTION, draft)
        db.session.commit()
        return ManagementResult(
            "Please choose the new course:\n\n"
            + self.catalogue.course_choices()
            + "\n\nPlease reply with the course number or course name, or “Keep enrollment” "
            "to make no change.",
            draft,
            "course_change_selection_required",
        )

    def _select_course_change(self, conversation, message):
        draft = self._pending_owned_draft(conversation)
        if draft is None:
            self._clear_pending(conversation)
            db.session.commit()
            return ManagementResult(
                "The pending course change is no longer available. No change was made.",
                None,
                "change_unavailable",
            )
        requested = self.catalogue.match_course(message)
        if requested is None:
            return ManagementResult(
                "Please choose one of the available courses by number or course name:\n\n"
                + self.catalogue.course_choices(),
                draft,
                "course_change_selection_required",
            )
        return self._stage_course_change(conversation, draft, requested)

    def _stage_course_change(self, conversation, draft, course):
        if course["name"] == draft.course:
            self._clear_pending(conversation)
            db.session.commit()
            return ManagementResult(
                "You are already enrolled in that course. No change was made.",
                draft,
                "same_course",
            )
        available = list(self.catalogue.selectable_intakes_for(course["name"]))
        if not available:
            self._clear_pending(conversation)
            self._escalate_once(
                draft,
                "Course change requested but no future open intake is available.",
                "course_change_review",
            )
            db.session.commit()
            return ManagementResult(
                "No future open intake is available for that course. I’ve referred the request to staff, "
                "and your existing enrollment remains unchanged.",
                draft,
                "change_escalated",
            )
        self._set_pending(conversation, AWAITING_COURSE_CHANGE_INTAKE, draft)
        conversation.pending_change_data = {
            "course": course["name"],
            "course_fee": str(self.catalogue.course_fee_for(course)),
        }
        conversation.pending_intake_options = [item["id"] for item in available]
        conversation.pending_action_requested_at = utc_now()
        choices = "\n".join(
            f"{number}. {item['display_date']} — {item['intake_type']}"
            for number, item in enumerate(available, start=1)
        )
        db.session.commit()
        return ManagementResult(
            f"You selected {course['name']}.\n\n📅 Available Intake Dates\n\n{choices}\n\n"
            "Please reply with the intake number or dates you prefer, or “Keep enrollment” "
            "to retain your current enrollment.",
            draft,
            "course_change_intake_required",
        )

    def _select_course_change_intake(self, conversation, message):
        draft = self._pending_owned_draft(conversation)
        pending = dict(conversation.pending_change_data or {})
        course_name = pending.get("course")
        if draft is None or not course_name:
            self._clear_pending(conversation)
            db.session.commit()
            return ManagementResult(
                "The pending course change is no longer available. No change was made.",
                draft,
                "change_unavailable",
            )
        selected = None
        normalised = _normalise(message)
        if re.fullmatch(r"\d+", normalised) and conversation.pending_intake_options:
            position = int(normalised)
            if 1 <= position <= len(conversation.pending_intake_options):
                selected = self.catalogue.intake_for_id(
                    conversation.pending_intake_options[position - 1]
                )
        if selected is None:
            selected, _ = self.catalogue.match_intake_selection(message, course_name)
        if selected is None or selected.get("id") not in set(conversation.pending_intake_options or []):
            return ManagementResult(
                "Please choose one of the displayed intake numbers or dates.",
                draft,
                "course_change_intake_required",
            )
        target = db.session.get(CourseDate, selected["id"])
        error = self.intakes.allocation_error(selected["id"], draft)
        if target is None or error:
            return ManagementResult(
                (error or "That intake is no longer available.")
                + " Your existing enrollment remains unchanged.",
                draft,
                "course_change_intake_required",
            )
        if self._duplicate_for(draft, course_name, target.id):
            self._clear_pending(conversation)
            self._escalate_once(
                draft,
                "Course change would duplicate an active enrollment for the same participant, course and intake.",
                "course_change_review",
            )
            db.session.commit()
            return ManagementResult(
                "An active enrollment already exists for this participant, course and intake. "
                "No change was made; staff review is required.",
                draft,
                "change_escalated",
            )
        pending.update(
            {
                "intake_id": target.id,
                "preferred_intake_date": target.date.isoformat(),
                "intake_start_date": target.date.isoformat(),
                "intake_end_date": target.end_date.isoformat(),
                "intake_is_demo": bool(target.is_demo),
            }
        )
        conversation.pending_action = AWAITING_CHANGE_CONFIRMATION
        conversation.pending_target_intake_id = target.id
        conversation.pending_change_data = pending
        conversation.pending_action_requested_at = utc_now()
        self._audit(draft, "course_change_requested", f"Course {course_name}; intake {target.id}")
        db.session.commit()
        return ManagementResult(
            "You requested the following change:\n\n"
            f"- Current course: {draft.course}\n"
            f"- New course: {course_name}\n"
            f"- New intake: {self._row_display(target)}\n\n"
            "Please reply “Confirm change” to apply this update, or “Keep enrollment” "
            "to keep your current enrollment.",
            draft,
            "change_confirmation_required",
        )

    def _prepare_confirmed_change(self, conversation, message):
        draft, multiple_reply = self._action_target(conversation, message)
        if multiple_reply:
            return ManagementResult(multiple_reply, None, "selection_required")
        if draft is None or not draft.confirmed_at or draft.status not in ACTIVE_STATUSES:
            return None
        fields = parse_enrollment_fields(message)
        if "preferred_intake_date" in fields or is_reschedule_request(message):
            self._clear_pending(conversation)
            return self._request_reschedule(conversation, message)
        if "course" in fields:
            course = self.catalogue.match_course(fields["course"])
            if not course:
                return ManagementResult(
                    "Please choose one of the available courses by number or name.",
                    draft,
                    "change_invalid",
                )
            if course["name"] != draft.course:
                return self._stage_course_change(conversation, draft, course)
        allowed = {
            "full_name", "date_of_birth", "email", "mobile_number",
        }
        candidate = {}
        errors = []
        for field, value in fields.items():
            if field not in allowed:
                continue
            normalised, error = validate_field(
                field, value, self.catalogue, course_value=draft.course
            )
            if error:
                errors.append(error)
            else:
                candidate[field] = _json_value(normalised)
        if errors:
            return ManagementResult(errors[0], draft, "change_invalid")
        if not candidate:
            return None

        conversation.pending_action = AWAITING_CHANGE_CONFIRMATION
        conversation.pending_enrollment_id = draft.id
        conversation.pending_change_data = candidate
        conversation.pending_action_requested_at = utc_now()
        self._audit(draft, "enrollment_change_requested", "Fields: " + ", ".join(sorted(candidate)))
        db.session.commit()
        lines = []
        for field, value in candidate.items():
            lines.append(
                f"- {self.catalogue.enrollment_field_label(field)}: "
                f"{_safe_change_display(field, value)}"
            )
        return ManagementResult(
            "You requested the following change:\n\n"
            + "\n".join(lines)
            + "\n\nPlease reply “Confirm change” to apply this update, or “Keep enrollment” "
            "to keep your current details.",
            draft,
            "change_confirmation_required",
        )

    def _confirm_change(self, conversation):
        draft = self._pending_owned_draft(conversation)
        candidate = dict(conversation.pending_change_data or {})
        if draft is None or not candidate or not draft.confirmed_at or draft.status not in ACTIVE_STATUSES:
            self._clear_pending(conversation)
            db.session.commit()
            return ManagementResult(
                "The pending enrollment change is no longer available. No change was made.",
                draft,
                "change_unavailable",
            )
        if "course" in candidate and "intake_id" in candidate:
            return self._confirm_course_change(conversation, draft, candidate)
        changed = []
        for field, value in candidate.items():
            if field == "date_of_birth":
                value = date.fromisoformat(value)
            setattr(draft, field, value)
            changed.append(field)
        self._clear_pending(conversation)
        self._audit(draft, "enrollment_change_confirmed", "Fields: " + ", ".join(sorted(changed)))
        db.session.commit()
        self._update_master_invoice(draft)
        if self.invoice_templates is not None:
            generation = self.invoice_templates.generate(draft, actor="participant", force=True)
            if (
                generation is not None
                and generation.status == "generated"
                and self.invoice_email is not None
            ):
                try:
                    self.invoice_email.queue_automatic(draft, generation)
                except InvoiceEmailError:
                    db.session.rollback()
        return ManagementResult(
            "Your enrollment has been updated successfully. Changed: "
            + ", ".join(self.catalogue.enrollment_field_label(field) for field in changed)
            + ". Your other details remain unchanged.",
            draft,
            "change_confirmed",
        )

    def _confirm_course_change(self, conversation, draft, candidate):
        target = db.session.get(CourseDate, candidate["intake_id"], with_for_update=True)
        course = self.catalogue.course_for(candidate.get("course"))
        if target is None or course is None or target.course_id != course.get("id"):
            self._clear_pending(conversation)
            db.session.commit()
            return ManagementResult(
                "The selected course intake is no longer available. Your enrollment remains unchanged.",
                draft,
                "change_unavailable",
            )
        error = self.intakes.allocation_error(target.id, draft)
        if error or self._duplicate_for(draft, course["name"], target.id):
            self._clear_pending(conversation)
            db.session.commit()
            return ManagementResult(
                (error or "An active enrollment already exists for that course and intake.")
                + " Your enrollment remains unchanged.",
                draft,
                "change_unavailable",
            )
        previous = {
            "course": draft.course,
            "course_fee": draft.course_fee,
            "intake_id": draft.intake_id,
            "preferred_intake_date": draft.preferred_intake_date,
            "intake_start_date": draft.intake_start_date,
            "intake_end_date": draft.intake_end_date,
            "intake_is_demo": draft.intake_is_demo,
        }
        draft.course = course["name"]
        draft.course_id = course["id"]
        draft.course_fee = Decimal(candidate["course_fee"])
        draft.intake_id = target.id
        draft.preferred_intake_date = target.date
        draft.intake_start_date = target.date
        draft.intake_end_date = target.end_date
        draft.intake_is_demo = bool(target.is_demo)
        try:
            self.intakes.allocate(draft)
        except ValueError as exc:
            for field, value in previous.items():
                setattr(draft, field, value)
            db.session.rollback()
            return ManagementResult(
                f"{exc} Your enrollment remains unchanged.",
                draft,
                "change_unavailable",
            )
        draft.previous_intake_id = previous["intake_id"]
        draft.rescheduled_at = utc_now()
        active_courses = ordered_courses(draft)
        if len(active_courses) == 1:
            course_row = active_courses[0]
            course_row.course_id = course["id"]
            course_row.course_name = course["name"]
            course_row.course_fee = Decimal(candidate["course_fee"])
            course_row.previous_intake_id = previous["intake_id"]
            course_row.intake_id = target.id
            course_row.intake_start_date = target.date
            course_row.intake_end_date = target.end_date
            course_row.intake_is_demo = bool(target.is_demo)
            course_row.allocated_intake_id = draft.allocated_intake_id
            course_row.intake_allocated_at = draft.intake_allocated_at
            course_row.rescheduled_at = draft.rescheduled_at
        self._clear_pending(conversation)
        self._audit(
            draft,
            "course_change_confirmed",
            f"Previous course {previous['course']}; new course {draft.course}; intake {target.id}",
        )
        db.session.commit()
        self._update_master_invoice(draft)
        if self.invoice_templates is not None:
            generation = self.invoice_templates.generate(draft, actor="participant", force=True)
            if generation is not None and generation.status == "generated" and self.invoice_email is not None:
                try:
                    self.invoice_email.queue_automatic(draft, generation)
                except InvoiceEmailError:
                    db.session.rollback()
        return ManagementResult(
            f"Your enrollment has been updated to {draft.course} for {self._row_display(target)}. "
            "Your enrollment reference is unchanged ✅",
            draft,
            "change_confirmed",
        )

    def _confirm_invoice_resend(self, conversation):
        draft = self._pending_owned_draft(conversation)
        if draft is None or not draft.confirmed_at or draft.status not in ACTIVE_STATUSES:
            self._clear_pending(conversation)
            db.session.commit()
            return ManagementResult(
                "The invoice cannot be resent because the confirmed enrollment is unavailable.",
                draft,
                "invoice_resend_unavailable",
            )
        if self.invoice_email is None:
            self._clear_pending(conversation)
            db.session.commit()
            return ManagementResult(
                "Invoice email delivery is temporarily unavailable. Please contact staff.",
                draft,
                "invoice_resend_unavailable",
            )
        try:
            delivery = self.invoice_email.participant_resend(draft)
            self._clear_pending(conversation)
            self._audit(draft, "invoice_resend_confirmed", f"Delivery {delivery.id} queued")
            db.session.commit()
            return ManagementResult(
                f"Your invoice has been queued for delivery to {_mask_email(draft.email)}. "
                "SMTP acceptance will be recorded separately from inbox delivery.",
                draft,
                "invoice_resend_queued",
            )
        except ResendCooldownError as error:
            db.session.rollback()
            return ManagementResult(str(error), draft, "invoice_resend_cooldown")
        except InvoiceEmailError as error:
            db.session.rollback()
            return ManagementResult(str(error), draft, "invoice_resend_unavailable")

    def _abandon_course_addition(self, conversation, draft):
        pending = dict(conversation.pending_change_data or {})
        row_id = pending.get("course_enrollment_id")
        row = db.session.get(EnrollmentCourse, row_id) if row_id else None
        if draft is None or row is None or row.enrollment_id != draft.id:
            return
        if row.allocated_intake_id:
            self.intakes.release_course(row)
        if pending.get("reactivated"):
            clear_course_intake(row)
            row.status = "cancelled"
            row.confirmed_at = None
        else:
            db.session.delete(row)
            db.session.flush()
        sync_registration(draft)

    def _request_course_addition(self, conversation, message):
        draft = self._current_or_single(conversation)
        if draft is None or not draft.confirmed_at or draft.status not in ACTIVE_STATUSES:
            return None
        active = ordered_courses(draft)
        if not active:
            return None
        conversation.pending_followup = None
        selected, _ = self.catalogue.match_course_selection(message)
        selected_course = selected[0] if len(selected) == 1 else self.catalogue.match_course(message)
        held_ids = {row.course_id for row in active}
        if selected_course is not None and selected_course["id"] in held_ids:
            other = next((item for item in self.catalogue.courses if item["id"] not in held_ids), None)
            if other is None or len(active) >= 2:
                return ManagementResult(
                    "You are already registered for that course, and no duplicate registration was created.",
                    draft,
                    "course_already_registered",
                )
            return ManagementResult(
                f"You are already registered for {selected_course['name']}; no duplicate was created. "
                f"You can add {other['name']} as your second course if you wish.",
                draft,
                "course_already_registered",
            )
        if len(active) >= 2:
            return ManagementResult(
                "This registration already contains the maximum of two different courses. "
                "No duplicate registration was created.",
                draft,
                "course_addition_unavailable",
            )
        if selected_course is None or selected_course["id"] in held_ids:
            selected_course = next(
                (item for item in self.catalogue.courses if item["id"] not in held_ids),
                None,
            )
        if selected_course is None:
            return None
        available = list(self.catalogue.selectable_intakes_for(selected_course["id"]))
        if not available:
            return ManagementResult(
                "No future open intake is currently available for the other course. "
                "Your confirmed enrollment remains unchanged.",
                draft,
                "course_addition_unavailable",
            )
        historical = next(
            (
                row for row in draft.course_enrollments
                if row.course_id == selected_course["id"]
            ),
            None,
        )
        reactivated = bool(historical is not None and historical.status == "cancelled")
        if reactivated and historical.allocated_intake_id:
            self.intakes.release_course(historical)
        row, added = add_course(
            draft,
            selected_course,
            self.catalogue,
            status="pending_addition",
        )
        if not added:
            return ManagementResult(
                "You are already registered for or currently adding that course. No duplicate was created.",
                draft,
                "course_already_registered",
            )
        self._set_pending(conversation, AWAITING_COURSE_ADDITION_INTAKE, draft)
        conversation.pending_change_data = {
            "course_id": selected_course["id"],
            "course_enrollment_id": row.id,
            "reactivated": reactivated,
        }
        conversation.pending_intake_options = [item["id"] for item in available]
        self._audit(
            draft,
            "second_course_requested",
            f"Course {selected_course['id']}; intake selection required",
        )
        if reactivated:
            self._audit(
                draft,
                "cancelled_course_reactivated_pending",
                f"Course {selected_course['id']}; existing child row reused",
            )
        db.session.commit()
        choices = "\n".join(
            f"{number}. {item['display_date']} — {item['intake_type']}"
            for number, item in enumerate(available, start=1)
        )
        return ManagementResult(
            f"Your existing enrollment for {active[0].course_name} remains confirmed.\n\n"
            f"Choose an intake for {selected_course['name']}:\n\n{choices}\n\n"
            "Reply with the intake number or dates, or “Keep enrollment” to make no change.",
            draft,
            "course_addition_intake_required",
        )

    def _select_course_addition_intake(self, conversation, message):
        draft = self._pending_owned_draft(conversation)
        pending = dict(conversation.pending_change_data or {})
        course = self.catalogue.course_for(pending.get("course_id"))
        pending_row = (
            db.session.get(EnrollmentCourse, pending.get("course_enrollment_id"))
            if pending.get("course_enrollment_id")
            else None
        )
        if (
            draft is None
            or course is None
            or pending_row is None
            or pending_row.enrollment_id != draft.id
            or pending_row.status != "pending_addition"
        ):
            self._clear_pending(conversation)
            db.session.commit()
            return ManagementResult(
                "The pending second-course request is no longer available. No change was made.",
                draft,
                "course_addition_unavailable",
            )
        selected = None
        normalised = _normalise(message)
        if re.fullmatch(r"\d+", normalised) and conversation.pending_intake_options:
            position = int(normalised)
            if 1 <= position <= len(conversation.pending_intake_options):
                selected = self.catalogue.intake_for_id(
                    conversation.pending_intake_options[position - 1]
                )
        if selected is None:
            selected, _ = self.catalogue.match_intake_selection(message, course["id"])
        allowed_ids = set(conversation.pending_intake_options or [])
        if selected is None or selected.get("id") not in allowed_ids:
            return ManagementResult(
                "Please choose one of the displayed intake numbers or dates.",
                draft,
                "course_addition_intake_required",
            )
        target = db.session.get(CourseDate, selected["id"])
        if not self._course_date_available(target, course["id"]):
            return ManagementResult(
                "That intake is no longer available. Please choose another displayed intake; "
                "your existing enrollment remains confirmed.",
                draft,
                "course_addition_intake_required",
            )
        active = [
            row for row in ordered_courses(draft)
            if row.status != "pending_addition"
        ]
        if len(active) != 1 or any(row.course_id == course["id"] for row in active):
            self._clear_pending(conversation)
            db.session.commit()
            return ManagementResult(
                "The registration changed while this request was pending. No duplicate course was added.",
                draft,
                "course_addition_unavailable",
            )
        existing_fee = Decimal(str(active[0].course_fee or 0))
        new_fee = Decimal(str(self.catalogue.course_fee_for(course) or 0))
        save_intake(pending_row, selected, self.catalogue)
        pending["intake_id"] = target.id
        conversation.pending_action = AWAITING_COURSE_ADDITION_CONFIRMATION
        conversation.pending_target_intake_id = target.id
        conversation.pending_change_data = pending
        conversation.pending_action_requested_at = utc_now()
        db.session.commit()
        return ManagementResult(
            "Please review the updated two-course registration:\n\n"
            f"1. {active[0].course_name} — {display_course_range(active[0])} — S${existing_fee:.2f}\n"
            f"2. {course['name']} — {self._row_display(target)} — S${new_fee:.2f}\n\n"
            f"Combined total: S${existing_fee + new_fee:,.2f}\n\n"
            "Your first course remains confirmed. Reply “Confirm change” to add the second course, "
            "or “Keep enrollment” to make no change.",
            draft,
            "course_addition_confirmation_required",
        )

    def _confirm_course_addition(self, conversation):
        draft = self._pending_owned_draft(conversation)
        pending = dict(conversation.pending_change_data or {})
        course = self.catalogue.course_for(pending.get("course_id"))
        intake_id = pending.get("intake_id")
        target = db.session.get(CourseDate, intake_id) if intake_id else None
        pending_row = (
            db.session.get(EnrollmentCourse, pending.get("course_enrollment_id"))
            if pending.get("course_enrollment_id")
            else None
        )
        active = (
            [
                row for row in ordered_courses(draft)
                if row.status != "pending_addition"
            ]
            if draft is not None
            else []
        )
        if (
            draft is None
            or not draft.confirmed_at
            or draft.status not in ACTIVE_STATUSES
            or course is None
            or len(active) != 1
            or any(row.course_id == course["id"] for row in active)
            or pending_row is None
            or pending_row.enrollment_id != draft.id
            or pending_row.course_id != course["id"]
            or pending_row.status != "pending_addition"
        ):
            self._clear_pending(conversation)
            db.session.commit()
            return ManagementResult(
                "The registration changed while this request was pending. No second course was added.",
                draft,
                "course_addition_unavailable",
            )
        if not self._course_date_available(target, course["id"]):
            if pending_row is not None:
                clear_course_intake(pending_row)
            pending.pop("intake_id", None)
            conversation.pending_action = AWAITING_COURSE_ADDITION_INTAKE
            conversation.pending_target_intake_id = None
            conversation.pending_change_data = pending
            available = list(self.catalogue.selectable_intakes_for(course["id"]))
            conversation.pending_intake_options = [item["id"] for item in available]
            self._audit(
                draft,
                "course_readdition_failed",
                f"Course {course['id']}; selected intake unavailable",
            )
            db.session.commit()
            return ManagementResult(
                "That intake is no longer available. Please choose another intake; "
                "your first course remains confirmed.",
                draft,
                "course_addition_intake_required",
            )
        original_status = draft.status
        original_confirmed_at = draft.confirmed_at
        try:
            intake = self.catalogue.intake_for_id(target.id)
            if intake is None:
                raise ValueError("That intake is no longer available.")
            save_intake(pending_row, intake, self.catalogue)
            self.intakes.allocate_registration(draft, [pending_row])
            pending_row.status = "confirmed"
            pending_row.confirmed_at = utc_now()
            draft.status = original_status
            draft.confirmed_at = original_confirmed_at
            sync_registration(draft)
            self._clear_pending(conversation)
            action = (
                "cancelled_course_readdition_confirmed"
                if pending.get("reactivated")
                else "second_course_confirmed"
            )
            self._audit(
                draft,
                action,
                f"Course {pending_row.course_id}; intake {pending_row.intake_id}",
            )
            db.session.commit()
        except (ValueError, TypeError) as error:
            db.session.rollback()
            draft = self._pending_owned_draft(conversation)
            pending_row = (
                db.session.get(
                    EnrollmentCourse, pending.get("course_enrollment_id")
                )
                if pending.get("course_enrollment_id")
                else None
            )
            if pending_row is not None and pending_row.status == "pending_addition":
                clear_course_intake(pending_row)
            pending.pop("intake_id", None)
            conversation.pending_action = AWAITING_COURSE_ADDITION_INTAKE
            conversation.pending_target_intake_id = None
            conversation.pending_change_data = pending
            available = list(self.catalogue.selectable_intakes_for(course["id"]))
            conversation.pending_intake_options = [item["id"] for item in available]
            if draft is not None:
                self._audit(
                    draft,
                    "course_readdition_failed",
                    f"Course {course['id']}; capacity revalidation failed",
                )
            db.session.commit()
            return ManagementResult(
                f"{error} Please choose another available intake. "
                "Your first course remains confirmed and unchanged.",
                draft,
                "course_addition_failed",
            )
        invoice_state = self._refresh_group_invoice(draft)
        reply = (
            f"{course['name']} has been added and confirmed. Both courses are now under "
            "the same registration and invoice."
        )
        if invoice_state == "failed":
            reply += " The invoice update needs a staff retry; no stale version is presented as current."
        else:
            reply += " The existing invoice has been updated with both course fees and the combined total."
        return ManagementResult(reply, draft, "course_added")

    def _course_date_available(self, target, course_id):
        return bool(
            target is not None
            and target.course_id == course_id
            and target.status == "open"
            and target.date > self.intakes.today
            and (target.participant_count or 0) < target.capacity
        )

    def status_reply(self, conversation):
        """Show confirmed current courses and at most this chat's incomplete draft."""

        confirmed = []
        seen = set()
        for draft in self._owned_drafts(conversation):
            entries = self._confirmed_current_course_entries(draft)
            if not entries:
                continue
            signature = tuple(
                sorted((course["id"], intake_key) for course, _, intake_key in entries)
            )
            if signature in seen:
                continue
            seen.add(signature)
            confirmed.append((draft, entries))

        current_draft = self._current_incomplete_draft(conversation)
        if not confirmed and current_draft is None:
            return (
                "📋 Enrollment Status\n\n"
                "No saved enrollment was found for your account. You can start an "
                "enrollment whenever you’re ready."
            )

        lines = [
            "✅ Enrollment Status: Confirmed"
            if len(confirmed) == 1 and current_draft is None
            else "📋 Enrollment Status"
        ]
        if current_draft is not None:
            lines.extend(["", "Current conversation", "- Status: Not confirmed yet"])
            pending_followup = str(conversation.pending_followup or "")
            if pending_followup.startswith("enrollment_correction:"):
                field = pending_followup.removeprefix("enrollment_correction:")
                lines.append(
                    f"- Pending change: {self.catalogue.enrollment_field_label(field)}"
                )
                if field == "mobile_number":
                    lines.append(
                        "- Next step: Send the new 8-digit mobile number, or say "
                        "‘Keep current number’."
                    )

                if field != "mobile_number":
                    label = self.catalogue.enrollment_field_label(field).casefold()
                    lines.append(
                        f"- Next step: Send the new {label}, or say Keep current."
                    )

        if confirmed:
            lines.extend(
                [
                    "",
                    "Previous confirmed enrollments"
                    if current_draft is not None
                    else "Confirmed enrollments",
                ]
            )
            number = 1
            for draft, entries in confirmed:
                for course, intake_display, _ in entries:
                    lines.append(
                        f"{number}. {course['name']} — {intake_display} — Confirmed"
                    )
                    number += 1
                if draft.friendly_reference:
                    lines.append(f"   Reference: {draft.friendly_reference}")
                export = MasterInvoiceExport.query.filter_by(
                    enrollment_id=draft.id
                ).one_or_none()
                if export is not None and export.invoice_number:
                    lines.append(f"   Invoice number: {export.invoice_number}")
                amount = (
                    total_fee(draft)
                    if draft.course_enrollments
                    else draft.course_fee
                )
                if amount is not None:
                    lines.append(f"   Current invoice total: S${amount:.2f}")

        if current_draft is not None:
            lines.extend(["", "Current draft details"])
            course = self.catalogue.course_for(
                current_draft.course_id or current_draft.course
            )
            lines.append(
                f"- Course: {course['name'] if course else 'Not selected yet'}"
            )
            if current_draft.preferred_intake_date:
                lines.append(f"- Selected intake: {self._intake_display(current_draft)}")
            saved = [
                self.catalogue.enrollment_field_label(field)
                for field in (
                    "full_name", "nric", "date_of_birth", "email", "mobile_number"
                )
                if getattr(current_draft, field, None) is not None
            ]
            lines.append(f"- Saved details: {', '.join(saved) if saved else 'None yet'}")
            from backend.services.enrollment.enrollment_service import EnrollmentService

            missing = EnrollmentService.missing_fields(current_draft, self.catalogue)
            if not pending_followup.startswith("enrollment_correction:"):
                lines.append(
                    "- Status detail: Draft — more information is required"
                    if missing
                    else "- Status detail: Ready for your confirmation"
                )
            if missing:
                lines.append(
                    "- Still needed: "
                    + ", ".join(
                        self.catalogue.enrollment_field_label(field)
                        for field in missing
                    )
                )
            corrections = list((current_draft.validation_errors or {}).keys())
            if corrections:
                lines.append(
                    "- Needs correction: "
                    + ", ".join(
                        self.catalogue.enrollment_field_label(field)
                        for field in corrections
                    )
                )
        return "\n".join(lines)

    def _status_response_draft(self, conversation):
        current = conversation.enrollment_draft
        if self._is_current_incomplete_draft(current):
            return current
        if current is not None and self._confirmed_current_course_entries(current):
            return current
        return None

    def _current_incomplete_draft(self, conversation):
        draft = conversation.enrollment_draft
        return draft if self._is_current_incomplete_draft(draft) else None

    def _is_current_incomplete_draft(self, draft):
        if draft is None or draft.status not in {"collecting", "awaiting_confirmation"}:
            return False
        current_ids = {course["id"] for course in self.catalogue.courses}
        active_rows = [
            row
            for row in draft.course_enrollments
            if row.status not in {"cancelled", "discarded", "retired_unavailable"}
        ]
        if active_rows:
            return all(row.course_id in current_ids for row in active_rows)
        return draft.course_id is None or draft.course_id in current_ids

    def _confirmed_current_course_entries(self, draft):
        if (
            draft is None
            or draft.confirmed_at is None
            or draft.status not in ACTIVE_STATUSES
        ):
            return []
        entries = []
        for row in ordered_courses(draft, include_cancelled=True):
            if row.status not in ACTIVE_STATUSES:
                continue
            course = self.catalogue.course_for(row.course_id)
            if course is None:
                continue
            entries.append(
                (
                    course,
                    display_course_range(row),
                    row.intake_id
                    or f"{row.intake_start_date or ''}:{row.intake_end_date or ''}",
                )
            )
        if entries:
            return entries
        course = self.catalogue.course_for(draft.course_id or draft.course)
        if course is None:
            return []
        return [
            (
                course,
                self._intake_display(draft),
                draft.intake_id
                or f"{draft.intake_start_date or ''}:{draft.intake_end_date or ''}",
            )
        ]

    def _single_status(self, draft):
        name = _first_name(draft.full_name)
        lines = [f"Hi {name} 👋" if name else "Hi 👋", "", "📋 Enrollment Status", ""]
        status = self._status_label(draft)
        lines.append(f"- Status: {status}")
        grouped = ordered_courses(draft, include_cancelled=True)
        if len(grouped) > 1:
            for course in grouped:
                lines.extend(
                    (
                        f"- Course {course.position}: {course.course_name}",
                        f"  Intake: {display_course_range(course)}",
                        f"  Status: {course.status.replace('_', ' ').title()}",
                    )
                )
        elif draft.course:
            lines.append(f"- Course: {draft.course}")

        if draft.status == "cancelled":
            lines.append(f"- Previous intake: {self._intake_display(draft)}")
            if draft.cancelled_at:
                cancelled = draft.cancelled_at.date()
                lines.append(f"- Cancelled on: {cancelled.day} {cancelled.strftime('%B %Y')}")
            if draft.refund_review_required:
                lines.append("- Payment/refund enquiries: Awaiting staff confirmation")
            lines.extend(["", "This enrollment is cancelled. No further cancellation action is required."])
            return "\n".join(lines)

        if draft.confirmed_at and draft.status in ACTIVE_STATUSES:
            if len(grouped) <= 1:
                lines.append(f"- Intake: {self._intake_display(draft)}")
            course = self.catalogue.course_for(draft.course_id or draft.course)
            lines.append(
                f"- Time: {(course or {}).get('training_time') or 'Staff confirmation is required'}"
            )
            if draft.friendly_reference:
                lines.append(f"- Reference: {draft.friendly_reference}")
            export = MasterInvoiceExport.query.filter_by(enrollment_id=draft.id).one_or_none()
            generation = InvoiceGeneration.query.filter_by(enrollment_id=draft.id).one_or_none()
            if export and export.invoice_number:
                lines.append(f"- Invoice number: {export.invoice_number}")
            amount = total_fee(draft)
            if amount is not None:
                lines.append(f"- Current invoice total: S${amount:.2f}")
            if generation is not None:
                invoice_states = {
                    "generated": "Ready to download",
                    "pending": "Generating",
                    "failed": "Failed - staff can retry",
                    "needs_review": "Awaiting staff review",
                    "void": "Unavailable",
                }
                lines.append(
                    f"- Invoice: {invoice_states.get(generation.status, generation.status.replace('_', ' ').title())}"
                )
            lines.extend(["", "Your enrollment is confirmed ✅"])
            return "\n".join(lines)

        saved = []
        for field in (
            "course", "preferred_intake_date", "full_name", "nric",
            "date_of_birth", "email", "mobile_number",
        ):
            if getattr(draft, field, None) is not None:
                saved.append(self.catalogue.enrollment_field_label(field))
        from backend.services.enrollment.enrollment_service import EnrollmentService

        missing = EnrollmentService.missing_fields(draft, self.catalogue)
        corrections = list((draft.validation_errors or {}).keys())
        lines[3] = "- Status: Draft — more information is required"
        if draft.preferred_intake_date:
            lines.append(f"- Selected intake: {self._intake_display(draft)}")
        lines.append(f"- Saved details: {', '.join(saved) if saved else 'None yet'}")
        if missing:
            lines.append(
                "- Still needed: "
                + ", ".join(self.catalogue.enrollment_field_label(field) for field in missing)
            )
        if corrections:
            lines.append(
                "- Needs correction: "
                + ", ".join(self.catalogue.enrollment_field_label(field) for field in corrections)
            )
        return "\n".join(lines)

    def status_context_reply(self, context):
        """Describe the resolved owner-scoped enrollment without invoking AI."""
        if context.ambiguous:
            return self.resolver.clarification_reply(context)
        draft = context.enrollment
        if draft is None:
            return (
                "Enrollment Status\n\nNo saved enrollment was found for your account. "
                "You can start an enrollment whenever you're ready."
            )
        if len(ordered_courses(draft, include_cancelled=True)) > 1:
            return self._single_status(draft)
        return self._single_context_status(draft, context)

    def _single_context_status(self, draft, context):
        name = _first_name(draft.full_name)
        status_label = self._status_label(draft)
        if draft.rescheduled_at and draft.status in ACTIVE_STATUSES:
            status_label = "Confirmed (rescheduled)"
        lines = [f"Hi {name} 👋" if name else "Hello 👋", "", f"✅ Enrollment Status: {status_label}", ""]
        course = context.course or self.catalogue.course_for(draft.course_id or draft.course)
        course_name = course.get("name") if course else draft.course
        if course_name:
            lines.append(f"- Course: {course_name}")
        if draft.friendly_reference:
            lines.append(f"- Reference: {draft.friendly_reference}")
        if draft.status == "cancelled":
            lines.append(f"- Previous intake: {self._intake_display(draft)}")
            if draft.cancelled_at:
                cancelled = draft.cancelled_at.date()
                lines.append(f"- Cancelled on: {cancelled.day} {cancelled.strftime('%B %Y')}")
            if draft.refund_review_required:
                lines.append("- Payment/refund enquiries: Awaiting staff confirmation")
            lines.extend(["", "This enrollment is cancelled. No further cancellation action is required."])
            return "\n".join(lines)
        if draft.confirmed_at and draft.status in ACTIVE_STATUSES:
            intake_display = self._context_intake_display(context, draft, include_type=True)
            lines.extend(
                [
                    f"- Intake: {intake_display}",
                    f"- Time: {self._training_time(context, course)}",
                ]
            )
            generation = InvoiceGeneration.query.filter_by(enrollment_id=draft.id).one_or_none()
            invoice_states = {
                "generated": "Ready to download",
                "pending": "Generating",
                "failed": "Failed - staff can retry",
                "needs_review": "Awaiting staff review",
                "void": "Unavailable",
            }
            invoice_status = invoice_states.get(generation.status, generation.status.replace("_", " ").title()) if generation else "Preparing"
            lines.append(f"- Invoice: {invoice_status}")
            delivery = (
                OutboundMessage.query.filter_by(enrollment_id=draft.id, channel="email")
                .order_by(OutboundMessage.created_at.desc())
                .first()
            )
            email_states = {
                "queued": "Pending delivery",
                "sending": "Pending delivery",
                "sent": "Sent",
                "failed": "Failed",
            }
            email_status = email_states.get(delivery.status, delivery.status.replace("_", " ").title()) if delivery else "Pending delivery"
            lines.append(f"- Invoice email: {email_status}")
            lines.extend(
                [
                    "",
                    "Your enrollment is confirmed.",
                    "You can ask to change enrollment details or the intake date, cancel, resend the invoice, or check status.",
                ]
            )
            return "\n".join(lines)
        # Preserve the established draft-status details.
        return self._single_status(draft)

    def training_details_reply(self, conversation, message, context):
        if context.ambiguous:
            return self.resolver.clarification_reply(context)
        draft = context.enrollment
        if draft is None:
            return (
                "You do not currently have an active enrollment.\n\n"
                "Would you like to view the available courses?"
            )
        course = context.course or self.catalogue.course_for(draft.course_id or draft.course)
        course_name = course.get("name") if course else (draft.course or "your course")
        venue = (course or {}).get("venue", {}).get("customer_reply") or (
            "The exact training venue will be confirmed by the Q&M Training team."
        )
        name = _first_name(draft.full_name)
        greeting = f"Hi {name} 👋\n\n" if name else ""
        return (
            greeting
            + "Here are your training details:\n\n"
            + f"- Course: {course_name}\n"
            + f"- Training date{'s' if context.intake and context.intake.date != context.intake.end_date else ''}: {self._context_intake_display(context, draft)}\n"
            + f"- Training time: {self._training_time(context, course)}\n"
            + f"- Venue: {venue}\n\n"
            + f"Your enrollment status is {self._status_label(draft).casefold()}."
        )

    def participant_fee_reply(self, context):
        if context.ambiguous:
            return self.resolver.clarification_reply(context)
        if context.enrollment is None:
            return "Please tell me which course you mean so I can give you the correct fee."
        course = context.course or self.catalogue.course_for(
            context.enrollment.course_id or context.enrollment.course
        )
        if course is None:
            return "The course fee requires staff confirmation."
        return f"The fee for {course['name']} is {self.catalogue.course_fee_display_for(course)}."

    @staticmethod
    def _training_time(context, course):
        intake = context.intake if context else None
        if (
            intake is not None
            and intake.start_time
            and intake.end_time
            and any(
                (
                    intake.start_time.hour,
                    intake.start_time.minute,
                    intake.end_time.hour,
                    intake.end_time.minute,
                )
            )
        ):
            def display(value):
                hour = value.hour % 12 or 12
                suffix = "am" if value.hour < 12 else "pm"
                return f"{hour}:{value.minute:02d}{suffix}"
            result = f"{display(intake.start_time)}-{display(intake.end_time)}"
            if intake.date != intake.end_date:
                result += " on each training day"
            return result
        return (course or {}).get("training_time") or "Staff confirmation is required"

    def _context_intake_display(self, context, draft, include_type=False):
        value = self.resolver.intake_display(context.intake, draft)
        if include_type and context.intake is not None and context.intake.label:
            value += f" - {context.intake.label}"
        return value

    def _select_course_cancellation(self, conversation, message):
        draft = self._pending_owned_draft(conversation)
        if draft is None:
            self._clear_pending(conversation)
            db.session.commit()
            return ManagementResult(
                "The pending course cancellation is no longer available. No change was made.",
                None,
                "not_found",
            )
        if _both_courses_choice(message):
            return self._request_cancellation(conversation, "Cancel both courses")
        course_ids = (conversation.pending_change_data or {}).get(
            "cancellable_course_ids"
        )
        course = self._match_grouped_course(draft, message, course_ids)
        if course is None:
            return ManagementResult(
                self._course_cancellation_choices(draft, course_ids),
                draft,
                "course_cancellation_selection_required",
            )
        return self._stage_course_cancellation(conversation, draft, course, message)

    def _stage_course_cancellation(self, conversation, draft, course, message=""):
        if course.status == "cancelled":
            self._clear_pending(conversation)
            db.session.commit()
            return ManagementResult(
                f"{course.course_name} is already cancelled. The other course remains unchanged.",
                draft,
                "already_cancelled",
            )
        if self._grouped_course_started(course):
            self._clear_pending(conversation)
            self._escalate_once(
                draft,
                f"Cancellation requested after the start of {course.course_name}; staff review is required.",
                "cancellation_review",
            )
            self._audit(draft, "course_cancellation_escalated", f"Course {course.course_id} has started")
            db.session.commit()
            return ManagementResult(
                "That course has already started or finished, so I cannot cancel it automatically. "
                "I’ve referred the request to staff; the registration remains unchanged.",
                draft,
                "cancellation_escalated",
            )
        course.cancellation_requested_at = utc_now()
        self._set_pending(conversation, AWAITING_COURSE_CANCELLATION_CONFIRMATION, draft)
        conversation.pending_change_data = {"course_enrollment_id": course.id}
        reason = _safe_reason(message)
        if reason:
            conversation.pending_change_data["reason"] = reason
        detail = f"Course {course.course_id}; confirmation required"
        if reason:
            detail += f"; reason: {reason}"
        self._audit(draft, "course_cancellation_requested", detail)
        db.session.commit()
        return ManagementResult(
            "You are requesting to cancel only this course:\n\n"
            f"- Course: {course.course_name}\n"
            f"- Intake: {display_course_range(course)}\n\n"
            "Your other course will remain confirmed. Reply “Confirm course cancellation” to continue, "
            "or “Keep both courses” to make no change.",
            draft,
            "course_cancellation_confirmation_required",
        )

    def _confirm_course_cancellation(self, conversation):
        draft = self._pending_owned_draft(conversation)
        pending = dict(conversation.pending_change_data or {})
        course_id = pending.get("course_enrollment_id")
        course = db.session.get(EnrollmentCourse, course_id) if course_id else None
        if draft is None or course is None or course.enrollment_id != draft.id:
            self._clear_pending(conversation)
            db.session.commit()
            return ManagementResult(
                "The pending course cancellation is no longer available. No change was made.",
                draft,
                "not_found",
            )
        if course.status == "cancelled":
            self._clear_pending(conversation)
            db.session.commit()
            return ManagementResult(
                f"{course.course_name} is already cancelled. No further action is required.",
                draft,
                "already_cancelled",
            )
        if len(ordered_courses(draft)) <= 1:
            self._clear_pending(conversation)
            db.session.commit()
            return ManagementResult(
                "This is now the only active course. No cancellation was applied; please request cancellation again.",
                draft,
                "cancellation_revalidation_required",
            )
        if self._grouped_course_started(course):
            self._clear_pending(conversation)
            self._escalate_once(
                draft,
                f"Cancellation confirmation received after the start of {course.course_name}.",
                "cancellation_review",
            )
            db.session.commit()
            return ManagementResult(
                "That course has already started or finished. Staff confirmation is required, "
                "so neither course was changed automatically.",
                draft,
                "cancellation_escalated",
            )

        paid = self._payment_received(draft)
        self.intakes.release_course(course)
        course.status = "cancelled"
        course.cancelled_at = utc_now()
        sync_registration(draft)
        draft.refund_review_required = paid
        self._clear_pending(conversation)
        if paid:
            self._escalate_once(
                draft,
                f"A course in paid registration {draft.id} was cancelled; payment review is required.",
                "payment_refund_review",
            )
        detail = f"Course {course.course_id}; one intake place released"
        if pending.get("reason"):
            detail += f"; reason: {pending['reason']}"
        self._audit(draft, "course_cancelled", detail)
        self._audit(
            draft,
            "course_capacity_released",
            f"Course {course.course_id}; intake {course.intake_id}",
        )
        db.session.commit()
        invoice_state = self._refresh_group_invoice(draft)
        remaining = ordered_courses(draft)
        reply = (
            f"{course.course_name} has been cancelled and its intake place has been released. "
            f"{remaining[0].course_name} remains confirmed."
        )
        if invoice_state == "failed":
            reply += " The current invoice update needs a staff retry; no stale version is presented as current."
        else:
            reply += " The existing invoice has been updated to show the remaining course and revised total."
        if paid:
            reply += " Payment or refund handling requires staff confirmation; no refund or credit was issued automatically."
        return ManagementResult(reply, draft, "course_cancelled")

    def _course_cancellation_choices(self, draft, course_ids=None):
        rows = self._cancellable_courses(draft, course_ids)
        lines = ["Which course would you like to cancel?", ""]
        for number, row in enumerate(rows, start=1):
            lines.append(f"{number}. {row.course_name} — {display_course_range(row)}")
        lines.extend(["", "Reply with the course number or name, or “Keep enrollment” to make no change."])
        return "\n".join(lines)

    def _cancellable_courses(self, draft, course_ids=None):
        active = {row.id: row for row in ordered_courses(draft)}
        if course_ids:
            return [active[row_id] for row_id in course_ids if row_id in active]
        return list(active.values())

    def _match_grouped_course(self, draft, message, course_ids=None):
        rows = self._cancellable_courses(draft, course_ids)
        text = _normalise(message)
        if re.search(r"\bkeep (?:the )?(?:first|1st|course 1)\b", text):
            return next((row for row in rows if row.position == 2), None)
        if re.search(r"\bkeep (?:the )?(?:second|2nd|course 2)\b", text):
            return next((row for row in rows if row.position == 1), None)
        number = re.fullmatch(r"(?:course\s*)?([12])", text)
        if number is None:
            number = re.search(
                r"\b(?:cancel|remove|drop|deregister).{0,40}\bcourse\s*([12])\b",
                text,
            )
        if number:
            position = int(number.group(1)) - 1
            return rows[position] if 0 <= position < len(rows) else None
        if re.search(r"\b(?:first|1st)(?: course)?\b", text):
            return rows[0] if rows else None
        if re.search(r"\b(?:second|2nd)(?: course)?\b", text):
            return rows[1] if len(rows) > 1 else None
        recognised, _ = self.catalogue.match_course_selection(message)
        recognised_ids = {item["id"] for item in recognised}
        matches = [
            row for row in rows
            if row.course_id in recognised_ids
        ]
        direct = self.catalogue.course_for(message)
        if not matches and direct is not None:
            matches = [row for row in rows if row.course_id == direct["id"]]
        if matches:
            return matches[0]
        lowered = message.casefold()
        return next(
            (row for row in rows if row.course_name.casefold() in lowered),
            None,
        )

    def _grouped_course_started(self, course):
        return bool(course.intake_start_date and course.intake_start_date <= self.intakes.today)

    def _request_cancellation(self, conversation, message):
        draft, multiple_reply = self._action_target(conversation, message)
        if multiple_reply:
            return ManagementResult(multiple_reply, None, "selection_required")
        if draft is None:
            return ManagementResult(
                "I could not find an enrollment to cancel for your account.",
                None,
                "not_found",
            )
        if draft.status == "cancelled":
            return ManagementResult(
                "This enrollment is already cancelled, so no further action is required.",
                draft,
                "already_cancelled",
            )
        if not draft.confirmed_at or draft.status not in ACTIVE_STATUSES:
            self._set_pending(conversation, AWAITING_DRAFT_DISCARD, draft)
            self._audit(draft, "draft_discard_requested", "Participant asked to discard an unconfirmed draft")
            db.session.commit()
            return ManagementResult(
                "This registration is still a draft and has not been confirmed. "
                "Reply “Discard draft” to remove it from your active registrations, "
                "or “Keep draft” to continue later.",
                draft,
                "draft_discard_requested",
            )
        active_courses = ordered_courses(draft)
        self._audit(
            draft,
            "cancellation_intent_detected",
            "Deterministic cancellation routing selected",
        )
        if len(active_courses) > 1 and not _whole_registration_cancellation(message):
            selected = self._match_grouped_course(draft, message)
            if selected is not None:
                return self._stage_course_cancellation(
                    conversation, draft, selected, message
                )
            self._set_pending(
                conversation, AWAITING_COURSE_CANCELLATION_SELECTION, draft
            )
            conversation.pending_change_data = {
                "cancellable_course_ids": [row.id for row in active_courses]
            }
            self._audit(
                draft,
                "course_cancellation_selection_requested",
                "Participant must choose one course or explicitly cancel the whole registration",
            )
            db.session.commit()
            return ManagementResult(
                self._course_cancellation_choices(
                    draft, [row.id for row in active_courses]
                ),
                draft,
                "course_cancellation_selection_required",
            )
        if self._course_started(draft) or draft.status in FINISHED_STATUSES:
            self._escalate_once(
                draft,
                "Cancellation requested after the course start date; staff review is required.",
                "cancellation_review",
            )
            self._audit(draft, "cancellation_escalated", "Course has started or finished")
            db.session.commit()
            return ManagementResult(
                "This course has already started or finished, so I cannot cancel it automatically. "
                "I’ve referred your request to staff for review.",
                draft,
                "cancellation_escalated",
            )

        draft.cancellation_requested_at = utc_now()
        reason = _safe_reason(message)
        if reason:
            draft.cancellation_reason = reason
        self._set_pending(conversation, AWAITING_CANCELLATION, draft)
        self._audit(draft, "cancellation_requested", "Participant confirmation required")
        db.session.commit()
        name = _first_name(draft.full_name)
        greeting = f"Hi {name},\n\n" if name else ""
        return ManagementResult(
            greeting
            + "You are requesting to cancel your enrollment for:\n\n"
            + f"Course: {draft.course}\n"
            + f"Intake: {self._intake_display(draft)}\n"
            + f"Reference: {draft.friendly_reference or 'Pending'}\n\n"
            + "Please reply “Confirm cancellation” to continue, or “Keep enrollment” "
            + "if you do not want to cancel.",
            draft,
            "cancellation_confirmation_required",
        )

    def _confirm_cancellation(self, conversation):
        draft = self._pending_owned_draft(conversation)
        if draft is None:
            self._clear_pending(conversation)
            db.session.commit()
            return ManagementResult(
                "I could not find that enrollment for your account. No change was made.",
                None,
                "not_found",
            )
        if draft.status == "cancelled":
            self._clear_pending(conversation)
            db.session.commit()
            return ManagementResult(
                "This enrollment is already cancelled, so no further action is required.",
                draft,
                "already_cancelled",
            )
        if self._course_started(draft) or draft.status in FINISHED_STATUSES:
            self._clear_pending(conversation)
            self._escalate_once(
                draft,
                "Cancellation confirmation received after the course start date.",
                "cancellation_review",
            )
            self._audit(draft, "cancellation_escalated", "Course has started or finished")
            db.session.commit()
            return ManagementResult(
                "This course has already started or finished. Staff confirmation is required, "
                "so your enrollment has not been changed automatically.",
                draft,
                "cancellation_escalated",
            )
        if not draft.confirmed_at or draft.status not in ACTIVE_STATUSES:
            self._clear_pending(conversation)
            db.session.commit()
            return ManagementResult(
                "This enrollment is no longer in a cancellable confirmed state. No change was made.",
                draft,
                "not_cancellable",
            )

        paid = self._payment_received(draft)
        grouped = ordered_courses(draft)
        if grouped:
            for course in grouped:
                self.intakes.release_course(course)
                course.status = "cancelled"
                course.cancelled_at = utc_now()
            sync_registration(draft)
        else:
            self.intakes.release(draft)
        draft.status = "cancelled"
        draft.cancelled_at = utc_now()
        draft.confirmed_at = None
        draft.validation_status = "cancelled"
        draft.pending_corrections = []
        draft.refund_review_required = paid
        generation = InvoiceGeneration.query.filter_by(enrollment_id=draft.id).one_or_none()
        if generation is not None:
            generation.status = "void"
            generation.failure_reason = "Enrollment cancelled; participant access disabled."
        self._clear_pending(conversation)
        if paid:
            self._escalate_once(
                draft,
                "A paid enrollment was cancelled; payment and refund handling require staff confirmation.",
                "payment_refund_review",
            )
        self._audit(
            draft,
            "enrollment_cancelled",
            "Place released; payment review required" if paid else "Place released",
        )
        db.session.commit()
        self._update_master_invoice(draft)
        reply = (
            "Your enrollment has been cancelled and the intake place has been released. "
            "If an invoice email was already sent, it cannot be recalled; its private audit record is retained."
        )
        if paid:
            reply += (
                " Payment or refund handling requires staff confirmation; no refund or "
                "credit note has been issued automatically."
            )
        return ManagementResult(reply, draft, "cancelled")

    def _discard_draft(self, conversation):
        draft = self._pending_owned_draft(conversation)
        if draft is None:
            self._clear_pending(conversation)
            db.session.commit()
            return ManagementResult("No saved draft was found. No change was made.", None, "not_found")
        if draft.confirmed_at or draft.status in ACTIVE_STATUSES:
            self._clear_pending(conversation)
            db.session.commit()
            return ManagementResult(
                "A confirmed enrollment cannot be removed with Discard draft. "
                "Please request enrollment cancellation instead.",
                draft,
                "confirmed_draft_not_discarded",
            )
        self.intakes.release(draft)
        draft.status = "discarded"
        draft.confirmed_at = None
        draft.pending_corrections = []
        draft.validation_errors = {}
        draft.validation_status = "discarded"
        self._audit(draft, "enrollment_draft_discarded", "Participant explicitly discarded draft")
        archive_discarded_draft(conversation, draft)
        conversation.active_intent = None
        conversation.pending_followup = None
        self._clear_pending(conversation)
        db.session.commit()
        return ManagementResult(
            "Your unconfirmed enrollment draft has been discarded. No confirmed enrollment was cancelled.",
            None,
            "draft_discarded",
        )

    def _request_reschedule(self, conversation, message, draft=None):
        multiple_reply = None
        if draft is None:
            draft, multiple_reply = self._action_target(conversation, message)
        if multiple_reply:
            return ManagementResult(multiple_reply, None, "selection_required")
        if draft is None:
            return ManagementResult("I could not find an enrollment to reschedule for your account.", None, "not_found")
        if draft.status == "cancelled":
            return ManagementResult("A cancelled enrollment cannot be rescheduled. Staff assistance is required.", draft, "not_reschedulable")
        if not draft.confirmed_at or draft.status not in ACTIVE_STATUSES:
            return ManagementResult("Please complete and confirm this enrollment before requesting an intake change.", draft, "not_reschedulable")

        courses = self._reschedulable_courses(draft)
        if len(courses) > 1:
            selected_course = self._match_reschedule_course(
                draft, message, [row.id for row in courses], allow_number=False
            )
            if selected_course is None:
                self._set_pending(
                    conversation, AWAITING_RESCHEDULE_COURSE_SELECTION, draft
                )
                conversation.pending_change_data = {
                    "course_enrollment_ids": [row.id for row in courses]
                }
                self._audit(
                    draft,
                    "intake_change_course_required",
                    "Participant must choose the course whose intake should change",
                )
                db.session.commit()
                return ManagementResult(
                    self._reschedule_course_choices(courses),
                    draft,
                    "reschedule_course_selection_required",
                )
            return self._begin_reschedule_selection(
                conversation, draft, selected_course, message
            )

        course = courses[0] if courses else None
        return self._begin_reschedule_selection(conversation, draft, course, message)

    def _select_reschedule_course(self, conversation, message):
        draft = self._pending_owned_draft(conversation)
        pending = dict(conversation.pending_change_data or {})
        allowed_ids = pending.get("course_enrollment_ids") or []
        course = (
            self._match_reschedule_course(
                draft, message, allowed_ids, allow_number=True
            )
            if draft is not None
            else None
        )
        if draft is None or course is None:
            courses = self._reschedulable_courses(draft, allowed_ids)
            if draft is None or not courses:
                self._clear_pending(conversation)
                db.session.commit()
                return ManagementResult(
                    "The pending intake change is no longer available. No change was made.",
                    draft,
                    "change_unavailable",
                )
            return ManagementResult(
                self._reschedule_course_choices(courses),
                draft,
                "reschedule_course_selection_required",
            )
        return self._begin_reschedule_selection(conversation, draft, course, "")

    def _begin_reschedule_selection(self, conversation, draft, course, message):
        course_start = (
            course.intake_start_date
            if course is not None
            else draft.intake_start_date or draft.preferred_intake_date
        )
        if (
            (course_start and course_start <= self.intakes.today)
            or draft.status in FINISHED_STATUSES
        ):
            self._escalate_once(draft, "Rescheduling requested after the course start date.", "reschedule_review")
            self._audit(draft, "reschedule_escalated", "Course has started or finished")
            db.session.commit()
            return ManagementResult("This course has already started or finished, so staff must review any date change.", draft, "reschedule_escalated")

        self._set_pending(conversation, AWAITING_RESCHEDULE_SELECTION, draft)
        conversation.pending_change_data = {
            "course_enrollment_id": course.id if course is not None else None,
            "course_id": (
                course.course_id
                if course is not None
                else draft.course_id or self.catalogue.course_id_for(draft.course)
            ),
        }
        draft.reschedule_requested_at = utc_now()
        reply = self._intake_choices(conversation, "")
        if _looks_like_intake_selection(message):
            selected, error = self._match_reschedule_selection(conversation, message)
            if selected:
                db.session.flush()
                return self._set_reschedule_target(conversation, draft, selected)
            reply = self._intake_choices(conversation, message, error)
        self._audit(draft, "reschedule_requested", "Participant must select an available intake")
        db.session.commit()
        return ManagementResult(
            reply,
            draft,
            "reschedule_selection_required",
        )

    def _select_reschedule_target(self, conversation, message):
        draft = self._pending_owned_draft(conversation)
        if draft is None:
            self._clear_pending(conversation)
            db.session.commit()
            return ManagementResult("I could not find that enrollment for your account. No change was made.", None, "not_found")
        selected, error = self._match_reschedule_selection(conversation, message)
        if not selected:
            reply = self._intake_choices(conversation, message, error)
            db.session.commit()
            return ManagementResult(reply, draft, "reschedule_selection_required")
        return self._set_reschedule_target(conversation, draft, selected)

    def _set_reschedule_target(self, conversation, draft, selected):
        course = self._pending_reschedule_course(conversation, draft)
        current_intake_id = (
            course.intake_id if course is not None else draft.intake_id
        )
        current_allocated_id = (
            course.allocated_intake_id
            if course is not None
            else draft.allocated_intake_id
        )
        if selected["id"] in {current_intake_id, current_allocated_id}:
            reply = self._intake_choices(
                conversation, "", "You are already enrolled in this intake."
            )
            db.session.commit()
            return ManagementResult(reply, draft, "same_intake")
        allowed_ids = set(conversation.pending_intake_options or [])
        course_id = (
            course.course_id
            if course is not None
            else draft.course_id or self.catalogue.course_id_for(draft.course)
        )
        if selected["id"] not in allowed_ids or selected.get("course_id") != course_id:
            reply = self._intake_choices(
                conversation,
                "",
                "Please choose one of the displayed alternative intakes.",
            )
            db.session.commit()
            return ManagementResult(reply, draft, "reschedule_selection_required")
        target = db.session.get(CourseDate, selected["id"])
        error = self.intakes.allocation_error(selected["id"], course or draft)
        if target is None or error:
            conversation.pending_action = AWAITING_RESCHEDULE_SELECTION
            conversation.pending_target_intake_id = None
            reply = self._intake_choices(conversation, "", error)
            db.session.commit()
            return ManagementResult(reply, draft, "reschedule_selection_required")
        conversation.pending_action = AWAITING_RESCHEDULE_CONFIRMATION
        conversation.pending_target_intake_id = target.id
        conversation.pending_source_intake_id = current_allocated_id or current_intake_id
        conversation.pending_intake_options = None
        conversation.pending_action_requested_at = utc_now()
        self._audit(draft, "reschedule_target_selected", f"Target intake {target.date.isoformat()}")
        db.session.commit()
        name = _first_name(draft.full_name)
        course_name = course.course_name if course is not None else draft.course
        current_display = (
            display_course_range(course)
            if course is not None
            else self._intake_display(draft)
        )
        return ManagementResult(
            (f"Hi {name} 👋\n\n" if name else "Hi 👋\n\n")
            + "Please confirm this intake change:\n\n"
            + f"- Course: {course_name}\n"
            + f"- Current intake: {current_display}\n"
            + f"- New intake: {self._row_display(target)}\n"
            + "\nReply “Confirm change” to continue, or “Keep enrollment” to make no change.",
            draft,
            "reschedule_confirmation_required",
        )

    def _confirm_reschedule(self, conversation):
        draft = self._pending_owned_draft(conversation)
        target_id = conversation.pending_target_intake_id
        if draft is None or not target_id:
            self._clear_pending(conversation)
            db.session.commit()
            return ManagementResult("The pending intake change could not be found. No change was made.", draft, "not_found")
        if draft.status == "cancelled" or draft.status in FINISHED_STATUSES:
            self._clear_pending(conversation)
            db.session.commit()
            return ManagementResult("This enrollment can no longer be rescheduled. No change was made.", draft, "not_reschedulable")
        target = db.session.get(CourseDate, target_id, with_for_update=True)
        course = self._pending_reschedule_course(conversation, draft)
        course_id = (
            course.course_id
            if course is not None
            else draft.course_id or self.catalogue.course_id_for(draft.course)
        )
        if target is None:
            conversation.pending_action = AWAITING_RESCHEDULE_SELECTION
            conversation.pending_target_intake_id = None
            reply = self._intake_choices(conversation, "", "That intake is no longer available.")
            db.session.commit()
            return ManagementResult(reply, draft, "reschedule_selection_required")
        current_allocated_id = (
            course.allocated_intake_id
            if course is not None
            else draft.allocated_intake_id
        )
        if current_allocated_id == target.id:
            self._clear_pending(conversation)
            db.session.commit()
            return ManagementResult("You are already enrolled in this intake.", draft, "same_intake")

        if target.course_id != course_id:
            error = "That intake does not belong to the selected course."
        else:
            error = self.intakes.allocation_error(target.id, course or draft)
        if error:
            conversation.pending_action = AWAITING_RESCHEDULE_SELECTION
            conversation.pending_target_intake_id = None
            reply = self._intake_choices(conversation, "", error)
            db.session.commit()
            return ManagementResult(reply, draft, "reschedule_selection_required")

        if course is not None:
            previous = (
                course.intake_id,
                course.intake_start_date,
                course.intake_end_date,
                course.intake_is_demo,
            )
            course.intake_id = target.id
            course.intake_start_date = target.date
            course.intake_end_date = target.end_date
            course.intake_is_demo = bool(target.is_demo)
            try:
                self.intakes.allocate_registration(draft, [course])
            except ValueError as exc:
                (
                    course.intake_id,
                    course.intake_start_date,
                    course.intake_end_date,
                    course.intake_is_demo,
                ) = previous
                sync_registration(draft)
                conversation.pending_action = AWAITING_RESCHEDULE_SELECTION
                conversation.pending_target_intake_id = None
                reply = self._intake_choices(conversation, "", str(exc))
                db.session.commit()
                return ManagementResult(reply, draft, "reschedule_selection_required")
            course.previous_intake_id = previous[0]
            course.rescheduled_at = utc_now()
            sync_registration(draft)
        else:
            previous = (
                draft.intake_id,
                draft.preferred_intake_date,
                draft.intake_start_date,
                draft.intake_end_date,
                draft.intake_is_demo,
            )
            draft.intake_id = target.id
            draft.preferred_intake_date = target.date
            draft.intake_start_date = target.date
            draft.intake_end_date = target.end_date
            draft.intake_is_demo = bool(target.is_demo)
            try:
                self.intakes.allocate(draft)
            except ValueError as exc:
                (
                    draft.intake_id,
                    draft.preferred_intake_date,
                    draft.intake_start_date,
                    draft.intake_end_date,
                    draft.intake_is_demo,
                ) = previous
                conversation.pending_action = AWAITING_RESCHEDULE_SELECTION
                conversation.pending_target_intake_id = None
                reply = self._intake_choices(conversation, "", str(exc))
                db.session.commit()
                return ManagementResult(reply, draft, "reschedule_selection_required")

        draft.previous_intake_id = previous[0]
        draft.rescheduled_at = utc_now()
        course_name = course.course_name if course is not None else draft.course
        self._clear_pending(conversation)
        self._audit(
            draft,
            "enrollment_rescheduled",
            f"Previous intake {previous[0] or 'unscheduled'}; new intake {target.id}",
        )
        db.session.commit()
        self._refresh_group_invoice(draft)
        return ManagementResult(
            f"Your {course_name} intake has been changed to {self._row_display(target)}. "
            "Your other enrollment details remain unchanged ✅",
            draft,
            "rescheduled",
        )

    def _reschedulable_courses(self, draft, course_ids=None):
        if draft is None:
            return []
        allowed = set(course_ids or [])
        return [
            row
            for row in ordered_courses(draft)
            if row.status in ACTIVE_STATUSES and (not allowed or row.id in allowed)
        ]

    def _match_reschedule_course(
        self, draft, message, course_ids=None, *, allow_number
    ):
        rows = self._reschedulable_courses(draft, course_ids)
        text = _normalise(message)
        if allow_number:
            number = re.fullmatch(r"(?:course\s*)?([1-9]\d*)", text)
            if number:
                position = int(number.group(1)) - 1
                return rows[position] if 0 <= position < len(rows) else None
            ordinal = re.fullmatch(r"(?:the )?(first|second|third)(?: course)?", text)
            if ordinal:
                position = {"first": 0, "second": 1, "third": 2}[ordinal.group(1)]
                return rows[position] if position < len(rows) else None
        recognised, _ = self.catalogue.match_course_selection(message)
        recognised_ids = {item["id"] for item in recognised}
        direct = self.catalogue.course_for(message)
        if direct is not None:
            recognised_ids.add(direct["id"])
        matches = [row for row in rows if row.course_id in recognised_ids]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _reschedule_course_choices(courses):
        lines = ["Which course intake would you like to change?", ""]
        lines.extend(
            f"{number}. {row.course_name} — {display_course_range(row)}"
            for number, row in enumerate(courses, start=1)
        )
        lines.extend(
            [
                "",
                "Reply with the course number or name, or “Keep enrollment” to make no change.",
            ]
        )
        return "\n".join(lines)

    def _pending_reschedule_course(self, conversation, draft):
        pending = dict(conversation.pending_change_data or {})
        course_enrollment_id = pending.get("course_enrollment_id")
        if course_enrollment_id:
            row = db.session.get(EnrollmentCourse, course_enrollment_id)
            if (
                row is not None
                and row.enrollment_id == draft.id
                and row.status in ACTIVE_STATUSES
            ):
                return row
            return None
        rows = self._reschedulable_courses(draft)
        return rows[0] if len(rows) == 1 else None

    def _owned_drafts(self, conversation):
        if not conversation.user_id:
            return []
        return (
            EnrollmentDraft.query.join(Conversation)
            .filter(Conversation.user_id == conversation.user_id)
            .order_by(EnrollmentDraft.updated_at.desc(), EnrollmentDraft.id.desc())
            .all()
        )

    def _current_or_single(self, conversation):
        if self.resolver is not None:
            return self.resolver.resolve(conversation).enrollment
        if conversation.enrollment_draft is not None:
            return conversation.enrollment_draft
        drafts = self._owned_drafts(conversation)
        return drafts[0] if len(drafts) == 1 else None

    def _action_target(self, conversation, message=""):
        if self.resolver is not None:
            context = self.resolver.resolve(conversation, message)
            if context.ambiguous:
                return None, self.resolver.clarification_reply(context)
            return context.enrollment, None
        if conversation.enrollment_draft is not None:
            return conversation.enrollment_draft, None
        drafts = [draft for draft in self._owned_drafts(conversation) if draft.status != "discarded"]
        if len(drafts) == 1:
            return drafts[0], None
        if len(drafts) > 1:
            lines = ["I found more than one enrollment for your account:", ""]
            for number, draft in enumerate(drafts, start=1):
                lines.append(f"{number}. {draft.course or 'Course not selected'} — {self._intake_display(draft)} — {self._status_label(draft)}")
            lines.extend(["", "Please open the relevant previous chat before making this change."])
            return None, "\n".join(lines)
        return None, None

    def _pending_owned_draft(self, conversation):
        if not conversation.pending_enrollment_id or not conversation.user_id:
            return None
        return (
            EnrollmentDraft.query.join(Conversation)
            .filter(
                EnrollmentDraft.id == conversation.pending_enrollment_id,
                Conversation.user_id == conversation.user_id,
            )
            .one_or_none()
        )

    @staticmethod
    def _set_pending(conversation, state, draft):
        conversation.pending_action = state
        conversation.pending_enrollment_id = draft.id
        conversation.pending_target_intake_id = None
        conversation.pending_source_intake_id = draft.allocated_intake_id or draft.intake_id
        conversation.pending_intake_options = None
        conversation.pending_action_requested_at = utc_now()

    @staticmethod
    def _clear_pending(conversation):
        conversation.pending_followup = None
        conversation.pending_action = None
        conversation.pending_enrollment_id = None
        conversation.pending_target_intake_id = None
        conversation.pending_source_intake_id = None
        conversation.pending_intake_options = None
        conversation.pending_change_data = None
        conversation.pending_action_requested_at = None

    def _intake_choices(self, conversation, message, reason=None):
        if reason:
            reason = reason.split("\n\nAvailable choices:", 1)[0]
        draft = self._pending_owned_draft(conversation) or self._current_or_single(conversation)
        course = self._pending_reschedule_course(conversation, draft) if draft else None
        pending = dict(conversation.pending_change_data or {})
        course_value = (
            course.course_id
            if course is not None
            else pending.get("course_id") or (draft.course_id or draft.course if draft else None)
        )
        current_ids = {
            value
            for value in (
                course.intake_id if course is not None else (draft.intake_id if draft else None),
                course.allocated_intake_id
                if course is not None
                else (draft.allocated_intake_id if draft else None),
            )
            if value
        }
        available = list(
            self.catalogue.selectable_intakes_for(course_value)
            if course_value
            else self.catalogue.selectable_intakes
        )
        available = [item for item in available if item["id"] not in current_ids]
        lowered = (message or "").casefold()
        months = [
            month for month in (
                "January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December",
            ) if month.casefold() in lowered
        ]
        if months:
            available = [
                item for item in available
                if any(month.casefold() in item["display_date"].casefold() for month in months)
            ]
        heading = f"📅 Available {months[0]} Intakes" if len(months) == 1 else "📅 Available Intakes"
        if not available:
            conversation.pending_intake_options = []
            return (reason or "No alternative future open intake is available.") + " Staff confirmation is required."
        conversation.pending_intake_options = [item["id"] for item in available]
        choices = "\n".join(
            f"{number}. {item['display_date']} — {item['intake_type']}"
            for number, item in enumerate(available, start=1)
        )
        prefix = f"{reason}\n\n" if reason else ""
        return f"{prefix}{heading}\n\n{choices}\n\nPlease reply with the intake number or dates you prefer."

    def _match_reschedule_selection(self, conversation, message):
        supplied = _normalise(message)
        option_ids = list(conversation.pending_intake_options or [])
        option = re.fullmatch(r"\d+", supplied)
        if option:
            position = int(option.group())
            if 1 <= position <= len(option_ids):
                selected = self.catalogue.intake_for_id(option_ids[position - 1])
                if selected and selected.get("status") == "open":
                    return selected, None
            return None, "Please choose one of the displayed intake numbers."
        ordinal = re.search(
            r"\b(first|second|third)(?: (?:available )?(?:intake|option|choice))\b",
            supplied,
        )
        if ordinal:
            position = {"first": 1, "second": 2, "third": 3}[ordinal.group(1)]
            if 1 <= position <= len(option_ids):
                selected = self.catalogue.intake_for_id(
                    option_ids[position - 1]
                )
                if selected and selected.get("status") == "open":
                    return selected, None
            return None, "Please choose one of the displayed intake numbers."
        draft = self._pending_owned_draft(conversation) or self._current_or_single(conversation)
        course = self._pending_reschedule_course(conversation, draft) if draft else None
        course_value = course.course_id if course is not None else (draft.course if draft else None)
        selected, error = self.catalogue.match_intake_selection(message, course_value)
        if selected is not None and selected["id"] not in set(option_ids):
            current_ids = {
                value
                for value in (
                    course.intake_id if course is not None else (draft.intake_id if draft else None),
                    course.allocated_intake_id
                    if course is not None
                    else (draft.allocated_intake_id if draft else None),
                )
                if value
            }
            if selected["id"] in current_ids:
                return selected, None
            return None, "Please choose one of the displayed alternative intakes."
        return selected, error

    def _course_started(self, draft):
        start = draft.intake_start_date or draft.preferred_intake_date
        return bool(start and start <= self.intakes.today)

    def _payment_received(self, draft):
        case = FinanceCase.query.filter_by(enrollment_id=draft.id).one_or_none()
        if case and case.status in {"Paid", "Receipt Issued"}:
            return True
        return Payment.query.filter_by(enrollment_id=draft.id).first() is not None

    @staticmethod
    def _duplicate_for(draft, course_name, intake_id):
        if not draft.nric or not draft.mobile_number:
            return False
        return (
            EnrollmentDraft.query.filter(
                EnrollmentDraft.id != draft.id,
                EnrollmentDraft.nric == draft.nric,
                EnrollmentDraft.mobile_number == draft.mobile_number,
                EnrollmentDraft.course == course_name,
                EnrollmentDraft.intake_id == intake_id,
                EnrollmentDraft.status.in_(tuple(ACTIVE_STATUSES | {"awaiting_confirmation"})),
            ).first()
            is not None
        )

    @staticmethod
    def _audit(draft, action, detail):
        db.session.add(
            AuditEvent(
                conversation_id=draft.conversation_id,
                actor="participant",
                action=action,
                detail=mask_nrics_in_text(detail),
            )
        )

    @staticmethod
    def _escalate_once(draft, reason, category):
        existing = StaffEscalation.query.filter_by(
            conversation_id=draft.conversation_id,
            category=category,
            status="open",
        ).first()
        if existing is None:
            db.session.add(
                StaffEscalation(
                    conversation_id=draft.conversation_id,
                    reason=reason,
                    category=category,
                )
            )

    def _update_master_invoice(self, draft):
        if self.master_invoice is None:
            return True
        try:
            self.master_invoice.mark_needs_update(draft.id)
            return bool(
                self.master_invoice.update_exported_enrollment(
                    draft.id, "participant"
                )
            )
        except Exception:
            db.session.rollback()
            return False

    def _refresh_group_invoice(self, draft):
        """Refresh the existing invoice identity after a grouped-course change."""
        master_updated = self._update_master_invoice(draft)
        if self.invoice_templates is None:
            return "disabled" if master_updated else "failed"
        try:
            generation = self.invoice_templates.generate(
                draft, actor="participant", force=True
            )
            if generation is None:
                return "disabled" if master_updated else "failed"
            if generation.status != "generated":
                self._audit(
                    draft,
                    "group_invoice_update_failed",
                    "Participant invoice regeneration is retryable",
                )
                db.session.commit()
                return "failed"
            if self.invoice_email is not None:
                try:
                    self.invoice_email.queue_automatic(draft, generation)
                except Exception:
                    db.session.rollback()
            self._audit(
                draft,
                "group_invoice_updated",
                f"Invoice version {generation.version}; master invoice {'updated' if master_updated else 'retry required'}",
            )
            db.session.commit()
            return "generated" if master_updated else "failed"
        except Exception:
            db.session.rollback()
            self._audit(
                draft,
                "group_invoice_update_failed",
                "Participant invoice regeneration is retryable",
            )
            db.session.commit()
            return "failed"

    def _intake_display(self, draft):
        if draft.intake_id:
            intake = db.session.get(CourseDate, draft.intake_id)
            if intake is not None:
                return _date_range(intake.date, intake.end_date)
        start = draft.intake_start_date or draft.preferred_intake_date
        end = draft.intake_end_date or start
        if not start:
            return "Awaiting staff confirmation"
        return _date_range(start, end)

    @staticmethod
    def _row_display(row):
        return _date_range(row.date, row.end_date)

    @staticmethod
    def _status_label(draft):
        return (draft.status or "draft").replace("_", " ").title()


def is_cancellation_request(message):
    text = _normalise(message)
    if re.search(r"\b(?:change|move|reschedul)\b.*\b(?:date|intake|course)\b", text):
        return False
    if re.search(r"\bcannot attend\b.*\b(?:date|intake|october|november|december)\b", text):
        return False
    return any(
        re.search(pattern, text)
        for pattern in (
            r"^cancel$",
            r"\bcancel (?:my )?(?:enrolment|enrollment|registration|course|intake)\b",
            r"\bcancel (?:my )?.*\bcourse\b",
            r"\bcancel (?:my )?.*\b(?:dental assist\w*|front desk|infection control|sterilisation)\b",
            r"\bcancel (?:my )?.*\bintake\b",
            r"\bwithdraw\b",
            r"\bremove me from (?:the )?course\b",
            r"\bremove (?:course\s*[12]|(?:the )?(?:first|second|1st|2nd) course)\b",
            r"\bdrop (?:course\s*[12]|(?:the )?(?:first|second|1st|2nd) course)\b",
            r"\bremove (?:the )?(?:first|second|1st|2nd|course [12]).*\bcourse\b",
            r"\bcancel (?:the )?(?:first|second|1st|2nd|course [12])\b",
            r"\bcancel .*(?:courses|entire enrollment|entire enrolment|whole registration)\b",
            r"\bderegister (?:me )?(?:from )?\b",
            r"\bonly want to keep (?:the )?(?:first|second|1st|2nd|course [12])\b",
            r"\bcannot attend anymore\b",
            r"\bchanged? my mind\b",
            r"\bdon t want to continue (?:with )?(?:my )?(?:registration|enrolment|enrollment)\b",
        )
    )


def is_course_addition_request(message):
    text = _normalise(message)
    return bool(
        re.search(r"\b(?:want|take|enrol|enroll|register).*(?:both|all) courses\b", text)
        or
        re.search(
            r"\b(?:add|include|also take|also enrol|also enroll|enrol|enroll|register|sign up)\b",
            text,
        )
        and (
            "other course" in text
            or "second course" in text
            or "front desk" in text
            or "dental assist" in text
        )
    )


def _whole_registration_cancellation(message):
    text = _normalise(message)
    return bool(
        re.search(
            r"\b(?:both|all|entire|whole)\b.*\b(?:courses?|registration|enrolment|enrollment)\b",
            text,
        )
        or re.search(r"\bcancel (?:my )?(?:entire|whole) (?:registration|enrolment|enrollment)\b", text)
    )


def is_reschedule_request(message):
    text = _normalise(message)
    if re.search(r"\bdid my reschedul.*work\b", text):
        return False
    return any(
        re.search(pattern, text)
        for pattern in (
            r"\breschedul",
            r"\bchange (?:me |my )?.*\b(?:intake|course date|date)\b",
            r"\bchoose (?:me |my )?(?:an? |the )?(?:other|another|new) (?:intake|date)\b",
            r"\bselected the wrong (?:course )?date\b",
            r"\bmove me to\b",
            r"\bput me in (?:the )?(?:weekend|weekday|next|other)",
            r"\bcannot attend (?:the )?.*(?:date|intake|october|november|december)",
            r"\bchange my course to\b.*\b(?:january|february|march|april|may|june|july|august|september|october|november|december)\b",
        )
    )


def is_status_query(message):
    text = _normalise(message)
    if is_career_question(text):
        return False
    return any(
        re.search(pattern, text)
        for pattern in (
            r"^(?:my )?(?:enrolment|enrollment|registration) status$",
            r"\bwhat(?: s| is) my (?:enrolment|enrollment|registration)? ?status\b",
            r"\bcheck (?:my )?(?:enrolment|enrollment|registration) status\b",
            r"\bcheck my (?:enrolment|enrollment|registration)\b",
            r"\bcheck status(?: lah| leh| ah)?\b",
            r"\bwhich intake did i (?:select|choose)\b",
            r"\bwhich course did i (?:register|enrol|enroll)(?: for)?\b",
            r"\bwhat intake did i (?:select|choose)\b",
            r"\bhas my (?:enrolment|enrollment|registration) been confirmed\b",
            r"\bis my (?:enrolment|enrollment|registration) confirmed\b",
            r"\bdid (?:my )?(?:enrolment|enrollment|registration) go through\b",
            r"\bshow (?:me )?my registration details\b",
            r"\bshow (?:me )?my registration\b",
            r"\bshow (?:me )?my registrations\b",
            r"\bshow (?:me )?all my (?:enrolments|enrollments|courses)\b",
            r"\bwhich courses am i registered for\b",
            r"\bwhich intakes did i (?:select|choose)\b",
            r"\bwhat happened to my cancelled course\b",
            r"\bam i (?:enrolled|registered)\b",
            r"\bwhat course date am i attending\b",
            r"\bdid my reschedul.*work\b",
            r"\bis my (?:enrolment|enrollment|registration) cancelled\b",
            r"\bwhat is left for me to complete\b",
            r"\bwhat s left for me to complete\b",
            r"\bcheck my course registration\b",
            r"\bwhat(?: s| is) my booking status\b",
            r"\bmy course status\b",
            r"^status please$",
            r"^status (?:for|of) enr \d{4} \d{6}$",
            r"\bi forgot which intake i (?:chose|selected)\b",
            r"\bwhat(?: s| is) my status(?: ah)?\b",
        )
    )


def is_participant_training_query(message):
    text = _normalise(message)
    has_personal_context = bool(re.search(r"\b(?:my|i|am i|did i)\b", text))
    return has_personal_context and any(
        re.search(pattern, text)
        for pattern in (
            r"\bwhat time\b",
            r"^course timing$",
            r"^my intake timing$",
            r"\bwhen (?:is|does) my (?:course|class|training|intake)",
            r"\bwhen do i need to attend\b",
            r"\bwhen should i arrive\b",
            r"\bwhat date did i select\b",
            r"\bwhich intake am i in\b",
            r"\bwhat are my class dates\b",
            r"\bwhat day is my (?:enrolment|enrollment|class|course)\b",
            r"\bwhere (?:is|will) my (?:course|class|training)",
            r"\bwhere and when (?:is )?my (?:course|class|training)",
            r"\bmy (?:course|class|training|intake) (?:date|dates|time|timing|venue|location)\b",
            r"\bwhat (?:course|intake) (?:am i|did i)",
        )
    )


def is_participant_fee_query(message):
    text = _normalise(message)
    return bool(
        re.search(r"\b(?:my|this) course\b", text)
        and re.search(r"\b(?:fee|fees|cost|price|how much)\b", text)
    )


def is_invoice_resend_request(message):
    text = _normalise(message)
    return any(
        phrase in text
        for phrase in (
            "resend my invoice",
            "resend invoice",
            "email my invoice again",
            "did not receive my invoice",
            "send the pdf to my email",
            "send my invoice again",
            "can you resend it",
            "please resend it",
            "resend it",
        )
    )


def is_general_change_request(message):
    text = _normalise(message)
    return bool(
        re.search(
            r"\b(?:change|update|correct) (?:my )?(?:enrolment|enrollment|details|course)\b",
            text,
        )
        or "change or cancel" in text
    )


def is_course_change_request(message):
    text = _normalise(message)
    return bool(
        re.search(r"\b(?:change|switch|move|update) (?:my )?course\b", text)
        or "chose the wrong course" in text
        or "selected the wrong course" in text
    )


def is_specific_change_request(message):
    text = _normalise(message)
    return bool(
        re.search(
            r"\b(?:change|update|correct) (?:my )?"
            r"(?:name|email|e mail|mobile|phone|date of birth|dob)\b",
            text,
        )
    )


def _normalise(message):
    text = re.sub(r"[^a-z0-9\s]", " ", (message or "").casefold())
    return " ".join(text.split())


def _confirms_cancellation(message):
    return _normalise(message) in {
        "confirm cancellation", "confirm course cancellation", "confirm cancel",
        "yes", "yes please",
    }


def _confirms_reschedule(message):
    return _normalise(message) in {
        "confirm reschedule", "confirm intake change", "confirm change", "yes", "yes please"
    }


def _confirms_resend(message):
    return _normalise(message) == "confirm resend"


def _confirms_change(message):
    return _normalise(message) == "confirm change"


def _confirms_course_addition(message):
    return _normalise(message) in {
        "confirm", "confirm change", "confirm addition", "confirm course addition",
        "yes", "yes please",
    }


def _keeps_invoice(message):
    return _normalise(message) == "keep invoice"


def _change_option(message):
    text = _normalise(message)
    if text in {"1", "participant details", "details"}:
        return "participant"
    if text in {"2", "course"}:
        return "course"
    if text in {"3", "intake", "intake date", "date"}:
        return "intake"
    if text in {"4", "cancel", "cancel enrollment", "cancel enrolment"}:
        return "cancel"
    return None


def _confirms_draft_discard(message):
    return _normalise(message) in {"discard draft", "confirm discard", "yes", "yes please"}


def _explicit_discard_command(message):
    return _normalise(message) in {"discard draft", "confirm discard"}


def _keeps_enrollment(message):
    return _normalise(message) in {
        "keep enrollment", "keep enrolment", "keep both courses", "do not cancel",
        "don t cancel", "cancel the request", "cancel request", "never mind",
        "nevermind",
    }


def _accepts_additional_course(message):
    return _normalise(message) in {
        "yes", "yes please", "yes add another course", "add another course",
        "add the other course", "i want another course",
        "show me the remaining course",
    }


def _declines_additional_course(message):
    return _normalise(message) in {
        "no", "no thanks", "no thank you", "that s all", "thats all",
        "that is all", "i m done", "im done",
    }


def _is_numeric_menu_attempt(message):
    return bool(re.fullmatch(r"(?:course\s*)?\d+", _normalise(message)))


def _both_courses_choice(message):
    return bool(
        re.fullmatch(
            r"(?:cancel\s+)?(?:both|all)(?:\s+(?:of\s+the\s+)?courses?)?",
            _normalise(message),
        )
    )


def _keeps_draft(message):
    return _normalise(message) in {"keep draft", "continue later"}


def _keeps_current_intake(message):
    return _normalise(message) in {
        "keep current intake", "keep my intake", "keep enrollment", "keep enrolment",
        "do not reschedule", "don t reschedule",
    }


def _confirmation_without_pending(message):
    return _normalise(message) in {
        "confirm cancellation", "confirm cancel", "confirm reschedule", "confirm intake change",
        "confirm course cancellation", "confirm resend", "confirm change",
        "confirm addition", "confirm course addition",
    }


def _looks_like_intake_selection(message):
    text = _normalise(message)
    return bool(
        re.fullmatch(r"\d+", text)
        or re.search(
            r"\b(?:first|second|third)(?: (?:available )?(?:intake|option|choice))\b",
            text,
        )
        or re.search(r"\b\d{1,2}(?:st|nd|rd|th)? (?:january|february|march|april|may|june|july|august|september|october|november|december)\b", text)
        or re.search(r"\b(?:january|february|march|april|may|june|july|august|september|october|november|december)(?:\s+20\d{2})?\b", text)
        or re.search(r"\b(?:weekend|weekday) intake\b", text)
    )


def _looks_like_course_selection(catalogue, message):
    text = _normalise(message)
    if re.fullmatch(r"(?:course\s*)?\d+", text):
        return True
    if re.match(r"^(?:what|when|where|why|how|can|could|is|are|do|does)\b", text):
        return False
    selected, _ = catalogue.match_course_selection(message)
    return bool(selected or catalogue.course_for(message))


def _safe_reason(message):
    match = re.search(r"(?i)\b(?:because|reason\s*:)\s*(.+)$", message or "")
    if not match:
        return None
    reason = mask_nrics_in_text(match.group(1).strip())[:500]
    return reason or None


def _date_range(start, end):
    if start == end:
        return f"{start.day} {start.strftime('%B %Y')}"
    if start.year == end.year and start.month == end.month:
        return f"{start.day}–{end.day} {start.strftime('%B %Y')}"
    if start.year == end.year:
        return f"{start.day} {start.strftime('%B')}–{end.day} {end.strftime('%B %Y')}"
    return f"{start.day} {start.strftime('%B %Y')}–{end.day} {end.strftime('%B %Y')}"


def _first_name(full_name):
    return full_name.split()[0] if full_name else None


def _mask_email(value):
    local, separator, domain = str(value or "").partition("@")
    if not separator:
        return "the registered email address"
    return f"{local[:1]}***@{domain}"


def _json_value(value):
    if isinstance(value, (date, datetime)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _as_utc(value):
    from datetime import timezone

    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _safe_change_display(field, value):
    if field == "email":
        return _mask_email(value)
    if field == "mobile_number":
        digits = str(value)
        return f"****{digits[-4:]}"
    if field == "date_of_birth":
        return "Updated date provided"
    return str(value)


__all__ = [
    "AWAITING_CANCELLATION",
    "AWAITING_RESCHEDULE_CONFIRMATION",
    "AWAITING_RESCHEDULE_COURSE_SELECTION",
    "AWAITING_RESCHEDULE_SELECTION",
    "EnrollmentManagementService",
    "ManagementResult",
    "is_cancellation_request",
    "is_general_change_request",
    "is_invoice_resend_request",
    "is_specific_change_request",
    "is_reschedule_request",
    "is_status_query",
]
