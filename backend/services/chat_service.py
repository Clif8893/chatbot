"""Conversation orchestration with application-controlled agent routing."""

import re
from difflib import get_close_matches
from datetime import timezone

from backend.agents.schemas import IntentClassification
from backend.extensions import db
from backend.models import Message
from backend.services.reply_parsing import choice_position, referenced_position, reply_decision
from backend.services.enrollment.enrollment_service import (
    EnrollmentService,
    POST_CONFIRMATION_ADD_COURSE_FOLLOWUP,
)
from backend.services.enrollment.enrollment_management_service import (
    is_invoice_resend_request,
    is_participant_fee_query,
    is_participant_training_query,
    is_status_query,
)
from backend.services.enrollment.enrollment_parser import parse_enrollment_fields
from backend.services.enrollment.registration_group_service import (
    display_range as display_course_range,
    ordered_courses,
)
from backend.services.enquiry import message_policy
from backend.services.enquiry.enquiry_service import EnquiryService
from backend.services.enquiry.knowledge_base import KnowledgeBase
from backend.services.enquiry.memory_service import facts_from_text, stated_credit
from backend.services.privacy import mask_nrics_in_text, redact_for_classification
from backend.services.invoice_email_service import InvoiceEmailError, ResendCooldownError
from backend.models.operations import AuditEvent


