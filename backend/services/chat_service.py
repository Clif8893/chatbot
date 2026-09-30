"""Conversation orchestration with application-controlled agent routing."""

import re
from difflib import get_close_matches
from datetime import timezone

from backend.agents.schemas import IntentClassification
from backend.extensions import db
from backend.services.enrollment_service import EnrollmentService
from backend.services.faq_attachments import FAQAttachmentCatalogue
from backend.services.privacy import mask_nrics_in_text, redact_for_classification
from backend.services.invoice_email_service import InvoiceEmailError, ResendCooldownError
from backend.services.enrollment_management_service import is_course_change_request


UNCLEAR_REPLY = (
    "Hi! 👋 Would you like to learn more about the course, or would you like to enrol?"
)
ENROLLMENT_OFFER_FOLLOWUP = "enrollment_offer"
ENROLLMENT_CONFIRMATION_FOLLOWUP = "enrollment_confirmation"


class ChatService:
    def __init__(
        self,
        repository,
        catalogue,
        ai_service,
        context_limit=20,
        master_invoice_service=None,
        invoice_template_service=None,
        invoice_email_service=None,
        intake_service=None,
        enrollment_management_service=None,
        enrollment_resolver=None,
        validation_options=None,
    ):
        self.repository = repository
        self.catalogue = catalogue
        self.ai_service = ai_service
        self.context_limit = context_limit
        self.enrollment = EnrollmentService(
            ai_service,
            catalogue,
            repository,
            context_limit=context_limit,
            intake_service=intake_service,
            validation_options=validation_options,
        )
        self.faq_attachments = FAQAttachmentCatalogue()
        self.master_invoice_service = master_invoice_service
        self.invoice_template_service = invoice_template_service
        self.invoice_email_service = invoice_email_service
        self.enrollment_management = enrollment_management_service
        self.enrollment_resolver = enrollment_resolver

    def respond(self, conversation, user_text):
        user_message = self.repository.add_message(
            conversation, "user", user_text, agent_name="intent_classifier"
        )
        db.session.commit()
        recent_messages = self.repository.recent_messages(
            conversation, self.context_limit + 1
        )
        prior_messages = [
            message for message in recent_messages if message.id != user_message.id
        ][-self.context_limit :]
        prior_messages = _context_since_last_draft_discard(prior_messages)
        last_assistant_message = next(
            (
                message.content
                for message in reversed(prior_messages)
                if message.role == "assistant"
            ),
            None,
        )

        draft = conversation.enrollment_draft
        if self.enrollment_management is not None:
            management = self.enrollment_management.handle(conversation, user_text)
            if management is not None:
                if management.action in {"draft_discarded", "draft_already_discarded"}:
                    conversation.active_intent = None
                else:
                    conversation.active_intent = "enrollment"
                return self._direct_reply(
                    conversation,
                    user_message,
                    management.draft,
                    management.reply,
                    intent="enrollment",
                    agent_name="enrollment_agent",
                )

        course_fact_topic = _course_fact_topic(user_text)
        if course_fact_topic:
            selected_course = self.catalogue.match_course(user_text)
            if selected_course is None and self.enrollment_resolver is not None:
                context = self.enrollment_resolver.resolve(conversation, user_text)
                if context.ambiguous:
                    reply = self.enrollment_resolver.clarification_reply(context)
                elif context.course is not None:
                    selected_course = context.course
                    reply = self.catalogue.focused_course_reply(selected_course, course_fact_topic)
                else:
                    reply = self.catalogue.course_clarification_reply()
            elif selected_course is None:
                reply = self.catalogue.course_clarification_reply()
            else:
                reply = self.catalogue.focused_course_reply(selected_course, course_fact_topic)
            name = _first_name(draft.full_name) if draft else None
            if draft and draft.status not in {
                "confirmed",
                "awaiting_course_date",
                "cancelled",
            }:
                if name and reply != self.catalogue.no_intakes_message:
                    reply = _personalise_reply(reply, name)
                reply += (
                    "\n\nWhen you’re ready, you can continue with your remaining "
                    "enrollment details 😊"
                )
            elif name and reply != self.catalogue.no_intakes_message:
                reply = _personalise_reply(reply, name)
            return self._direct_reply(
                conversation, user_message, draft, reply,
                intent="faq", agent_name="faq_agent",
            )
        if _is_course_catalogue_request(user_text):
            reply = (
                "📚 Available Courses\n\n"
                f"{self.catalogue.course_choices()}\n\n"
                "Reply with a course number or name if you would like details or want to enrol."
            )
            return self._direct_reply(
                conversation, user_message, draft, reply,
                intent="faq", agent_name="faq_agent",
            )
        if (
            "infection" in user_text.casefold()
            and "control" in user_text.casefold()
            and not is_course_change_request(user_text)
            and not (draft and conversation.active_intent == "enrollment")
        ):
            return self._direct_reply(
                conversation,
                user_message,
                draft,
                self.catalogue.course_overview_reply("dental-infection-control"),
                intent="faq",
                agent_name="faq_agent",
            )
        pending_intake = self.enrollment.handle_pending_intake_selection(
            conversation, user_text
        )
        if pending_intake is not None:
            reply, draft = pending_intake
            conversation.active_intent = "enrollment"
            return self._direct_reply(
                conversation,
                user_message,
                draft,
                reply,
                intent="enrollment",
                agent_name="enrollment_agent",
            )

        if (
            draft is not None
            and draft.status not in {"discarded", "cancelled"}
            and _is_enrollment_progress_request(user_text)
        ):
            conversation.active_intent = "enrollment"
            waiting_for_intake = self.enrollment.remember_intake_choices(
                conversation, draft
            )
            if not waiting_for_intake:
                conversation.pending_followup = (
                    ENROLLMENT_CONFIRMATION_FOLLOWUP
                    if draft.status == "awaiting_confirmation"
                    else None
                )
            return self._direct_reply(
                conversation,
                user_message,
                draft,
                self.enrollment.progress_reply(draft),
                intent="enrollment",
                agent_name="enrollment_agent",
            )

        invoice_email_request = _invoice_email_request_kind(user_text)
        if invoice_email_request:
            if self.enrollment_resolver is not None:
                invoice_context = self.enrollment_resolver.resolve(conversation, user_text)
                if invoice_context.ambiguous:
                    return self._direct_reply(
                        conversation, user_message, None,
                        self.enrollment_resolver.clarification_reply(invoice_context),
                        intent="enrollment", agent_name="enrollment_agent",
                    )
                draft = invoice_context.enrollment
            conversation.active_intent = "enrollment"
            reply = self._invoice_email_reply(draft, invoice_email_request)
            result = self._direct_reply(
                conversation,
                user_message,
                draft,
                reply,
                intent="enrollment",
                agent_name="enrollment_agent",
            )
            result["show_invoice_action"] = True
            if draft is not None and self.invoice_email_service is not None:
                result["invoice_email"] = self.invoice_email_service.serialize(draft)
            return result

        if _is_invoice_request(user_text):
            if self.enrollment_resolver is not None:
                invoice_context = self.enrollment_resolver.resolve(conversation, user_text)
                if invoice_context.ambiguous:
                    return self._direct_reply(
                        conversation, user_message, None,
                        self.enrollment_resolver.clarification_reply(invoice_context),
                        intent="enrollment", agent_name="enrollment_agent",
                    )
                draft = invoice_context.enrollment
            conversation.active_intent = "enrollment"
            result = self._direct_reply(
                conversation,
                user_message,
                draft,
                self._invoice_status_reply(draft),
                intent="enrollment",
                agent_name="enrollment_agent",
            )
            result["show_invoice_action"] = True
            return result

        if _is_intake_availability_request(user_text):
            reply = self.catalogue.approved_faq_reply(["course dates"])
            if draft and draft.status not in {
                "confirmed", "awaiting_course_date", "cancelled"
            }:
                self.enrollment.remember_intake_choices(conversation, draft)
                reply += f"\n\n{self.enrollment.next_step_prompt(draft)}"
            return self._direct_reply(
                conversation,
                user_message,
                draft,
                clean_customer_copy(reply),
                intent="faq",
                agent_name="faq_agent",
            )

        contextual_decision = _contextual_decision(
            user_text, conversation.pending_followup
        )
        if (
            contextual_decision == "reject"
            and conversation.pending_followup == ENROLLMENT_OFFER_FOLLOWUP
        ):
            if draft and _is_empty_course_offer_draft(draft):
                db.session.delete(draft)
                draft = None
            user_message.detected_intent = "faq"
            user_message.classification_confidence = 1.0
            conversation.active_intent = "faq"
            conversation.pending_followup = None
            reply = (
                "No problem. I’m happy to answer any other course questions "
                "whenever you’re ready."
            )
            assistant_message = self.repository.add_message(
                conversation,
                "assistant",
                reply,
                agent_name="intent_classifier",
                detected_intent="faq",
                classification_confidence=1.0,
            )
            db.session.commit()
            return {
                "conversation_id": conversation.id,
                "message": serialize_message(assistant_message),
                "routing": {
                    "intent": "faq",
                    "confidence": 1.0,
                    "agent": "intent_classifier",
                },
                "enrollment": serialize_enrollment(draft, self.catalogue),
            }

        greeting = _greeting_salutation(user_text)
        if greeting:
            name = _first_name(draft.full_name) if draft else None
            if draft and conversation.active_intent == "enrollment" and draft.status not in {
                "confirmed",
                "awaiting_course_date",
                "cancelled",
            }:
                personal = f", {name}" if name else ""
                reply = (
                    f"{greeting}{personal}! 👋 We can continue your enrolment. "
                    f"{self.enrollment.next_step_prompt(draft)}"
                )
            else:
                reply = (
                    f"{greeting}! 👋 How can I help you today? You can ask about "
                    "our courses, fees, SkillsFuture support, or start an enrolment."
                )
            assistant_message = self.repository.add_message(
                conversation,
                "assistant",
                reply,
                agent_name="intent_classifier",
                detected_intent="unclear",
                classification_confidence=1.0,
            )
            db.session.commit()
            return {
                "conversation_id": conversation.id,
                "message": serialize_message(assistant_message),
                "routing": {
                    "intent": "unclear",
                    "confidence": 1.0,
                    "agent": "intent_classifier",
                },
                "enrollment": serialize_enrollment(draft, self.catalogue),
            }

        enrollment_message = user_text
        if contextual_decision:
            classification = IntentClassification(
                intent="enrollment",
                confidence=1.0,
                short_reason="Contextual response to the persisted enrollment state.",
            )
            enrollment_message = (
                "Confirm"
                if conversation.pending_followup
                == ENROLLMENT_CONFIRMATION_FOLLOWUP
                and contextual_decision == "confirm"
                else "not correct"
                if conversation.pending_followup
                == ENROLLMENT_CONFIRMATION_FOLLOWUP
                else "Yes, enrol me in this course"
            )
            conversation.pending_followup = None
        elif self.enrollment.is_contextual_stage_reply(
            draft, user_text, last_assistant_message
        ):
            classification = IntentClassification(
                intent="enrollment",
                confidence=1.0,
                short_reason="Contextual answer to the latest enrollment question.",
            )
        else:
            classification = self.ai_service.classify(
                message=redact_for_classification(user_text),
                context=self._format_context(prior_messages, redact=True),
                active_intent=conversation.active_intent,
                draft_status=draft.status if draft else None,
                pending_followup=conversation.pending_followup,
                draft_summary=self._classification_draft_summary(draft),
            )
        if classification.intent == "unclear" and (
            _is_intake_question(user_text) or _is_utap_question(user_text)
        ):
            classification.intent = "faq"
        user_message.detected_intent = classification.intent
        user_message.classification_confidence = classification.confidence

        confirmed_now = False
        was_confirmed = False
        if classification.intent == "faq":
            name = _first_name(draft.full_name) if draft else None
            faq_result = self.ai_service.answer_faq(
                message=redact_for_classification(user_text),
                context=self._format_context(prior_messages, redact=True),
                catalogue_context=self.catalogue.agent_context(),
                customer_first_name=name,
            )
            matched_topics = _ground_faq_topics_to_current_message(
                user_text, faq_result.matched_topics
            )
            if not self.catalogue.has_intake_dates and _is_intake_question(user_text):
                reply = self.catalogue.no_intakes_message
            elif (
                self.catalogue.venue_confirmation_required
                and _is_venue_question(user_text)
            ):
                reply = self.catalogue.approved_faq_reply(["venue"])
            elif _is_utap_question(user_text):
                reply = self.catalogue.approved_faq_reply(["UTAP"])
            else:
                approved_reply = self.catalogue.approved_faq_reply(matched_topics)
                reply = self.catalogue.ground_faq_answer(approved_reply)
            reply = clean_customer_copy(reply)
            agent_name = "faq_agent"
            if draft and draft.status not in {
                "confirmed",
                "awaiting_course_date",
                "cancelled",
            }:
                if name and reply != self.catalogue.no_intakes_message:
                    reply = _personalise_reply(reply, name)
                reply += (
                    "\n\nWhen youâ€™re ready, you can continue with your remaining "
                    "enrollment details ðŸ˜Š"
                )
            else:
                if name and reply != self.catalogue.no_intakes_message:
                    reply = _personalise_reply(reply, name)
            if conversation.active_intent != "enrollment":
                conversation.active_intent = "faq"
            if draft and draft.course:
                if draft.status == "awaiting_confirmation":
                    conversation.pending_followup = ENROLLMENT_CONFIRMATION_FOLLOWUP
            else:
                conversation.pending_followup = (
                    ENROLLMENT_OFFER_FOLLOWUP
                    if _offers_enrollment(reply)
                    else None
                )
            attachment = self.faq_attachments.for_reply(user_text, matched_topics)
        elif classification.intent == "enrollment":
            conversation.active_intent = "enrollment"
            was_confirmed = bool(draft and draft.confirmed_at)
            reply, draft = self.enrollment.handle(
                conversation,
                enrollment_message,
                self._format_context(prior_messages, redact=True),
                last_assistant_message=last_assistant_message,
            )
            waiting_for_intake = self.enrollment.remember_intake_choices(
                conversation, draft
            )
            if waiting_for_intake:
                pass
            elif draft.course is None:
                conversation.pending_followup = (
                    ENROLLMENT_OFFER_FOLLOWUP
                    if len(self.catalogue.courses) == 1
                    else None
                )
            elif draft.status == "awaiting_confirmation":
                conversation.pending_followup = ENROLLMENT_CONFIRMATION_FOLLOWUP
            else:
                conversation.pending_followup = None
            agent_name = "enrollment_agent"
            attachment = None
            confirmed_now = bool(draft.confirmed_at) and not was_confirmed
            if draft.confirmed_at:
                conversation.pending_intake_options = None
        else:
            reply = UNCLEAR_REPLY
            agent_name = "intent_classifier"
            attachment = None

        assistant_message = self.repository.add_message(
            conversation,
            "assistant",
            reply,
            agent_name=agent_name,
            detected_intent=classification.intent,
            classification_confidence=classification.confidence,
            image_url=attachment["image_url"] if attachment else None,
            image_alt=attachment["image_alt"] if attachment else None,
            image_status=attachment["image_status"] if attachment else None,
        )
        db.session.commit()

        if confirmed_now and self.master_invoice_service is not None:
            try:
                exported = self.master_invoice_service.queue_and_export(draft)
                if exported and self.invoice_template_service is not None:
                    document = self.invoice_template_service.generate(draft)
                    if (
                        document.status == "generated"
                        and self.invoice_email_service is not None
                    ):
                        self.invoice_email_service.queue_automatic(draft, document)
            except Exception:
                # The confirmed database record is deliberately independent of the
                # recoverable workbook export.
                db.session.rollback()
        elif (
            was_confirmed
            and not draft.confirmed_at
            and self.master_invoice_service is not None
        ):
            try:
                self.master_invoice_service.mark_needs_update(draft.id)
            except Exception:
                db.session.rollback()

        result = {
            "conversation_id": conversation.id,
            "message": serialize_message(assistant_message),
            "routing": {
                "intent": classification.intent,
                "confidence": classification.confidence,
                "agent": agent_name,
            },
            "enrollment": serialize_enrollment(draft, self.catalogue),
        }
        if confirmed_now:
            result["show_invoice_action"] = True
            if self.invoice_email_service is not None:
                result["invoice_email"] = self.invoice_email_service.serialize(draft)
        return result

    def _direct_reply(
        self, conversation, user_message, draft, reply, *, intent, agent_name
    ):
        """Persist an application-generated answer without invoking CrewAI."""
        user_message.detected_intent = intent
        user_message.classification_confidence = 1.0
        assistant_message = self.repository.add_message(
            conversation,
            "assistant",
            reply,
            agent_name=agent_name,
            detected_intent=intent,
            classification_confidence=1.0,
        )
        db.session.commit()
        return {
            "conversation_id": conversation.id,
            "message": serialize_message(assistant_message),
            "routing": {
                "intent": intent,
                "confidence": 1.0,
                "agent": agent_name,
            },
            "enrollment": serialize_enrollment(draft, self.catalogue),
        }

    def _invoice_status_reply(self, draft):
        if draft is None or draft.status == "cancelled" or not draft.confirmed_at:
            return (
                "An invoice is unavailable until your enrollment has been completed "
                "and confirmed."
            )
        if self.invoice_template_service is None:
            return "Your invoice is not available yet. Please check again later."
        record = self.invoice_template_service.for_enrollment(draft.id)
        if record is None or record.status == "pending":
            return "Your invoice is being generated. Please check the invoice status again shortly."
        if record.status == "generated":
            return "Your invoice is ready. Select Download Invoice in Enrollment & payment."
        if record.status == "needs_review":
            return (
                "Your invoice needs staff review before it can be downloaded. "
                "Please check again after our team has reviewed it."
            )
        return (
            "Invoice generation was not completed. Our finance team can safely retry it; "
            "your confirmed enrollment is unchanged."
        )

    def _invoice_email_reply(self, draft, request_kind):
        if draft is None or draft.status == "cancelled" or not draft.confirmed_at:
            return (
                "An invoice email is unavailable until your enrollment has been "
                "completed and confirmed."
            )
        if self.invoice_email_service is None:
            return "Invoice email delivery is not available yet."
        if request_kind == "resend":
            try:
                self.invoice_email_service.participant_resend(draft)
            except ResendCooldownError as error:
                return str(error)
            except InvoiceEmailError as error:
                return str(error)
        status = self.invoice_email_service.serialize(draft)
        if status["status"] == "sent":
            return f"Your invoice has been emailed to {status['recipient_masked']}."
        if status["status"] in {"pending", "sending"}:
            return "Your invoice email is being prepared. Please check again shortly."
        if status["status"] == "disabled":
            return "Email delivery is not configured. You can still download your invoice."
        if status["status"] == "failed":
            return (
                "Your enrollment is confirmed and your invoice is ready, but the email "
                "could not be delivered. You can still download it or request another attempt."
            )
        return "Your invoice must be generated before it can be emailed."

    @staticmethod
    def _format_context(messages, redact=False):
        lines = []
        for message in messages:
            content = (
                redact_for_classification(message.content)
                if redact
                else mask_nrics_in_text(message.content)
            )
            lines.append(f"{message.role}: {content}")
        return "\n".join(lines)

    def _classification_draft_summary(self, draft):
        """Return state and field names only; never participant identity values."""
        if draft is None:
            return None
        tracked_fields = (
            "course",
            "preferred_intake_date",
            "full_name",
            "nric",
            "date_of_birth",
            "email",
            "mobile_number",
            "payment_method",
            "skillsfuture_amount",
            "paynow_amount",
        )
        return {
            "status": draft.status,
            "saved_fields": [
                field for field in tracked_fields if getattr(draft, field) is not None
            ],
            "missing_fields": self.enrollment.all_missing_fields(draft),
            "invalid_fields": sorted((draft.validation_errors or {}).keys()),
        }


