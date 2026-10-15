"""Staff administration and consent-aware, durable lead follow-up scheduling."""

from datetime import date, datetime, timedelta, timezone
from copy import deepcopy

from flask import current_app

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from backend.extensions import db
from backend.models import Conversation, EnrollmentDraft, Message, utc_now
from backend.models.operations import (
    AuditEvent, CatalogueContent, ConsentRecord, ContactMapping, CourseDate,
    FollowupRun, Lead, StaffEscalation,
)
from backend.services.lead_nurturing import WELCOME_MENU_FOLLOWUP
from backend.services.privacy import mask_nrics_in_text, mask_nric


def timestamp(value):
    if value is None:
        return None
    return aware(value).isoformat().replace("+00:00", "Z")


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def parse_date(value, field):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a date in YYYY-MM-DD format.")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{field} must be a date in YYYY-MM-DD format.") from None


class OperationsService:
    def __init__(self, catalogue, delivery_service=None, followup_hours=(24, 72, 168), finance_service=None, intake_service=None, master_invoice_service=None):
        hours = tuple(float(value) for value in followup_hours)
        if not hours or any(value <= 0 for value in hours) or tuple(sorted(set(hours))) != hours:
            raise ValueError("Follow-up hours must be positive, unique, and increasing.")
        self.catalogue = catalogue
        self.delivery = delivery_service
        self.followup_hours = hours
        self.finance = finance_service
        self.intakes = intake_service
        self.master_invoice = master_invoice_service

    def _conversation(self, conversation_id):
        conversation = db.session.get(Conversation, conversation_id)
        if conversation is None:
            raise LookupError("Conversation was not found.")
        return conversation

    def lead_for(self, conversation_id):
        self._conversation(conversation_id)
        lead = Lead.query.filter_by(conversation_id=conversation_id).first()
        if lead is None:
            lead = Lead(conversation_id=conversation_id)
            db.session.add(lead)
            db.session.flush()
        return lead

    def audit(self, action, detail, conversation_id=None, actor="system"):
        row = AuditEvent(conversation_id=conversation_id, actor=actor, action=action,
                         detail=mask_nrics_in_text(str(detail)))
        db.session.add(row)
        return row

    def incoming_contact(self, channel, external_id, conversation_id=None, now=None):
        if channel not in {"whatsapp", "email", "web", "development"}:
            raise ValueError("Unsupported contact channel.")
        if not isinstance(external_id, str) or not external_id.strip() or len(external_id) > 320:
            raise ValueError("An external contact identity is required.")
        external_id = external_id.strip()
        if channel == "email":
            external_id = external_id.casefold()
        identity_digest = current_app.extensions["crypto"].lookup_digest(external_id)
        mapping = ContactMapping.query.filter_by(channel=channel, identity_digest=identity_digest).first()
        if mapping:
            if conversation_id and mapping.conversation_id != conversation_id:
                raise ValueError("Contact is already linked to another conversation.")
            conversation = self._conversation(mapping.conversation_id)
        else:
            conversation = self._conversation(conversation_id) if conversation_id else Conversation()
            db.session.add(conversation)
            db.session.flush()
            db.session.add(ContactMapping(channel=channel, external_id=external_id, identity_digest=identity_digest, conversation_id=conversation.id))
            try:
                db.session.flush()
            except IntegrityError:
                db.session.rollback()
                mapping = ContactMapping.query.filter_by(channel=channel, identity_digest=identity_digest).first()
                if mapping is None or (conversation_id and mapping.conversation_id != conversation_id):
                    raise ValueError("Contact identity could not be linked.") from None
                conversation = self._conversation(mapping.conversation_id)
        lead = self.lead_for(conversation.id)
        lead.channel, lead.recipient = channel, external_id
        self.record_inbound(conversation.id, now=now)
        db.session.commit()
        return conversation

    def record_inbound(self, conversation_id, now=None):
        lead = self.lead_for(conversation_id)
        lead.last_inbound_at = now or utc_now()
        if lead.status == "awaiting_response":
            self.stop_followups(conversation_id, "responded")
        db.session.flush()
        return lead

    def consent_for(self, conversation_id):
        return (ConsentRecord.query.filter_by(conversation_id=conversation_id)
                .order_by(ConsentRecord.recorded_at.desc(), ConsentRecord.id.desc()).first())

    def set_consent(self, conversation_id, granted, version="2026-06", source="chat", now=None):
        conversation = self._conversation(conversation_id)
        if type(granted) is not bool:
            raise ValueError("Consent must be true or false.")
        if not isinstance(version, str) or not version or len(version) > 64:
            raise ValueError("A consent policy version is required.")
        recorded = aware(now or utc_now())
        previous = self.consent_for(conversation_id)
        if previous and recorded <= aware(previous.recorded_at):
            recorded = aware(previous.recorded_at) + timedelta(microseconds=1)
        conversation.status = 'active' if granted else 'consent_withdrawn'
        conversation.pending_followup = 'registration_details' if granted and conversation.active_intent == 'enrollment' else None
        row = ConsentRecord(conversation_id=conversation_id, granted=granted, version=version,
                            source=source, recorded_at=recorded)
        db.session.add(row)
        self.audit("consent_granted" if granted else "consent_withdrawn", f"Policy {version}; source {source}", conversation_id)
        if not granted:
            self.stop_followups(conversation_id, "declined")
            from backend.models.communications import OutboundMessage
            db.session.execute(db.update(OutboundMessage).where(OutboundMessage.conversation_id == conversation_id,
                               OutboundMessage.status == 'queued', OutboundMessage.purpose.like('balance_reminder:%')).values(status='cancelled'))
        db.session.flush()
        return row

    def awaiting_response(self, conversation_id, channel=None, recipient=None, now=None):
        lead = self.lead_for(conversation_id)
        if lead.status in {"enrolled", "escalated", "declined"}:
            return lead
        lead.channel = channel or lead.channel
        lead.recipient = recipient or lead.recipient
        lead.status = "awaiting_response"
        lead.last_contact_at = now or utc_now()
        lead.followup_count = 0
        lead.sequence += 1
        db.session.flush()
        return lead

    def stop_followups(self, conversation_id, reason):
        allowed = {"responded", "enrolled", "escalated", "declined", "timed_out"}
        if reason not in allowed:
            raise ValueError("Invalid lead stop reason.")
        lead = self.lead_for(conversation_id)
        lead.status = reason
        from backend.models.communications import OutboundMessage
        pending = db.session.execute(db.select(OutboundMessage).where(OutboundMessage.conversation_id == conversation_id,
                    OutboundMessage.status == 'queued', OutboundMessage.purpose.like('lead_followup_%'))).scalars()
        for outbound in pending:
            outbound.status = 'cancelled'
        self.audit("followups_stopped", reason, conversation_id)
        db.session.flush()
        return lead

    def escalate(self, conversation_id, reason, category="unrecognised", actor="system"):
        self._conversation(conversation_id)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 5000:
            raise ValueError("An escalation reason is required (maximum 5000 characters).")
        # Repeated uncertainty does not produce duplicate open staff cases.
        row = StaffEscalation.query.filter_by(conversation_id=conversation_id, status="open", category=category).first()
        if row is None:
            row = StaffEscalation(conversation_id=conversation_id, reason=mask_nrics_in_text(reason), category=category)
            db.session.add(row)
            self.audit("escalated", reason, conversation_id, actor)
            alert_email = current_app.config.get('STAFF_ALERT_EMAIL')
            if alert_email and self.delivery:
                self.notify("email", alert_email, "Q&M participant case requires review",
                            f"A {category} case requires review. Sign in to the staff portal and open case {conversation_id}.",
                            conversation_id, "staff_escalation_alert")
        self.stop_followups(conversation_id, "escalated")
        db.session.flush()
        return row

    def notify(self, channel, recipient, subject, body, conversation_id=None, purpose="staff_reply"):
        if self.delivery is None:
            raise ValueError("Outbound delivery is not configured.")
        return self.delivery.queue(channel=channel, recipient=recipient, subject=subject, body=body,
                                   conversation_id=conversation_id, purpose=purpose)

    def run_followups(self, now=None):
        now = aware(now or utc_now())
        queued = []
        # Leads who contacted Q&M first are told in the first reply that they may
        # receive reminders and can reply STOP; an explicit withdrawal always wins.
        explicit_consent_required = current_app.config.get("FOLLOWUP_REQUIRE_EXPLICIT_CONSENT", False)
        for lead in Lead.query.filter_by(status="awaiting_response").order_by(Lead.created_at).all():
            consent = self.consent_for(lead.conversation_id)
            if consent is not None and not consent.granted:
                continue
            if consent is None and explicit_consent_required:
                continue
            if not lead.recipient or lead.channel not in {"email", "whatsapp"} or not lead.last_contact_at:
                continue
            draft = EnrollmentDraft.query.filter_by(conversation_id=lead.conversation_id).first()
            if draft and draft.status not in {"collecting", "awaiting_confirmation"}:
                self.stop_followups(lead.conversation_id, "enrolled")
                continue
            if lead.last_inbound_at and aware(lead.last_inbound_at) > aware(lead.last_contact_at):
                self.stop_followups(lead.conversation_id, "responded")
                continue
            if StaffEscalation.query.filter_by(conversation_id=lead.conversation_id, status="open").first():
                self.stop_followups(lead.conversation_id, "escalated")
                continue
            previous = FollowupRun.query.filter_by(lead_id=lead.id, sequence=lead.sequence).all()
            completed = {row.step for row in previous}
            step = next((i for i in range(len(self.followup_hours)) if i not in completed), None)
            if step is None:
                lead.status = "timed_out"
                continue
            if now < aware(lead.last_contact_at) + timedelta(hours=self.followup_hours[step]):
                continue
            row = FollowupRun(lead_id=lead.id, sequence=lead.sequence, step=step, created_at=now)
            hours = self.followup_hours[step]
            body = self.followup_body(step)
            conversation = self._conversation(lead.conversation_id)
            # The unique step constraint also protects concurrent scheduler workers.
            # Reserve the step and persist its outbox row in the same savepoint.
            try:
                with db.session.begin_nested():
                    db.session.add(row)
                    db.session.flush()
                    delivery = self.notify(lead.channel, lead.recipient, "Q&M dental course enquiry follow-up", body,
                                           lead.conversation_id, f"lead_followup_{hours:g}h")
                    row.delivery_id = delivery.id
                    lead.followup_count = len(completed) + 1
                    if lead.followup_count == len(self.followup_hours):
                        lead.status = "timed_out"
                    if conversation.pending_followup is None and conversation.pending_action is None:
                        # Reminders invite numbered replies, so re-open the quick-reply menu.
                        conversation.pending_followup = WELCOME_MENU_FOLLOWUP
                    self.audit("followup_queued", f"Step {step + 1} at {hours:g} hours", lead.conversation_id)
            except IntegrityError:
                continue
            queued.append(row.id)
        db.session.commit()
        return {"queued": len(queued), "followup_ids": queued}

    def followup_body(self, step):
        """Staff-approved reminder wording for a zero-based follow-up step."""
        templates = self.catalogue.lead_nurturing.get("followup_templates") or []
        if templates:
            return templates[min(step, len(templates) - 1)]
        return ("Following up on your enquiry about our dental courses. "
                "You can ask about fees or available dates, or tell us you would like to register. "
                "Reply STOP to stop reminders or ask for a member of our team.")

    def update_lead(self, lead_id, payload, actor="staff"):
        lead = db.session.get(Lead, lead_id)
        if lead is None:
            raise LookupError("Lead was not found.")
        if "consent" in payload:
            self.set_consent(lead.conversation_id, payload["consent"], source="staff")
        if "status" in payload:
            if payload["status"] == "awaiting_response":
                if lead.status in {"declined", "escalated", "enrolled"}:
                    raise ValueError("Resolve the stop condition before restarting follow-ups.")
                self.awaiting_response(lead.conversation_id)
            else:
                self.stop_followups(lead.conversation_id, payload["status"])
        self.audit("lead_updated", str({k: v for k, v in payload.items() if k in {"status", "consent"}}), lead.conversation_id, actor)
        db.session.commit()
        return self.serialize_lead(lead)

    def update_escalation(self, escalation_id, payload, actor="staff"):
        row = db.session.get(StaffEscalation, escalation_id)
        if row is None:
            raise LookupError("Escalation was not found.")
        status = payload.get("status", row.status)
        if status not in {"open", "resolved"}:
            raise ValueError("Escalation status must be open or resolved.")
        note = payload.get("note", row.note)
        if note is not None and (not isinstance(note, str) or len(note) > 5000):
            raise ValueError("Note must be text of at most 5000 characters.")
        row.status, row.note = status, mask_nrics_in_text(note)
        row.resolved_at = utc_now() if status == "resolved" else None
        if status == "open":
            self.stop_followups(row.conversation_id, "escalated")
        elif not StaffEscalation.query.filter(StaffEscalation.conversation_id == row.conversation_id,
                                            StaffEscalation.status == "open", StaffEscalation.id != row.id).first():
            lead = self.lead_for(row.conversation_id)
            if lead.status == "escalated":
                lead.status = "responded"
        self.audit("escalation_updated", f"{status}: {note or ''}", row.conversation_id, actor)
        db.session.commit()
        return self.serialize_escalation(row)

    def _occupants(self, intake_date):
        query = EnrollmentDraft.query.filter_by(preferred_intake_date=intake_date)
        finance_class = db.Model.registry._class_registry.get("FinanceCase")
        if finance_class:
            query = query.outerjoin(finance_class, finance_class.enrollment_id == EnrollmentDraft.id)
            query = query.filter(or_(finance_class.status.in_(["Enrolled", "Invoice Sent", "Awaiting Payment", "Paid", "Receipt Issued", "Credit Note Required"]),
                                     (finance_class.id.is_(None) & EnrollmentDraft.status.in_(["confirmed", "enrolled", "invoice_sent", "awaiting_payment", "paid", "receipt_issued"]))))
        else:
            query = query.filter(EnrollmentDraft.status.in_(["confirmed", "enrolled", "invoice_sent", "awaiting_payment", "paid", "receipt_issued"]))
        return query.all()

    def _paid(self, draft):
        if draft.status.casefold().replace(" ", "_") in {"paid", "receipt_issued"}:
            return True
        finance_class = db.Model.registry._class_registry.get("FinanceCase")
        if finance_class:
            case = finance_class.query.filter_by(enrollment_id=draft.id).first()
            return case is not None and case.status in {"Paid", "Receipt Issued"}
        return False

    def serialize_course_date(self, row):
        occupants = self._occupants(row.date)
        stored_count = int(row.participant_count or 0)
        enrollment_count = max(stored_count, len(occupants))
        effective = self.intakes.to_catalogue_item(row) if self.intakes else None
        return {"id": row.id, "date": row.date.isoformat(), "end_date": row.end_date.isoformat(),
                "course_id": row.course_id, "start_time": row.start_time.strftime("%H:%M"),
                "end_time": row.end_time.strftime("%H:%M"), "is_demo": bool(row.is_demo),
                "label": row.label, "capacity": row.capacity,
                "status": effective["status"] if effective else row.status,
                "configured_status": row.status,
                "enrollment_count": enrollment_count,
                "participant_count": stored_count,
                "paid_count": sum(self._paid(draft) for draft in occupants),
                "available_places": max(0, row.capacity - enrollment_count) if row.status == "open" else 0}

    def save_course_date(self, payload, course_date_id=None, actor="staff"):
        row = db.session.get(CourseDate, course_date_id) if course_date_id else CourseDate()
        if row is None:
            raise LookupError("Course date was not found.")
        start = parse_date(payload.get("date", row.date), "date")
        end = parse_date(payload.get("end_date", row.end_date or start + timedelta(days=1)), "end_date")
        capacity = payload.get("capacity", row.capacity)
        status = payload.get("status", row.status or "open")
        label = payload.get("label", row.label or "")
        if type(capacity) is not int or not 1 <= capacity <= 10000:
            raise ValueError("Capacity must be an integer from 1 to 10000.")
        if end < start:
            raise ValueError("End date cannot precede start date.")
        if status not in {"open", "closed", "cancelled"}:
            raise ValueError("Status must be open, closed or cancelled.")
        if not isinstance(label, str) or len(label) > 255:
            raise ValueError("Label must be text of at most 255 characters.")
        occupants = self._occupants(row.date) if row.date else []
        if (occupants or int(row.participant_count or 0) > 0) and start != row.date:
            raise ValueError("An occupied intake cannot change its date; assign participants individually.")
        if capacity < max(len(occupants), int(row.participant_count or 0)):
            raise ValueError("Capacity cannot be lower than the current enrollment count.")
        if CourseDate.query.filter(CourseDate.date == start, CourseDate.id != (row.id or "")).first():
            raise ValueError("A course intake already exists on this date.")
        row.date, row.end_date, row.capacity, row.status, row.label = start, end, capacity, status, label
        db.session.add(row)
        db.session.merge(CatalogueContent(key="dates_managed", value=True))
        self.audit("course_date_updated" if course_date_id else "course_date_created", f"{start}; capacity {capacity}; {status}", actor=actor)
        db.session.commit()
        return self.serialize_course_date(row)

    def delete_course_date(self, course_date_id, actor="staff"):
        row = db.session.get(CourseDate, course_date_id)
        if row is None:
            raise LookupError("Course date was not found.")
        if self._occupants(row.date) or int(row.participant_count or 0) > 0:
            raise ValueError("An occupied intake cannot be deleted. Close it to stop new enrollments.")
        self.audit("course_date_deleted", row.date.isoformat(), actor=actor)
        db.session.delete(row)
        db.session.merge(CatalogueContent(key="dates_managed", value=True))
        db.session.commit()

    def reserve_confirmation(self, draft):
        """Validate a final enrollment while the caller's transaction is active."""
        if draft.preferred_intake_date is None:
            return  # An unscheduled enrollment awaits staff course assignment.
        intake = db.session.get(CourseDate, draft.intake_id) if draft.intake_id else CourseDate.query.filter_by(date=draft.preferred_intake_date).first()
        if intake is None:
            raise ValueError("The selected course intake is unavailable. Please choose an available date.")
        if self.intakes:
            error = self.intakes.allocation_error(intake.id, draft)
            if error:
                raise ValueError(error)

    def assign_course_date(self, conversation_id, course_date_id, actor="staff"):
        conversation = self._conversation(conversation_id)
        draft = conversation.enrollment_draft
        if draft is None or draft.status not in {'confirmed', 'awaiting_course_date', 'awaiting_confirmation', 'paid', 'receipt_issued'}:
            raise ValueError("Complete and confirm the participant's registration details before assigning a course date.")
        intake = db.session.get(CourseDate, course_date_id, with_for_update=True)
        if intake is None:
            raise LookupError("Course date was not found.")
        previous = (
            draft.course_id,
            draft.course,
            draft.course_fee,
            draft.preferred_intake_date,
            draft.intake_id,
            draft.intake_start_date,
            draft.intake_end_date,
            draft.intake_is_demo,
        )
        selected_course = self.catalogue.course_for(intake.course_id)
        if selected_course is None:
            raise ValueError("The selected intake is not linked to an available course.")
        if draft.course_id and draft.course_id != intake.course_id:
            raise ValueError("The selected intake does not belong to the selected course.")
        draft.course_id = selected_course["id"]
        draft.course = selected_course["name"]
        draft.course_fee = self.catalogue.course_fee_for(selected_course)
        draft.preferred_intake_date = intake.date
        draft.intake_id = intake.id
        draft.intake_start_date = intake.date
        draft.intake_end_date = intake.end_date
        draft.intake_is_demo = bool(intake.is_demo)
        try:
            self.reserve_confirmation(draft)
        except ValueError:
            (
                draft.course_id,
                draft.course,
                draft.course_fee,
                draft.preferred_intake_date,
                draft.intake_id,
                draft.intake_start_date,
                draft.intake_end_date,
                draft.intake_is_demo,
            ) = previous
            raise
        if not self._paid(draft):
            draft.status = 'awaiting_confirmation'
            draft.confirmed_at = None
            self.audit('course_date_proposed', f'Participant confirmation requested for {intake.date}', conversation_id, actor)
            self._conversation(conversation_id).active_intent = 'enrollment'
            from backend.services.enrollment.enrollment_service import EnrollmentService
            summary = EnrollmentService(
                None, self.catalogue, None, intake_service=self.intakes
            )._confirmation_summary(draft)
            db.session.add(Message(conversation_id=conversation_id, role='assistant', content=summary, agent_name='enrollment_agent'))
            self.notify('email', draft.email, 'Please confirm your course date', summary, conversation_id, 'course_date_confirmation_request')
            if draft.mobile_number:
                self.notify('whatsapp', draft.mobile_number, None, summary, conversation_id, 'course_date_confirmation_request')
            db.session.commit()
            if self.master_invoice:
                self.master_invoice.mark_needs_update(draft.id)
            return self.case_detail(conversation_id)
        if self.intakes:
            self.intakes.allocate(draft)
        self.audit("paid_course_assigned", f"Previous {previous[3] or 'unscheduled'}; assigned {intake.date}; original issued invoice retained", conversation_id, actor)
        # Financial documents retain their issued snapshots; the new confirmation
        # is the authoritative schedule notice and the audit preserves history.
        lead = self.lead_for(conversation_id)
        recipient, channel = (draft.email, "email") if draft.email else (lead.recipient, lead.channel)
        if recipient and channel:
            selected_course = self.catalogue.course_for(draft.course_id or intake.course_id or draft.course)
            course_name = selected_course.get("name") if selected_course else draft.course
            training_time = selected_course.get("training_time", "") if selected_course else ""
            venue = selected_course.get("venue", {}).get("customer_reply", "Staff confirmation required") if selected_course else "Staff confirmation required"
            self.notify(channel, recipient, f"Confirmed {course_name} dates",
                        f"Your paid enrollment for {course_name} is scheduled for {intake.date} to {intake.end_date}. "
                        f"Training time: {training_time}. Venue: {venue}.",
                        conversation_id, "course_assignment")
        db.session.commit()
        if self.master_invoice:
            self.master_invoice.update_exported_enrollment(draft.id, actor)
        return self.case_detail(conversation_id)

    def update_content(self, payload, actor="staff"):
        allowed_course = {"overview", "duration", "training_time", "venue", "website", "no_intakes_message",
                          "minimum_qualification", "job_opportunities", "skillsfuture", "utap", "enrollment", "paynow"}
        if set(payload) - {"course", "faqs"}:
            raise ValueError("Content may contain only course and faqs.")
        current = deepcopy(self.catalogue.public_summary())
        if "course" in payload:
            patch = payload["course"]
            if not isinstance(patch, dict) or set(patch) - allowed_course:
                raise ValueError("Course edits support FAQ and guidance content; use course dates to edit intakes.")
            for key, value in patch.items():
                original = current["course"].get(key)
                if isinstance(original, str):
                    if not isinstance(value, str) or not value.strip() or len(value) > 10000:
                        raise ValueError(f"{key} must be nonempty text of at most 10000 characters.")
                    current["course"][key] = value
                elif isinstance(original, dict):
                    permitted = set(original) | ({"instructions"} if key == "paynow" else set())
                    if not isinstance(value, dict) or set(value) - permitted:
                        raise ValueError(f"Unsupported {key} content fields.")
                    for inner_key, inner_value in value.items():
                        template = original.get(inner_key, "")
                        if type(inner_value) is not type(template):
                            raise ValueError(f"{key}.{inner_key} has an invalid value type.")
                    current["course"][key].update(value)
                else:
                    raise ValueError(f"Unsupported content field {key}.")
        if "faqs" in payload:
            faqs = payload["faqs"]
            course_ids = {course["id"] for course in self.catalogue.courses}

            def valid_text(value):
                return isinstance(value, str) and 0 < len(value.strip()) <= 10000

            def valid_faq(item):
                # course_answers optionally overrides the answer for one named course.
                if not isinstance(item, dict) or not {"topic", "answer"} <= set(item) <= {"topic", "answer", "course_answers"}:
                    return False
                course_answers = item.get("course_answers", {})
                return (valid_text(item["topic"]) and valid_text(item["answer"]) and isinstance(course_answers, dict)
                        and set(course_answers) <= course_ids and all(valid_text(v) for v in course_answers.values()))

            if not isinstance(faqs, list) or len(faqs) > 100 or not all(valid_faq(item) for item in faqs):
                raise ValueError("FAQs must be up to 100 objects with nonempty topic and answer text, "
                                 "plus optional per-course answers for current courses.")
            current["faqs"] = deepcopy(faqs)
        # Dates and financial fee values are managed by their dedicated workflows.
        current["course"].pop("intakes", None)
        db.session.merge(CatalogueContent(key="content", value=current))
        self.audit("catalogue_content_updated", "Course FAQ content updated", actor=actor)
        db.session.commit()
        return self.catalogue.public_summary()

    def serialize_lead(self, lead):
        draft = EnrollmentDraft.query.filter_by(conversation_id=lead.conversation_id).first()
        consent = self.consent_for(lead.conversation_id)
        return {"id": lead.id, "conversation_id": lead.conversation_id, "status": lead.status,
                "channel": lead.channel, "recipient": lead.recipient, "full_name": draft.full_name if draft else None,
                "email": draft.email if draft else None, "followup_count": lead.followup_count,
                "last_inbound_at": timestamp(lead.last_inbound_at), "last_contact_at": timestamp(lead.last_contact_at),
                "consent": {"granted": consent.granted if consent else False, "version": consent.version if consent else None,
                            "recorded_at": timestamp(consent.recorded_at) if consent else None}}

    @staticmethod
    def serialize_escalation(row):
        return {"id": row.id, "conversation_id": row.conversation_id, "reason": row.reason, "category": row.category,
                "status": row.status, "note": row.note, "created_at": timestamp(row.created_at), "resolved_at": timestamp(row.resolved_at)}

    def case_detail(self, conversation_id):
        conversation = self._conversation(conversation_id)
        lead = self.lead_for(conversation_id)
        draft = conversation.enrollment_draft
        return {"lead": self.serialize_lead(lead),
                "escalations": [self.serialize_escalation(row) for row in StaffEscalation.query.filter_by(conversation_id=conversation_id).order_by(StaffEscalation.created_at.desc()).all()],
                "messages": [{"id": row.id, "role": row.role, "content": mask_nrics_in_text(row.content), "created_at": timestamp(row.created_at), "agent_name": row.agent_name}
                             for row in Message.query.filter_by(conversation_id=conversation_id).order_by(Message.created_at).all()],
                "audit": [{"id": row.id, "actor": row.actor, "action": row.action, "detail": row.detail, "created_at": timestamp(row.created_at)}
                          for row in AuditEvent.query.filter_by(conversation_id=conversation_id).order_by(AuditEvent.created_at.desc()).all()],
                "followups": [{"step": row.step + 1, "created_at": timestamp(row.created_at), "delivery_id": row.delivery_id}
                              for row in FollowupRun.query.filter_by(lead_id=lead.id).order_by(FollowupRun.created_at).all()],
                "finance": self.finance.serialize_enrollment(draft) if draft and self.finance else None,
                "enrollment_draft": {"id": draft.id, "full_name": draft.full_name, "nric_masked": mask_nric(draft.nric),
                                     "email": draft.email, "course": draft.course, "status": draft.status,
                                     "preferred_intake_date": draft.preferred_intake_date.isoformat() if draft.preferred_intake_date else None,
                                     "cancellation_requested_at": timestamp(draft.cancellation_requested_at),
                                     "cancelled_at": timestamp(draft.cancelled_at),
                                     "cancellation_reason": draft.cancellation_reason,
                                     "reschedule_requested_at": timestamp(draft.reschedule_requested_at),
                                     "rescheduled_at": timestamp(draft.rescheduled_at),
                                     "previous_intake_id": draft.previous_intake_id,
                                     "refund_review_required": bool(draft.refund_review_required)} if draft else None}

    def staff_reply(self, conversation_id, body, subject="Course enquiry", actor="staff"):
        if not isinstance(body, str) or not body.strip() or len(body) > 10000:
            raise ValueError("Reply must be nonempty text of at most 10000 characters.")
        lead = self.lead_for(conversation_id)
        if not lead.channel or not lead.recipient:
            raise ValueError("No delivery contact is recorded for this conversation.")
        row = self.notify(lead.channel, lead.recipient, subject, body, conversation_id)
        db.session.add(Message(conversation_id=conversation_id, role="assistant", content=mask_nrics_in_text(body), agent_name="Staff"))
        self.audit("staff_reply_queued", f"Delivery {row.id}", conversation_id, actor)
        db.session.commit()
        return {"delivery_id": row.id, "status": "queued"}