ENROLLMENT_OFFER_FOLLOWUP = "enrollment_offer"
ENROLLMENT_CONFIRMATION_FOLLOWUP = "enrollment_confirmation"
INTAKE_INTEREST_FOLLOWUP = "intake_interest"
INTEREST_CONTEXT_KEY = "enrollment_interest_context"
COURSE_CHOICE_PROMPT_START = "Which course would you like"
# Web button ids the API still accepts; routing uses only the message text.
QUICK_ACTIONS = frozenset({"about", "fee", "dates", "enrol"})
# Messages Module A handles in any state, including mid-enrollment.
GLOBAL_ENQUIRY_SIGNALS = {
    message_policy.CRISIS,
    message_policy.THREAT,
    message_policy.STOP,
    message_policy.FORGET,
    message_policy.HUMAN,
}
# Draft states in which an enquiry reply reminds the participant to continue.
DRAFT_DONE_STATUSES = {"confirmed", "awaiting_course_date", "cancelled", "discarded"}


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
        operations_service=None,
        knowledge_base=None,
        memory_service=None,
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
        self.master_invoice_service = master_invoice_service
        self.invoice_template_service = invoice_template_service
        self.invoice_email_service = invoice_email_service
        self.enrollment_management = enrollment_management_service
        self.enrollment_resolver = enrollment_resolver
        self.operations = operations_service
        self.knowledge_base = knowledge_base or KnowledgeBase(catalogue)
        self.enquiry = EnquiryService(
            ai_service,
            self.knowledge_base,
            memory_service=memory_service,
            operations_service=operations_service,
        )

    def respond(self, conversation, user_text, quick_action=None):
        # quick_action is a legacy web-button id; the message text carries the
        # meaning and Module A resolves which course is meant from context.
        user_message = self.repository.add_message(
            conversation, "user", user_text, agent_name="intent_classifier"
        )
        # Facts such as a stated SkillsFuture balance are remembered whichever
        # module answers this message.
        self.enquiry.memory.apply(conversation, facts_from_text(user_text))
        db.session.commit()
        recent_messages = self.repository.recent_messages(
            conversation, self.context_limit + 1
        )
        prior_messages = [
            message for message in recent_messages if message.id != user_message.id
        ][-self.context_limit :]
        # Course memory uses this chat's whole history (never other chats), not
        # only the recent window given to the language model.
        course_history = [
            message
            for message in self.repository.all_messages(conversation)
            if message.id != user_message.id
        ]
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
        if message_policy.precheck(user_text).kind in GLOBAL_ENQUIRY_SIGNALS:
            conversation.funding_state = None
            # Safety, opt-out, forget-me and staff requests apply in any state.
            return self._enquiry_reply(
                conversation,
                user_message,
                draft,
                user_text,
                prior_messages,
                course_history,
                intent="faq",
                confidence=1.0,
                handler="deterministic",
            )
        from backend.services.enquiry.funding import FUNDING_ENTRIES, chat_estimate, funding_result_course
        funding_course, _ = self._course_focus(user_text, course_history, draft)
        funding_reply = chat_estimate(
            conversation, user_text, self.knowledge_base, course=funding_course,
            prior_messages=prior_messages,
        )
        if funding_reply:
            if self.operations:
                self.operations.record_inbound(conversation.id)
            result = self._direct_reply(conversation, user_message, draft, funding_reply,
                                        intent="faq", agent_name="enquiry_agent", routing_agent="enquiry", rule="funding_estimate")
            completed_course = funding_result_course(funding_reply, self.catalogue)
            if completed_course:
                recorded = db.session.get(Message, result["message"]["id"])
                recorded.kb_entry_ids = list(FUNDING_ENTRIES[completed_course["id"]])
                recorded.response_type = "grounded_answer"
                result["routing"]["response_type"] = "grounded_answer"
                db.session.commit()
            else:
                result["routing"]["response_type"] = "clarifying_question"
            return result
        # Only an already-open transaction or an exact read-only operation owns the
        # message before semantic routing. New broad requests are classified first.
        management_first = bool(
            conversation.pending_action is not None
            or is_status_query(user_text)
            or is_participant_training_query(user_text)
            or is_participant_fee_query(user_text)
            or is_invoice_resend_request(user_text)
            or re.fullmatch(
                r"\s*confirm\s+(?:cancellation|change|resend|reschedule)\s*[.!]?\s*",
                str(user_text or ""),
                re.IGNORECASE,
            )
        )
        if self.enrollment_management is not None and management_first:
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
                    rule=management.action,
                )

        retired_reply = self.enrollment.redirect_retired_draft(conversation, draft)
        if retired_reply is not None:
            conversation.active_intent = "enrollment"
            return self._direct_reply(
                conversation,
                user_message,
                draft,
                retired_reply,
                intent="enrollment",
                agent_name="enrollment_agent",
                rule="retired_course_redirect",
            )

        pending_correction = self.enrollment.pending_correction_field(conversation)
        if pending_correction is not None and _is_conversational_clarification(user_text):
            correction_label = self.catalogue.enrollment_field_label(
                pending_correction
            ).casefold()
            return self._direct_reply(
                conversation,
                user_message,
                draft,
                (
                    f"You asked to change your {correction_label}. "
                    + self.enrollment.correction_prompt(draft, pending_correction)
                ),
                intent="workflow_question",
                agent_name="enrollment_agent",
                rule="pending_correction_clarification",
            )
        if pending_correction is not None and _keeps_current_detail(user_text):
            reply = self.enrollment.keep_current_correction_value(
                conversation, draft
            )
            return self._direct_reply(
                conversation,
                user_message,
                draft,
                reply,
                intent="enrollment",
                agent_name="enrollment_agent",
                rule="pending_correction_kept",
            )

        draft_question = self.enrollment.draft_question_reply(
            conversation, draft, user_text
        )
        if draft_question is not None:
            question_kind, reply = draft_question
            rules = {
                "detail": "saved_enrollment_detail_lookup",
                "details": "saved_enrollment_details_lookup",
                "guidance": "enrollment_field_guidance",
            }
            return self._direct_reply(
                conversation,
                user_message,
                draft,
                reply,
                intent="workflow_question",
                agent_name="enrollment_agent",
                rule=rules[question_kind],
            )

        # Exact answers to displayed choices remain deterministic and are resolved
        # only against the option ids stored for this conversation. Questions,
        # hesitation and unrelated messages return None and continue to CrewAI.
        pending_course = self.enrollment.handle_pending_course_selection(
            conversation, user_text
        )
        if pending_course is not None:
            reply, draft = pending_course
            conversation.active_intent = "enrollment"
            self.enrollment.remember_intake_choices(conversation, draft)
            return self._direct_reply(
                conversation,
                user_message,
                draft,
                reply,
                intent="enrollment",
                agent_name="enrollment_agent",
                rule="displayed_course_selection",
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
                rule="displayed_intake_selection",
            )

        # Invoice reads/resend requests are explicit deterministic operations. They
        # never need a language model to select an enrollment or document record.
        invoice_email_request = _invoice_email_request_kind(user_text)
        if invoice_email_request:
            if self.enrollment_resolver is not None:
                invoice_context = self.enrollment_resolver.resolve(conversation, user_text)
                if invoice_context.ambiguous:
                    return self._direct_reply(
                        conversation, user_message, None,
                        self.enrollment_resolver.clarification_reply(invoice_context),
                        intent="enrollment", agent_name="enrollment_agent",
                        rule="invoice_enrollment_clarification",
                    )
                draft = invoice_context.enrollment
            conversation.active_intent = "enrollment"
            reply = self._invoice_email_reply(draft, invoice_email_request)
            result = self._direct_reply(
                conversation, user_message, draft, reply,
                intent="enrollment", agent_name="enrollment_agent",
                rule=f"invoice_email_{invoice_email_request}",
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
                        rule="invoice_enrollment_clarification",
                    )
                draft = invoice_context.enrollment
            conversation.active_intent = "enrollment"
            result = self._direct_reply(
                conversation, user_message, draft, self._invoice_status_reply(draft),
                intent="enrollment", agent_name="enrollment_agent",
                rule="invoice_download_status",
            )
            result["show_invoice_action"] = True
            return result

        enrollment_message = user_text
        pending_followup_before_decision = conversation.pending_followup
        staged_interest_before_decision = self._staged_interest(conversation)
        implicit_interest_option_ids = list(
            (staged_interest_before_decision or {}).get("intake_option_ids") or []
        )
        supplied_offer_fields = parse_enrollment_fields(user_text)
        implicit_offer_acceptance = bool(
            staged_interest_before_decision
            and pending_followup_before_decision
            in {ENROLLMENT_OFFER_FOLLOWUP, INTAKE_INTEREST_FOLLOWUP}
            and any(
                field in supplied_offer_fields
                for field in ("full_name", "mobile_number", "email", "date_of_birth", "nric")
            )
        )
        if implicit_offer_acceptance:
            draft = self.enrollment.begin_from_confirmed_interest(
                conversation,
                staged_interest_before_decision["course_id"],
                staged_interest_before_decision.get("intake_id"),
            )
            self._mark_interested(conversation)
            self._clear_staged_interest(conversation)
        contextual_decision = _contextual_decision(
            user_text, pending_followup_before_decision
        )
        course_choices, duplicate_course_choice = self.catalogue.match_course_selection(
            user_text
        )
        classification_handler = "crewai"
        classification_rule = "semantic_intent_classification"
        if implicit_offer_acceptance:
            classification = IntentClassification(
                intent="enrollment",
                confidence=1.0,
                short_reason=(
                    "Participant supplied enrollment details in response to the "
                    "enrollment invitation."
                ),
            )
            classification_handler = "deterministic"
            classification_rule = "implicit_enrollment_acceptance"
        elif contextual_decision:
            if (
                pending_followup_before_decision == ENROLLMENT_OFFER_FOLLOWUP
                and contextual_decision == "confirm"
            ):
                staged_interest = self._staged_interest(conversation)
                if staged_interest:
                    draft = self.enrollment.begin_from_confirmed_interest(
                        conversation,
                        staged_interest["course_id"],
                        staged_interest.get("intake_id"),
                    )
                    self._mark_interested(conversation)
                    self._clear_staged_interest(conversation)
            classification = IntentClassification(
                intent="enrollment",
                confidence=1.0,
                short_reason="Exact response to the persisted enrollment state.",
            )
            classification_handler = "deterministic"
            classification_rule = "pending_enrollment_confirmation"
            enrollment_message = (
                "Confirm"
                if pending_followup_before_decision
                == ENROLLMENT_CONFIRMATION_FOLLOWUP
                and contextual_decision == "confirm"
                else "not correct"
                if pending_followup_before_decision
                == ENROLLMENT_CONFIRMATION_FOLLOWUP
                else "Yes, enrol me in this course"
            )
            conversation.pending_followup = None
        elif (course_choices or duplicate_course_choice) and (
            draft is not None
            or "available courses" in str(last_assistant_message or "").casefold()
            or (
                any(
                    term in user_text.casefold()
                    for term in ("enrol", "enroll", "register", "sign up")
                )
                # "What do I need to register for X?" is a question, not a selection.
                and not _is_question(user_text)
            )
        ):
            classification = IntentClassification(
                intent="enrollment",
                confidence=1.0,
                short_reason="Exact canonical course selection.",
            )
            classification_handler = "deterministic"
            classification_rule = "canonical_course_selection"
        elif self.enrollment.is_pending_correction_answer(conversation, user_text):
            classification = IntentClassification(
                intent="enrollment",
                confidence=1.0,
                short_reason="Value supplied for the pending enrollment correction.",
            )
            classification_handler = "deterministic"
            classification_rule = "pending_correction_value"
        else:
            classification = self.ai_service.classify(
                message=redact_for_classification(user_text),
                context=self._format_context(prior_messages, redact=True),
                active_intent=conversation.active_intent,
                draft_status=draft.status if draft else None,
                pending_followup=conversation.pending_followup,
                draft_summary=self._classification_draft_summary(draft),
            )
        if (
            pending_followup_before_decision == INTAKE_INTEREST_FOLLOWUP
            and classification.intent != "enrollment_interest"
            and _is_displayed_intake_interest_reply(user_text)
        ):
            staged_interest = self._staged_interest(conversation) or {}
            staged_course = self.catalogue.course_for(
                staged_interest.get("course_id")
            )
            selected_intake = None
            if staged_course is not None:
                selected_intake, _ = self.catalogue.match_intake_selection(
                    user_text, staged_course["id"]
                )
            options = list(conversation.pending_intake_options or [])
            position = referenced_position(user_text)
            if position is not None and 1 <= position <= len(options):
                # "The 2nd one works for me" points at the dates as displayed.
                selected_intake = self.catalogue.intake_for_id(options[position - 1])
            if (
                selected_intake is not None
                and selected_intake["id"]
                in set(conversation.pending_intake_options or [])
            ):
                classification = IntentClassification(
                    intent="enrollment_interest",
                    confidence=1.0,
                    short_reason="Exact selection from the displayed intake dates.",
                )
                classification_handler = "deterministic"
                classification_rule = "displayed_intake_interest"
        if (
            classification.intent == "enrollment_interest"
            and classification_handler == "crewai"
            and (
                _is_question(user_text)
                or _is_greeting(user_text)
                or stated_credit(user_text)
            )
        ):
            # Questions, greetings and shared circumstances are answered first;
            # Module A hands off on a clear wish to enrol.
            classification.intent = "faq"
            classification_rule = "question_before_interest"
        if (
            classification.intent == "pause_workflow"
            and classification_handler == "crewai"
            and not _PAUSE_CUE.search(user_text)
        ):
            # Frustration ("so many questions") is not a request to pause.
            classification.intent = "faq"
            classification_rule = "no_pause_cue"
        user_message.detected_intent = classification.intent
        user_message.classification_confidence = classification.confidence

        if classification.intent == "enrollment_interest":
            return self._interest_reply(
                conversation,
                user_message,
                draft,
                user_text,
                course_history,
                classification,
                classification_handler,
                classification_rule,
            )

        context_course = self._conversation_course(course_history)
        enrol_course = (
            self.catalogue.other_course(self._current_course(course_history))
            if _refers_to_other_course(user_text)
            else context_course
        )
        if (
            enrol_course is not None
            and (draft is None or not draft.course)
            and _is_enrolment_start(user_text)
            and not self.catalogue.courses_mentioned(user_text)
            and not _addresses_all_courses(user_text)
        ):
            # "I'd like to enrol" while discussing one course enrols in that course;
            # "... in the other course" enrols in the other one.
            user_text = f"{user_text.rstrip(' .!')} in the {enrol_course['name']}"
            enrollment_message = user_text

        if (
            classification.intent == "enrollment"
            and draft is not None
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
                rule="enrollment_progress_lookup",
            )

        if (
            contextual_decision == "reject"
            and pending_followup_before_decision == ENROLLMENT_OFFER_FOLLOWUP
        ):
            self._clear_staged_interest(conversation)
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
            return self._direct_reply(
                conversation, user_message, draft, reply,
                intent="faq", agent_name="intent_classifier",
                rule="decline_enrollment_offer",
            )

        pending_correction = self.enrollment.pending_correction_field(conversation)
        has_open_draft = bool(draft and draft.status not in {"discarded", "cancelled"})
        if classification.intent == "workflow_question" and (
            pending_correction is not None or has_open_draft
        ):
            if pending_correction is not None:
                correction_label = self.catalogue.enrollment_field_label(
                    pending_correction
                ).casefold()
                reply = (
                    "The enrollment details are complete. The only pending action is "
                    f"your request to change the {correction_label}. "
                    + self.enrollment.correction_prompt(draft, pending_correction)
                )
            else:
                reply = (
                    "Of course — here is your saved progress.\n\n"
                    f"{self.enrollment.progress_reply(draft)}"
                )
            return self._direct_reply(
                conversation, user_message, draft, clean_customer_copy(reply),
                intent="workflow_question", agent_name="intent_classifier",
                handler="crewai", routing_agent="classifier",
                rule="workflow_question_response",
                confidence=classification.confidence,
            )
        if classification.intent == "pause_workflow" and (
            draft and draft.status not in {"discarded", "cancelled", "confirmed"}
        ):
            # With no enrollment to pause, hesitation is an ordinary enquiry turn.
            reply = (
                "Of course — we can pause here. Your current enrollment progress "
                "will stay available in this conversation, and you can ask another "
                "course question or say ‘Continue my enrollment’ when you’re ready."
            )
            return self._direct_reply(
                conversation, user_message, draft, reply,
                intent="pause_workflow", agent_name="intent_classifier",
                handler="crewai", routing_agent="classifier",
                rule="pause_preserve_pending_state",
                confidence=classification.confidence,
            )
        if classification.intent == "resume_workflow":
            if draft and draft.status not in {"discarded", "cancelled", "confirmed"}:
                conversation.active_intent = "enrollment"
                pending_correction = self.enrollment.pending_correction_field(
                    conversation
                )
                if pending_correction is not None:
                    correction_label = self.catalogue.enrollment_field_label(
                        pending_correction
                    ).casefold()
                    reply = (
                        "Welcome back. The only pending action is your request to "
                        f"change the {correction_label}. "
                        + self.enrollment.correction_prompt(draft, pending_correction)
                    )
                else:
                    self.enrollment.remember_intake_choices(conversation, draft)
                    reply = self.enrollment.continuation_reply(draft)
            else:
                reply = (
                    "There isn’t a saved enrollment to continue in this conversation. "
                    "Tell me which course you’d like to enrol in and we can start."
                )
            return self._direct_reply(
                conversation, user_message, draft, reply,
                intent="resume_workflow", agent_name="intent_classifier",
                handler="crewai", routing_agent="classifier",
                rule="resume_saved_enrollment",
                confidence=classification.confidence,
            )
        if classification.intent != "enrollment":
            return self._enquiry_reply(
                conversation,
                user_message,
                draft,
                user_text,
                prior_messages,
                course_history,
                intent=classification.intent,
                confidence=classification.confidence,
                handler=classification_handler,
            )

        conversation.active_intent = "enrollment"
        if self.enrollment_management is not None and not management_first:
            management = self.enrollment_management.handle(conversation, user_text)
            if management is not None:
                return self._direct_reply(
                    conversation,
                    user_message,
                    management.draft,
                    management.reply,
                    intent="enrollment",
                    agent_name="enrollment_agent",
                    rule=management.action,
                )
        was_confirmed = bool(draft and draft.confirmed_at)
        reply, draft = self.enrollment.handle(
            conversation,
            enrollment_message,
            self._format_context(prior_messages, redact=True),
            last_assistant_message=last_assistant_message,
            deterministic_confirmation=(
                contextual_decision
                if pending_followup_before_decision
                == ENROLLMENT_CONFIRMATION_FOLLOWUP
                else None
            ),
        )
        waiting_for_intake = self.enrollment.remember_intake_choices(
            conversation, draft
        )
        if (
            implicit_offer_acceptance
            and implicit_interest_option_ids
            and draft is not None
            and draft.preferred_intake_date is None
            and not draft.validation_errors
        ):
            conversation.pending_intake_options = implicit_interest_option_ids
            conversation.pending_followup = "intake_selection"
            reply = self.enrollment.intake_prompt_for_ids(
                implicit_interest_option_ids,
                draft.course_id or draft.course,
            )
            waiting_for_intake = True
        if waiting_for_intake:
            pass
        elif self.enrollment.pending_correction_field(conversation):
            pass
        elif draft is None or draft.course is None:
            self.enrollment.remember_course_choices(conversation)
        elif draft.status == "awaiting_confirmation":
            conversation.pending_followup = ENROLLMENT_CONFIRMATION_FOLLOWUP
        else:
            conversation.pending_followup = None
        agent_name = "enrollment_agent"
        confirmed_now = bool(draft and draft.confirmed_at) and not was_confirmed
        if confirmed_now and self.enrollment.can_offer_another_course(draft):
            conversation.pending_followup = POST_CONFIRMATION_ADD_COURSE_FOLLOWUP
            db.session.add(
                AuditEvent(
                    conversation_id=draft.conversation_id,
                    actor="system",
                    action="post_confirmation_add_course_displayed",
                    detail="Participant was offered the remaining eligible course",
                )
            )
        if draft and draft.confirmed_at:
            conversation.pending_intake_options = None

        assistant_message = self.repository.add_message(
            conversation,
            "assistant",
            reply,
            agent_name=agent_name,
            detected_intent=classification.intent,
            classification_confidence=classification.confidence,
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
                "handler": classification_handler,
                "agent": (
                    _routing_agent_name(agent_name)
                    if classification_handler == "crewai"
                    else "none"
                ),
                "rule": (
                    classification_rule
                    if classification_handler == "deterministic"
                    else "faq_grounded_answer"
                    if classification.intent == "faq"
                    else "enrollment_extraction_and_deterministic_validation"
                    if classification.intent == "enrollment"
                    else classification_rule
                ),
            },
            "enrollment": serialize_enrollment(draft, self.catalogue, conversation),
            "quick_replies": [],
        }
        if confirmed_now:
            result["show_invoice_action"] = True
            if self.invoice_email_service is not None:
                result["invoice_email"] = self.invoice_email_service.serialize(draft)
        return result

    def _enquiry_reply(
        self,
        conversation,
        user_message,
        draft,
        user_text,
        prior_messages,
        course_history,
        *,
        intent,
        confidence,
        handler,
    ):
        """Answer through Module A, keeping Module B's offer and draft state intact."""
        continuation = None
        funding_state = conversation.funding_state or {}
        funding_course = self.catalogue.course_for(funding_state.get("course_id"))
        if funding_state:
            from backend.services.enquiry.funding import pending_funding_prompt
            continuation = "To continue the funding calculation: " + pending_funding_prompt(conversation, self.knowledge_base)
        elif draft and draft.status not in DRAFT_DONE_STATUSES:
            continuation = (
                "When you’re ready, we can continue where you left off. "
                f"{self.enrollment.next_step_prompt(draft)}"
            )
        enquiry = self.enquiry.handle(
            conversation,
            user_text,
            prior_messages,
            participant_name=(
                draft.full_name.split()[0] if draft and draft.full_name else None
            ),
            enrolling_course=(self._single_draft_course(draft) or {}).get("name"),
            continuation=continuation,
            funding_course=(funding_course or {}).get("name"),
            # In a new chat "dates plz" names no course; memory must not choose one.
            course_unspecified=not self._course_focus(user_text, course_history, draft)[1],
        )
        if enquiry.handoff_enrollment:
            conversation.funding_state = None
            return self._interest_reply(
                conversation,
                user_message,
                draft,
                user_text,
                course_history,
                IntentClassification(
                    intent="enrollment_interest",
                    confidence=confidence,
                    short_reason="The enquiry assistant detected a wish to enroll.",
                ),
                "crewai",
                "enquiry_handoff",
            )

        reply = enquiry.text
        if conversation.active_intent != "enrollment":
            conversation.active_intent = "faq"
        listed_course = self.knowledge_base.intake_listing_course(enquiry.kb_entry_ids)
        if draft is None and listed_course is not None:
            # A bare date reply to these dates is understood as interest (Module B).
            self._remember_intake_interest_context(
                conversation, self.catalogue.course_for(listed_course)
            )
        if draft and draft.course:
            if draft.status == "awaiting_confirmation":
                conversation.pending_followup = ENROLLMENT_CONFIRMATION_FOLLOWUP
        elif self.enrollment.is_waiting_for_course(conversation):
            pass
        elif self._staged_interest(conversation):
            conversation.pending_followup = (
                ENROLLMENT_OFFER_FOLLOWUP
                if self._staged_interest(conversation).get("intake_id")
                else INTAKE_INTEREST_FOLLOWUP
            )
        else:
            conversation.pending_followup = (
                ENROLLMENT_OFFER_FOLLOWUP if _offers_enrollment(reply) else None
            )
        result = self._direct_reply(
            conversation,
            user_message,
            draft,
            reply,
            intent=intent,
            agent_name="enquiry_agent",
            handler=handler,
            routing_agent="enquiry",
            rule=f"enquiry_{enquiry.response_type}",
            confidence=confidence,
        )
        result["quick_replies"] = enquiry.quick_replies
        recorded = db.session.get(Message, result["message"]["id"])
        recorded.kb_entry_ids = enquiry.kb_entry_ids
        recorded.response_type = enquiry.response_type
        db.session.commit()
        result["routing"]["response_type"] = enquiry.response_type
        if enquiry.used_fallback:
            result["routing"]["used_fallback"] = True
        return result

    def _mark_interested(self, conversation):
        """Qualify a lead after a genuine interest signal without collecting PII."""
        if self.operations is None:
            return None
        lead = self.operations.lead_for(conversation.id)
        if lead.status not in {"enrolled", "declined", "escalated"}:
            lead.status = "qualified"
        return lead

    def _interest_reply(
        self,
        conversation,
        user_message,
        draft,
        user_text,
        course_history,
        classification,
        classification_handler,
        classification_rule,
    ):
        """Stage a qualified interest and offer to begin enrollment (Module B entry)."""
        staged_interest = self._staged_interest(conversation) or {}
        focus = self.catalogue.course_for(staged_interest.get("course_id"))
        if focus is None:
            focus = self._course_focus(user_text, course_history, draft)[0]
        intake = None
        if focus is not None:
            intake, _ = self.catalogue.match_intake_selection(
                user_text, focus["id"]
            )
            displayed = set(conversation.pending_intake_options or [])
            if intake is not None and displayed and intake["id"] not in displayed:
                intake = None
        if focus is None:
            self.enrollment.remember_course_choices(conversation)
            return self._direct_reply(
                conversation,
                user_message,
                draft,
                self.catalogue.course_choice_prompt(
                    "I’d be happy to help. Which course are you interested in?"
                ),
                intent="enrollment_interest",
                agent_name="intent_classifier",
                handler="crewai",
                routing_agent="classifier",
                rule="interest_needs_course",
                confidence=classification.confidence,
            )
        month_intakes = (
            _intakes_for_mentioned_month(
                user_text,
                self.catalogue.selectable_intakes_for(focus["id"]),
            )
            if intake is None
            else []
        )
        if len(month_intakes) > 1:
            self._remember_month_interest_context(
                conversation, focus, month_intakes
            )
            self._mark_interested(conversation)
            choices = "\n".join(
                f"{index}. {item['display_date']} — {item['intake_type']}"
                for index, item in enumerate(month_intakes, start=1)
            )
            return self._direct_reply(
                conversation,
                user_message,
                draft,
                (
                    f"Great — I found {len(month_intakes)} matching intakes for "
                    f"the {focus['name']}:\n\n{choices}\n\nWhich one works for you?"
                ),
                intent="enrollment_interest",
                agent_name="intent_classifier",
                handler=classification_handler,
                routing_agent=(
                    "classifier" if classification_handler == "crewai" else None
                ),
                rule="interest_needs_exact_intake",
                confidence=classification.confidence,
            )
        self._remember_staged_interest(conversation, focus, intake)
        self._mark_interested(conversation)
        chosen = (
            f"The {intake['display_date']} intake is currently available for "
            f"the {focus['name']}."
            if intake is not None
            else f"I understand you’re interested in the {focus['name']}."
        )
        reply = (
            f"{chosen}\n\nWould you like me to begin your enrollment"
            + (" for this intake?" if intake is not None else "?")
            + " I’ll ask for your name and mobile number first. Your NRIC/FIN "
            "will only be requested later for official registration and invoice preparation."
        )
        return self._direct_reply(
            conversation,
            user_message,
            draft,
            reply,
            intent="enrollment_interest",
            agent_name="intent_classifier",
            handler=classification_handler,
            routing_agent=(
                "classifier" if classification_handler == "crewai" else None
            ),
            rule=(
                "qualified_interest_confirmation"
                if classification_handler == "crewai"
                else classification_rule
            ),
            confidence=classification.confidence,
        )

    def _direct_reply(
        self,
        conversation,
        user_message,
        draft,
        reply,
        *,
        intent,
        agent_name,
        rule="application_rule",
        handler="deterministic",
        routing_agent=None,
        confidence=1.0,
    ):
        """Persist an answer and expose how it was actually produced."""
        user_message.detected_intent = intent
        user_message.classification_confidence = confidence
        assistant_message = self.repository.add_message(
            conversation,
            "assistant",
            reply,
            agent_name=agent_name,
            detected_intent=intent,
            classification_confidence=confidence,
        )
        db.session.commit()
        return {
            "conversation_id": conversation.id,
            "message": serialize_message(assistant_message),
            "routing": {
                "intent": intent,
                "confidence": confidence,
                "handler": handler,
                "agent": (
                    routing_agent
                    if routing_agent is not None
                    else "none"
                    if handler == "deterministic"
                    else _routing_agent_name(agent_name)
                ),
                "rule": rule,
            },
            "enrollment": serialize_enrollment(draft, self.catalogue, conversation),
            "quick_replies": [],
        }

    def _course_focus(self, user_text, prior_messages, draft=None):
        """Return (course or None, whether the scope is settled).

        A message naming one course is answered for that course only, and
        "your course", "courses", "both" or "all" covers every course. Any other
        question, including a quick-action button such as "Available dates",
        follows the course the conversation is currently about.
        """
        listed = self._listed_courses(prior_messages)
        position = referenced_position(user_text) if listed else None
        if position is not None and 1 <= position <= len(listed):
            # "I'm interested in the 1st" points at the list the latest reply showed.
            return listed[position - 1], True
        mentioned = self.catalogue.courses_mentioned(user_text)
        if mentioned:
            return (mentioned[0] if len(mentioned) == 1 else None), True
        if _refers_to_other_course(user_text):
            # "the other course" is whichever course the conversation is not about.
            return self.catalogue.other_course(self._current_course(prior_messages)), True
        if _addresses_all_courses(user_text):
            return None, True
        current = self._conversation_course(prior_messages)
        if current is not None:
            return current, True
        enrolling = self._single_draft_course(draft)
        if enrolling is not None:
            # Mid-enrolment, "how much is the course?" means the course being enrolled in.
            return enrolling, True
        return None, False

    def _remember_intake_interest_context(self, conversation, course):
        """Remember exactly which approved dates were displayed during enquiry."""
        options = self.catalogue.selectable_intakes_for(course["id"])
        data = dict(conversation.pending_change_data or {})
        data[INTEREST_CONTEXT_KEY] = {
            "course_id": course["id"],
            "intake_id": None,
        }
        conversation.pending_change_data = data
        conversation.pending_intake_options = [item["id"] for item in options]
        conversation.pending_followup = INTAKE_INTEREST_FOLLOWUP

    def _remember_staged_interest(self, conversation, course, intake=None):
        data = dict(conversation.pending_change_data or {})
        data[INTEREST_CONTEXT_KEY] = {
            "course_id": course["id"],
            "intake_id": intake["id"] if intake is not None else None,
        }
        conversation.pending_change_data = data
        conversation.pending_followup = ENROLLMENT_OFFER_FOLLOWUP

    def _remember_month_interest_context(self, conversation, course, intakes):
        data = dict(conversation.pending_change_data or {})
        data[INTEREST_CONTEXT_KEY] = {
            "course_id": course["id"],
            "intake_id": None,
            "intake_option_ids": [item["id"] for item in intakes],
        }
        conversation.pending_change_data = data
        conversation.pending_intake_options = [item["id"] for item in intakes]
        conversation.pending_followup = INTAKE_INTEREST_FOLLOWUP

    @staticmethod
    def _staged_interest(conversation):
        value = dict(conversation.pending_change_data or {}).get(INTEREST_CONTEXT_KEY)
        return value if isinstance(value, dict) and value.get("course_id") else None

    @staticmethod
    def _clear_staged_interest(conversation):
        data = dict(conversation.pending_change_data or {})
        data.pop(INTEREST_CONTEXT_KEY, None)
        conversation.pending_change_data = data or None
        conversation.pending_intake_options = None
        if conversation.pending_followup in {
            INTAKE_INTEREST_FOLLOWUP,
            ENROLLMENT_OFFER_FOLLOWUP,
        }:
            conversation.pending_followup = None

    def _listed_courses(self, prior_messages):
        """Courses in the latest reply's numbered or bulleted list, in shown order.

        The list may come from a menu or from free text written by the
        assistant ("We currently offer 2 courses: 1. ... 2. ..."). Fewer than
        two listed courses is not a choice, so an empty list is returned.
        """
        latest = next(
            (m for m in reversed(prior_messages or []) if m.role == "assistant"), None
        )
        if latest is None:
            return []
        listed = []
        for line in str(latest.content or "").splitlines():
            if not _LIST_ITEM.match(line):
                continue
            mentioned = self.catalogue.courses_mentioned(_LIST_ITEM.sub("", line))
            if len(mentioned) == 1:
                listed.append(mentioned[0])
        return listed if len(listed) >= 2 else []

    def _current_course(self, prior_messages):
        """The course being discussed: from the participant's messages, or else the
        single course the latest reply was about."""
        current = self._conversation_course(prior_messages)
        if current is not None:
            return current
        replies = [m for m in prior_messages or [] if m.role == "assistant"]
        if replies:
            mentioned = self.catalogue.courses_mentioned(replies[-1].content)
            if len(mentioned) == 1:
                return mentioned[0]
        return None

    def _single_draft_course(self, draft):
        if draft is None or draft.status in {"discarded", "cancelled"}:
            return None
        if len(ordered_courses(draft)) > 1:
            return None
        # Older drafts store only the course name.
        return self.catalogue.course_for(draft.course_id or draft.course)

    def _conversation_course(self, prior_messages):
        """The one course the participant's recent messages are about, if any.

        The participant's newest message that settles a scope wins: one named
        course sets it, while naming several courses or "your course",
        "courses", "both" or "all" clears it. Assistant replies are ignored
        because Reply Template wording ("The course fee is $600 nett ...") does
        not always repeat the course name.
        """
        from backend.services.enquiry.funding import funding_result_course

        messages = list(prior_messages or [])
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if message.role != "user":
                continue
            previous = messages[index - 1] if index else None
            if (
                previous is not None
                and previous.role == "assistant"
                and (previous.content.startswith(COURSE_CHOICE_PROMPT_START)
                     or funding_result_course(previous.content, self.catalogue))
            ):
                # "2" or "both" answering "Which course would you like ...?".
                picked = self.catalogue.picked_course(message.content)
                if picked == "both":
                    return None
                if picked is not None:
                    return picked
            mentioned = self.catalogue.courses_mentioned(message.content)
            if not mentioned and _refers_to_other_course(message.content):
                return self.catalogue.other_course(self._current_course(messages[:index]))
            if len(mentioned) == 1:
                return mentioned[0]
            if len(mentioned) > 1 or _addresses_all_courses(message.content):
                return None
        return None

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
            return "Your invoice is ready. Select Download Invoice in Enrollment & invoice."
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
        "sequence": message.sequence,
        "outcome_status": message.outcome_status,
        "error_category": message.error_category,
        "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
    }
    if message.image_url and message.image_alt:
        serialized.update(
            image_url=message.image_url,
            image_alt=message.image_alt,
            image_status=message.image_status,
        )
    return serialized