def serialize_message(message):
    timestamp = message.created_at
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    serialized = {
        "id": message.id,
        "role": message.role,
        "content": clean_customer_copy(mask_nrics_in_text(message.content))
        if message.role == "assistant"
        else mask_nrics_in_text(message.content),
        "agent_name": message.agent_name,
        "detected_intent": message.detected_intent,
        "classification_confidence": message.classification_confidence,
        "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
    }
    if message.image_url and message.image_alt:
        serialized.update(
            image_url=message.image_url,
            image_alt=message.image_alt,
            image_status=message.image_status,
        )
    return serialized


def serialize_enrollment(draft, catalogue=None):
    if draft is None:
        return {
            "status": None,
            "missing_fields": [],
            "demo_intakes_enabled": bool(
                catalogue and catalogue.enable_demo_intakes
            ),
            "selected_intake": None,
        }
    selected_intake = None
    if draft.intake_start_date:
        selected_intake = {
            "id": draft.intake_id,
            "start_date": draft.intake_start_date.isoformat(),
            "end_date": (
                draft.intake_end_date or draft.intake_start_date
            ).isoformat(),
            "display_date": _display_intake_range(
                draft.intake_start_date,
                draft.intake_end_date or draft.intake_start_date,
            ),
            "is_demo": bool(draft.intake_is_demo),
        }
    return {
        "status": draft.status,
        "course_id": draft.course_id,
        "course_name": draft.course,
        "course_fee": str(draft.course_fee) if draft.course_fee is not None else None,
        "friendly_reference": draft.friendly_reference,
        "missing_fields": EnrollmentService.missing_fields(draft, catalogue),
        "demo_intakes_enabled": bool(
            catalogue and catalogue.enable_demo_intakes
        ),
        "selected_intake": selected_intake,
    }


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


