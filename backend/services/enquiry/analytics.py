"""Observed enquiry metrics. Follow-up attribution is temporal, not causal."""
from collections import Counter, defaultdict
from statistics import median
from decimal import Decimal
import re

from backend.models import Message, EnrollmentDraft
from backend.models.operations import Lead, StaffEscalation, FollowupRun
from backend.models.communications import OutboundMessage
from backend.models.finance import Invoice, Payment
from backend.services.operations_service import aware


def reengaged_step(lead, messages):
    inbound = [aware(m.created_at) for m in messages if m.role == "user"]
    runs = FollowupRun.query.filter_by(lead_id=lead.id).all()
    deliveries = [(run, OutboundMessage.query.filter_by(id=run.delivery_id).first()) for run in runs if run.delivery_id]
    matched = set()
    for moment in inbound:
        preceding = [(run, row) for run, row in deliveries if row and row.sent_at and aware(row.sent_at) < moment]
        if preceding:
            run, _ = max(preceding, key=lambda pair: aware(pair[1].sent_at))
            matched.add(run.step + 1)
    return matched


def priority(lead, draft, messages):
    text = " ".join(m.content for m in messages if m.role == "user")
    reasons = []
    for label, pattern in [("Asked about dates", r"\b(date|dates|intake|schedule)\b"),
                           ("Asked about fees or payment", r"\b(fee|fees|cost|payment|paynow)\b")]:
        if re.search(pattern, text, re.I):
            reasons.append(label)
    if draft and draft.full_name:
        reasons.append("Provided name")
    if draft:
        reasons.append("Opened enrollment")
    if reengaged_step(lead, messages):
        reasons.append("Replied after follow-up")
    score = len(reasons)
    return {"priority_score": score, "temperature": "Hot" if score >= 3 else "Warm" if score else "Cold", "priority_reasons": reasons}


def analytics():
    grouped = defaultdict(list)
    for message in Message.query.order_by(Message.created_at).all():
        grouped[message.conversation_id].append(message)
    eligible = {cid: rows for cid, rows in grouped.items() if any(m.role == "user" for m in rows)}
    escalations = StaffEscalation.query.all()
    escalated = {row.conversation_id for row in escalations}
    contained = sum(cid not in escalated and any(m.response_type == "grounded_answer" for m in rows)
                    and not any(m.agent_name == "Staff" for m in rows) for cid, rows in eligible.items())
    response_times = []
    topics = Counter()
    for rows in eligible.values():
        first = next(m for m in rows if m.role == "user")
        reply = next((m for m in rows if m.role == "assistant" and aware(m.created_at) >= aware(first.created_at)), None)
        if reply:
            response_times.append((aware(reply.created_at) - aware(first.created_at)).total_seconds())
        for m in rows:
            topics.update(set(m.kb_entry_ids or []))
    funnel = defaultdict(lambda: {"enquiry": 0, "enrolled": 0, "paid": 0})
    reengaged = Counter()
    for lead in Lead.query.all():
        bucket = funnel[(lead.source or "web", lead.ad_id or "")]
        bucket["enquiry"] += 1
        draft = EnrollmentDraft.query.filter_by(conversation_id=lead.conversation_id).first()
        if draft and draft.confirmed_at and not draft.cancelled_at:
            bucket["enrolled"] += 1
            invoice = Invoice.query.filter_by(enrollment_id=draft.id).first()
            paid = sum((payment.amount for payment in Payment.query.filter_by(enrollment_id=draft.id).all()), Decimal(0))
            if invoice and invoice.course_fee > 0 and paid >= invoice.course_fee:
                bucket["paid"] += 1
        reengaged.update(reengaged_step(lead, grouped[lead.conversation_id]))
    categories = Counter(row.category for row in escalations)
    subjects = Counter((row.subject or row.reason).strip().casefold() for row in escalations if row.category == "unanswered_question")
    from flask import current_app
    kb = current_app.extensions["knowledge_base"]
    topic_names = Counter()
    for entry_id, count in topics.items():
        entry = kb.entry(entry_id)
        topic_names[entry.topic if entry else "Historical topic"] += count
    total = len(eligible)
    category_rates = {
        category: len({row.conversation_id for row in escalations
                       if row.category == category and row.conversation_id in eligible}) / total if total else 0
        for category in categories
    }
    return {"conversations": total, "contained_conversations": contained,
            "containment_rate": contained / total if total else 0,
            "escalation_rate": len(escalated & eligible.keys()) / total if total else 0,
            "escalations_by_category": dict(categories), "top_kb_topics": dict(topic_names.most_common(10)),
            "escalation_rates_by_category": category_rates,
            "top_unanswered_subjects": dict(subjects.most_common(10)),
            "followup_reengagement": dict(reengaged),
            "funnel": [{"source": source, "ad_id": ad_id or None, **counts} for (source, ad_id), counts in funnel.items()],
            "median_first_response_seconds": median(response_times) if response_times else None,
            "definitions": "All recorded history. Containment requires a grounded answer and no escalation or staff reply. Follow-up counts are unique leads per latest preceding sent step; temporal association only. Historical topic logs may be incomplete."}