def serialize_enrollment(draft, catalogue=None, conversation=None):
    if draft is None:
        return {
            "status": None,
            "course_id": None,
            "course_name": None,
            "course_fee": None,
            "missing_fields": [],
            "demo_intakes_enabled": bool(
                catalogue and catalogue.enable_demo_intakes
            ),
            "selected_intake": None,
            "courses": [],
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
    # Keep cancelled sibling courses visible in participant history while the
    # active legacy fields continue to mirror the remaining confirmed course.
    courses = ordered_courses(draft, include_cancelled=True)
    payload = {
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
    serialized_courses = [
        {
            "id": row.id,
            "position": row.position,
            "course_id": row.course_id,
            "course_name": row.course_name,
            "course_fee": str(row.course_fee),
            "status": row.status,
            "selected_intake": (
                {
                    "id": row.intake_id,
                    "start_date": row.intake_start_date.isoformat(),
                    "end_date": (
                        row.intake_end_date or row.intake_start_date
                    ).isoformat(),
                    "display_date": display_course_range(row),
                    "is_demo": bool(row.intake_is_demo),
                }
                if row.intake_start_date
                else None
            ),
        }
        for row in courses
    ]
    if (
        conversation is not None
        and conversation.pending_action in {
            "awaiting_course_addition_intake",
            "awaiting_course_addition_confirmation",
        }
        and catalogue is not None
    ):
        pending = conversation.pending_change_data or {}
        pending_course = catalogue.course_for(pending.get("course_id"))
        if pending_course is not None and pending_course["id"] not in {
            row.course_id for row in courses
        }:
            pending_intake = catalogue.intake_for_id(pending.get("intake_id"))
            serialized_courses.append(
                {
                    "id": f"pending-{pending_course['id']}",
                    "position": len(courses) + 1,
                    "course_id": pending_course["id"],
                    "course_name": pending_course["name"],
                    "course_fee": str(catalogue.course_fee_for(pending_course)),
                    "status": "pending_addition",
                    "selected_intake": (
                        {
                            "id": pending_intake["id"],
                            "start_date": pending_intake["start_date"],
                            "end_date": pending_intake["end_date"],
                            "display_date": pending_intake["display_date"],
                            "is_demo": bool(pending_intake.get("is_demo")),
                        }
                        if pending_intake is not None
                        else None
                    ),
                }
            )
    if len(serialized_courses) > 1:
        payload["courses"] = serialized_courses
    return payload


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


def _routing_agent_name(agent_name):
    """Public routing metadata uses stable domain names, not DB implementation labels."""
    return {
        "intent_classifier": "classifier",
        "faq_agent": "faq",
        "enquiry_agent": "enquiry",
        "enrollment_agent": "enrollment",
    }.get(agent_name, "none")


def _refers_to_other_course(message):
    return bool(
        re.search(
            r"\b(?:the\s+)?(?:other|another)\s+(?:course|one|programme|program|class|cert\w*)\b"
            r"|\bthe other\b",
            str(message or "").casefold(),
        )
    )


def _addresses_all_courses(message):
    """'Your course', 'courses', 'both' or 'all' mean every course.

    "The course" is not included: it follows the course being discussed.
    """
    text = str(message or "").casefold()
    return bool(
        re.search(
            r"\b(?:ur|your|yr|you guys|q&m)\s+(?:course|class|programme|program)\b"
            r"|\b(?:courses|classes|programmes|programs)\b"
            r"|\b(?:both|all|each|every|either)\b",
            text,
        )
    )


def _is_enrolment_start(message):
    """A request to enrol, as opposed to a question about how enrolment works."""
    text = " ".join(str(message or "").casefold().split())
    if "?" in text or re.match(r"^(?:how|what|can|could|do|does|is|are|when|where|which)\b", text):
        return False
    return bool(re.search(r"\b(?:enrol|enroll|register|sign (?:me )?up)\b", text))


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


def _is_skillsfuture_question(message):
    lowered = str(message or "").casefold()
    return _contains_close_word(lowered, "skillsfuture") or bool(
        re.search(r"\bmid[- ]?career\b.*\b(?:credit|tier|fund)", lowered)
    )


def _is_fee_question(message):
    lowered = str(message or "").casefold()
    return bool(
        re.search(
            r"\b(?:fee|fees|cost|costs|price|pricing|expensive|overpriced|pricey|afford\w*)\b|"
            # Singlish "so ex" / "very ex" means expensive.
            r"\b(?:so|very|too|quite|super|damn)\s+ex\b|"
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


# Imperative ways of asking: "state the ...", "list the ...", "give me the ...".
_ASKING = r"tell|state|list|give|show|share|send|provide|explain|know|pls|please"


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


def _contains_close_word(message, expected):
    words = re.findall(r"[a-z]+", message.casefold())
    return bool(get_close_matches(expected, words, n=1, cutoff=0.7))


# A list line in a reply: "1. Basic ...", "2) ...", "• ..." or "- ...".
_LIST_ITEM = re.compile(r"^\s*(?:\d{1,2}\s*[.)]|[•\-*])\s+")

_DELIVERY_MODE = r"\b(?:online|virtual|virtually|zoom|e-learning|elearning|remote|remotely|in person|in-person|physical|face to face|face-to-face)\b"


def _contextual_decision(message, pending_followup):
    if pending_followup not in {
        ENROLLMENT_OFFER_FOLLOWUP,
        ENROLLMENT_CONFIRMATION_FOLLOWUP,
    }:
        return None
    normalized = re.sub(r"[^a-z\s]", " ", (message or "").casefold())
    normalized = " ".join(normalized.split())
    affirmatives = {
        "confirm",
        "confirmed",
        "i confirm",
        "confirm enrollment",
        "confirm enrolment",
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
        "not correct",
        "reject",
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
    decision = reply_decision(message, actions=_ENROLLMENT_DECISION_WORDS)
    return {"yes": "confirm", "no": "reject"}.get(decision)


# Words that may accompany a yes/no to "Confirm this enrollment?",
# e.g. "yes enrol me" or "ok submit".
_ENROLLMENT_DECISION_WORDS = {
    "enrol", "enroll", "enrolment", "enrollment", "register", "registration",
    "sign", "up", "me", "in", "this", "course", "submit", "want", "to", "would",
    "like", "id", "d", "can", "please", "do",
}


def _is_displayed_intake_interest_reply(message):
    """Recognise a selection only after exact approved intake options were shown."""
    text = " ".join(str(message or "").casefold().split())
    if not text or "?" in text:
        return False
    if choice_position(text.strip(" .!")) is not None:
        return True
    has_date = referenced_position(text) is not None or bool(
        re.search(
            r"\b\d{1,2}(?:st|nd|rd|th)?(?:\s+(?:of\s+)?)?"
            r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
            r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
            r"dec(?:ember)?)\b",
            text,
        )
    )
    if not has_date:
        return False
    # A bare displayed date is a selection. Longer replies need a positive cue so
    # an availability question never becomes permission to enroll.
    words = re.findall(r"[a-z0-9]+", text)
    return len(words) <= 3 or bool(
        re.search(
            r"\b(?:works?|suits?|prefer|choose|take|want|book|reserve|good|okay|ok|"
            r"go with|go for|fine|interested|like)\b",
            text,
        )
    )


def _is_conversational_clarification(message):
    text = " ".join(
        re.sub(r"[^a-z0-9\s]", " ", str(message or "").casefold()).split()
    )
    return bool(
        text in {
            "what",
            "huh",
            "sorry what",
            "what do you mean",
            "i don t understand",
            "i dont understand",
            "i do not understand",
            "don t understand",
            "dont understand",
        }
        or re.search(
            r"\b(?:what do you want|what do you need|what should i send|"
            r"what is required|what is it|so what now|what next|which number)\b",
            text,
        )
    )


def _keeps_current_detail(message):
    text = " ".join(
        re.sub(r"[^a-z0-9\s]", " ", str(message or "").casefold()).split()
    )
    exact = {
        "keep current",
        "keep current number",
        "keep my current number",
        "keep current details",
        "keep my current details",
        "keep current email",
        "keep my current email",
        "keep current name",
        "keep my current name",
        "leave it",
        "leave it as it is",
        "don t change it",
        "do not change it",
        "never mind",
        "nevermind",
    }
    if text in exact:
        return True
    return bool(
        re.fullmatch(
            r"(?:(?:wait|actually|sorry|okay|ok|no)\s+)?"
            r"(?:never\s*mind|don t change it|do not change it|"
            r"keep (?:my )?current (?:details|email|name|number)|leave it(?: as it is)?)"
            r"(?:\s+(?:please|thanks|thank you))?",
            text,
        )
    )


def _intakes_for_mentioned_month(message, intakes):
    text = str(message or "").casefold()
    months = {
        "january": "01",
        "february": "02",
        "march": "03",
        "april": "04",
        "may": "05",
        "june": "06",
        "july": "07",
        "august": "08",
        "september": "09",
        "october": "10",
        "november": "11",
        "december": "12",
    }
    mentioned = next(
        (number for name, number in months.items() if re.search(rf"\b{name}\b", text)),
        None,
    )
    if mentioned is None:
        return []
    return [
        item
        for item in intakes
        if str(item.get("start_date", ""))[5:7] == mentioned
    ]


_QUESTION_START = re.compile(
    r"^\s*(?:what|wat|how|hw|when|where|whr|which|who|why|can|could|do|does|is|are|"
    r"will|would|may|should)\b",
    re.I,
)


_GREETING = re.compile(
    r"^\s*(?:hi|hello|hey|hiya|yo|good\s+(?:morning|afternoon|evening)|morning|"
    r"afternoon|evening)\b[\w\s,'’]{0,20}[.!]*\s*$",
    re.I,
)


_PAUSE_CUE = re.compile(
    r"\b(?:pause|later|not\s+now|another\s+time|hold\s+on|for\s+now|take\s+a\s+break|"
    r"think\s+about\s+it|decide|not\s+sure|unsure|come\s+back|busy|not\s+ready|"
    r"ask\s+something\s+else|stop\s+here)\b",
    re.I,
)


def _is_question(message):
    text = str(message or "")
    return "?" in text or bool(_QUESTION_START.match(text))


def _is_greeting(message):
    return bool(_GREETING.match(str(message or "")))


def _offers_enrollment(reply):
    normalized = " ".join((reply or "").casefold().split())
    return bool(
        "would you like to enrol" in normalized
        or "would you like to enroll" in normalized
        or "begin an enrolment" in normalized
        or "begin an enrollment" in normalized
        # Approved introduction wording (Reply Template).
        or "interested in enrolling, let me know" in normalized
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
        )
    )