def clean_customer_copy(content):
    """Remove wording retained in older local conversation records."""
    cleaned = re.sub(
        r"(?i)prototype enrollment summary \(not an official Q&M enrollment\):",
        "Please confirm your enrolment details:",
        content or "",
    )
    cleaned = re.sub(
        r"(?i)your enrollment draft is still saved\. We can continue it whenever you are ready\.",
        "We can continue your enrolment whenever you’re ready.",
        cleaned,
    )
    cleaned = re.sub(
        r"(?im)^prototype notice:.*(?:\r?\n)*",
        "",
        cleaned,
    )
    cleaned = re.sub(
        r"(?i)this is not (?:an? )?official Q&M (?:information|enrollment)\.?",
        "",
        cleaned,
    )
    cleaned = "\n".join(
        line
        if re.match(r"(?i)^demo (?:intake dates|note):?", line.strip())
        else re.sub(
            r"(?i)\b(?:fictional|synthetic|mock|prototype)\b\s*", "", line
        )
        for line in cleaned.splitlines()
    )
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if re.search(
        r"(?i)\b(?:crewai|openai|intent classification|routing|mock response|next stage)\b",
        cleaned,
    ):
        return "I can help with course information or guide you through enrolment."
    return cleaned or "I’m sorry, I don’t have that information at the moment."


def _first_name(full_name):
    return full_name.split()[0] if full_name else None


def _is_intake_question(message):
    lowered = message.casefold()
    return _contains_close_word(lowered, "intake") or any(
        term in lowered
        for term in (
            "course date",
            "intake",
            "available date",
            "dates are available",
            "weekend date",
            "weekday date",
            "next class",
            "next course",
            "when is the course",
            "when are the classes",
        )
    )


def _is_intake_availability_request(message):
    lowered = " ".join((message or "").casefold().split())
    words = re.findall(r"[a-z]+", lowered)
    question_opening = bool(
        re.match(r"^(?:what|which|when|show|are there|do you have)\b", lowered)
    )
    short_request = len(words) <= 6 and any(
        term in lowered
        for term in ("available intake", "next intake", "course dates")
    )
    return _is_intake_question(message) and (
        "?" in message or question_opening or short_request
    )


def _is_enrollment_progress_request(message):
    lowered = " ".join((message or "").casefold().split())
    # Keep clear course/funding questions in FAQ routing even during enrollment.
    if _is_fee_question(lowered) or _is_skillsfuture_question(lowered):
        return False
    patterns = (
        r"\bwhat (?:information|details|fields) do you still need\b",
        r"\bwhat (?:information|details) (?:have )?i (?:already )?(?:provided|given|give)\b",
        r"\bwhat (?:details|information) i give already\b",
        r"\bwhat (?:details|information|fields) (?:are )?missing\b",
        r"\bshow (?:me )?(?:my )?(?:enrolment|enrollment|registration) progress\b",
        r"\bwhat(?:'s| is) left\b",
        r"\bwhich fields (?:have i completed|are complete)\b",
        r"\bwhat do i need to correct\b",
        r"\bhave i completed (?:my )?registration\b",
        r"\bwhat else (?:do )?you need\b",
        r"\bwhat have i (?:already )?(?:provided|given)\b",
        r"\bwhat (?:other )?(?:details|information) do you need\b",
        r"\bwhat (?:information|details) have i not provided\b",
        r"\bwhat do i still need to send\b",
        r"\bwhat do you need from me now\b",
        r"\bwhat(?:'s| is)? (?:the )?next step\b",
        r"\banything else\b",
        r"\banything missing\b",
        r"\bwhat(?:'s| is) missing(?: ah)?\b",
        r"\bso now need what\b",
        r"\bwhat (?:information|details) is left\b",
        r"\b(?:can we |okay[,.]? |ok[,.]? )?continue(?: my| with my| the)? (?:enrolment|enrollment|registration)\b",
        r"\bcontinue from where we (?:stopped|left off)\b",
        r"\b(?:can we |okay[,.]? |ok[,.]? )?continue[?!.]*\s*$",
        r"\b(?:resume|carry on)(?: my| with my| the)? (?:enrolment|enrollment|registration)\b",
        r"^next[?!.]*$",
    )
    return any(re.search(pattern, lowered) for pattern in patterns)


def _context_since_last_draft_discard(messages):
    """Keep history visible while preventing a discarded draft from seeding AI context."""
    boundary = -1
    for index, message in enumerate(messages):
        if (
            message.role == "assistant"
            and "enrollment draft has been discarded" in message.content.casefold()
        ):
            boundary = index
    return messages[boundary + 1 :]


def _ground_faq_topics_to_current_message(message, matched_topics):
    """Reject stale high-risk FAQ topics not supported by the current user turn."""
    topics = [str(topic) for topic in matched_topics or []]
    grounded = []
    for topic in topics:
        normalized = topic.casefold()
        if "skillsfuture" in normalized and not _is_skillsfuture_question(message):
            continue
        if "fee" in normalized and not _is_fee_question(message):
            continue
        grounded.append(topic)
    return grounded


def _is_skillsfuture_question(message):
    lowered = str(message or "").casefold()
    return _contains_close_word(lowered, "skillsfuture") or bool(
        re.search(r"\bmid[- ]?career\b.*\b(?:credit|tier|fund)", lowered)
    )


def _is_fee_question(message):
    lowered = str(message or "").casefold()
    return bool(
        re.search(
            r"\b(?:fee|fees|cost|costs|price|pricing|expensive)\b|"
            r"\b(?:how|hw)\s+(?:much|mutch)\b",
            lowered,
        )
    )


def _invoice_email_request_kind(message):
    lowered = " ".join((message or "").casefold().split())
    if re.search(
        r"\b(?:did you|have you) email(?:ed)? (?:me )?(?:my |the )?invoice\b|"
        r"\bwhich email (?:address )?did you send (?:it|the invoice) to\b|"
        r"\bemail status (?:for )?(?:my |the )?invoice\b",
        lowered,
    ):
        return "status"
    if re.search(
        r"\bemail (?:me )?(?:my |the )?invoice\b|"
        r"\bresend (?:me )?(?:my |the )?invoice\b|"
        r"\bsend (?:me )?(?:my |the )?invoice again\b",
        lowered,
    ):
        return "resend"
    return None


def _is_course_catalogue_request(message):
    text = " ".join(str(message or "").casefold().split())
    return any(
        phrase in text
        for phrase in (
            "what courses are available",
            "which courses are available",
            "what course do you offer",
            "what courses do you offer",
            "list the courses",
            "show me the courses",
        )
    )


def _course_fact_topic(message):
    """Identify focused generic course questions before broad FAQ classification."""
    text = re.sub(r"[^a-z0-9\s]", " ", str(message or "").casefold())
    text = " ".join(text.split())
    if re.search(r"\b(?:fee|fees|cost|price|how much)\b", text) and re.search(
        r"\b(?:what|how|check|tell|is|are|does|course|workshop)\b", text
    ):
        return "fee"
    if re.search(r"\b(?:what time|timing|hours|start|end|how long|duration)\b", text) and re.search(
        r"\b(?:what|when|how|does|is|are|course|workshop|class)\b", text
    ):
        return "time"
    if re.search(r"\b(?:venue|location|where|held)\b", text):
        return "venue"
    if not re.search(r"\b(?:skillsfuture|mid career|utap)\b", text) and re.search(
        r"\b(?:what|which|how|can|payment options|paynow details)\b", text
    ) and re.search(r"\b(?:payment|pay|paynow)\b", text):
        return "payment"
    return None


def _is_invoice_request(message):
    lowered = " ".join((message or "").casefold().split())
    return any(
        re.search(pattern, lowered)
        for pattern in (
            r"\bdownload (?:my )?invoice\b",
            r"\bcan i (?:get|have|download) (?:my )?invoice\b",
            r"\bshow (?:me )?(?:my )?invoice\b",
            r"\bwhere is (?:my )?invoice\b",
            r"\binvoice status\b",
        )
    )


def _is_utap_question(message):
    return _contains_close_word(message.casefold(), "utap")


def _contains_close_word(message, expected):
    words = re.findall(r"[a-z]+", message.casefold())
    return bool(get_close_matches(expected, words, n=1, cutoff=0.7))


def _is_venue_question(message):
    lowered = message.casefold()
    return any(term in lowered for term in ("where", "venue", "location", "held"))


def _personalise_reply(reply, first_name):
    if re.search(rf"(?i)\b{re.escape(first_name)}\b", reply):
        return reply
    return f"Hi {first_name} 👋\n\n{reply}"


def _greeting_salutation(message):
    normalized = re.sub(r"[^a-z\s]", "", (message or "").casefold())
    normalized = " ".join(normalized.split())
    greetings = {
        "good morning": "Good morning",
        "gud morning": "Good morning",
        "morning": "Good morning",
        "morningg": "Good morning",
        "good afternoon": "Good afternoon",
        "gud afternoon": "Good afternoon",
        "afternoon": "Good afternoon",
        "good evening": "Good evening",
        "gud evening": "Good evening",
        "evening": "Good evening",
        "hi": "Hi",
        "hii": "Hi",
        "hello": "Hello",
        "helo": "Hello",
        "hey": "Hey",
        "heyy": "Hey",
        "greeting": "Hello",
        "greetings": "Hello",
    }
    return greetings.get(normalized)


def _contextual_decision(message, pending_followup):
    if pending_followup not in {
        ENROLLMENT_OFFER_FOLLOWUP,
        ENROLLMENT_CONFIRMATION_FOLLOWUP,
    }:
        return None
    normalized = re.sub(r"[^a-z\s]", " ", (message or "").casefold())
    normalized = " ".join(normalized.split())
    affirmatives = {
        "yes",
        "yes please",
        "sure",
        "sure please",
        "okay",
        "ok",
        "i would like to",
        "i d like to",
        "sign me up",
        "can enrol",
        "can enroll",
        "yup",
        "yeah",
        "please do",
        "go ahead",
    }
    negatives = {
        "no",
        "no thanks",
        "no thank you",
        "not now",
        "maybe later",
        "not yet",
    }
    if normalized in affirmatives:
        return "confirm"
    if normalized in negatives:
        return "reject"
    return None


def _offers_enrollment(reply):
    normalized = " ".join((reply or "").casefold().split())
    return bool(
        "would you like to enrol" in normalized
        or "would you like to enroll" in normalized
        or "begin an enrolment" in normalized
        or "begin an enrollment" in normalized
    )


def _is_empty_course_offer_draft(draft):
    return all(
        getattr(draft, field) is None
        for field in (
            "course",
            "course_fee",
            "full_name",
            "nric",
            "date_of_birth",
            "email",
            "preferred_intake_date",
            "mobile_number",
            "payment_method",
            "skillsfuture_amount",
            "paynow_amount",
        )
    )
