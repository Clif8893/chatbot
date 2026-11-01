"""Regression coverage for attribution, transport menus, learning and estimates."""
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
import json

import pytest

from backend.extensions import db
from backend.models import Conversation, Message, EnrollmentDraft, utc_now
from backend.models.operations import KnowledgeDraft, StaffEscalation, FollowupRun
from backend.models.communications import OutboundMessage
from backend.services.enquiry.funding import chat_estimate, estimate, render
from backend.services.course_catalogue import CourseCatalogue
from backend.services.enquiry.knowledge_base import KnowledgeBase
from backend.services.enquiry.whatsapp_menu import interactive_payload, inbound_text
from backend.services.enquiry.analytics import analytics


@pytest.mark.parametrize("credit,ntuc,older,now,later", [
    (0, False, False, "600.00", "0.00"), (100, False, False, "500.00", "0.00"),
    (600, False, False, "0.00", "0.00"), (0, True, False, "600.00", "250.00"),
    (0, True, True, "600.00", "300.00"),
])
def test_funding_estimates(app, credit, ntuc, older, now, later):
    with app.app_context():
        result = estimate(app.extensions["knowledge_base"], credit, ntuc, older)
        assert result["pay_now"] == now
        assert result["possible_reimbursement"] == later


@pytest.mark.parametrize("options", [
    {"credit": -1}, {"credit": "NaN"}, {"credit": "Infinity"}, {"credit": "1.111"},
    {"credit": 601}, {"credit": 100, "ntuc": True}, {"mid_career": True}, {"ntuc": "yes"},
])
def test_funding_rejects_invalid_inputs(app, options):
    with app.app_context(), pytest.raises(ValueError):
        estimate(app.extensions["knowledge_base"], **options)


def test_funding_caps_and_live_fee(app, monkeypatch):
    with app.app_context():
        kb = app.extensions["knowledge_base"]
        monkeypatch.setattr(kb.catalogue, "course_for", lambda _: {"fee": {"amount": 1200}})
        assert estimate(kb, ntuc=True)["possible_reimbursement"] == "250.00"
        assert estimate(kb, ntuc=True, age_40=True)["possible_reimbursement"] == "500.00"


def test_menu_round_trip_and_limits():
    labels = ["Can I use Mid-Career credit?", "NTUC UTAP support", "Course fees", "Next intake dates"]
    menu = interactive_payload("Choose a topic", labels)
    assert menu["type"] == "list"
    rows = menu["action"]["sections"][0]["rows"]
    assert all(len(row["title"]) <= 24 for row in rows)
    assert inbound_text({"interactive": {"list_reply": rows[0]}}) == labels[0]
    menu = interactive_payload("Hello", ["Fees", "Dates"])
    assert menu["type"] == "button"
    reply = menu["action"]["buttons"][0]["reply"]
    assert inbound_text({"interactive": {"button_reply": reply}}) == "Fees"
    assert interactive_payload("x" * 1025, labels) is None


def test_guided_funding_and_stop(client, conversation_id):
    def send(text):
        response = client.post("/api/chat", json={"conversation_id": conversation_id, "message": text})
        assert response.status_code == 200
        return response.get_json()["message"]["content"]
    assert "Which course" in send("Calculate funding")
    assert "basic-tier" in send("1")
    assert "NTUC" in send("0")
    assert "40" in send("yes")
    assert "300.00" in send("yes")
    send("Calculate funding")
    send("STOP")
    with client.application.app_context():
        assert db.session.get(Conversation, conversation_id).funding_state is None


@pytest.mark.parametrize("selection", ["2", "Front Desk Operations"])
@pytest.mark.parametrize("trigger", ["Calculate funding", "c alculate funding"])
def test_new_chat_funding_requires_course_and_uses_discounted_front_desk_fee(
    client, conversation_id, selection, trigger,
):
    def send(text):
        response = client.post("/api/chat", json={"conversation_id": conversation_id, "message": text})
        assert response.status_code == 200
        return response.get_json()["message"]["content"]

    reply = send(trigger)
    assert "Which course" in reply
    assert "Basic Certificate in Dental Assisting" in reply
    assert "Certificate in Dental Clinic Front Desk Operations" in reply
    assert "basic-tier" not in reply
    assert "Which course" in send("0")
    reply = send(selection)
    assert reply.startswith("How much basic-tier SkillsFuture Credit")
    assert "Front Desk Operations" not in reply
    reply = send("100")
    assert "Course fee: S$450.00" in reply
    assert "Q&M-funded discount (10%): -S$45.00" in reply
    assert "Pay now: S$305.00" in reply
    assert "estimate" not in reply.casefold()
    assert "15 November 2026 at 11:59 pm" in reply
    assert "two-week enrolment promotion" in reply
    assert "Singapore time" not in reply
    assert "reimbursement later" not in reply
    assert "claim approval" not in reply
    assert "Basic SkillsFuture Credit used: S$100.00" in reply
    assert "Funding details:" in reply
    with client.application.app_context():
        conversation = db.session.get(Conversation, conversation_id)
        assert conversation.funding_state is None
        assert conversation.enrollment_draft is None
        recorded = Message.query.filter_by(conversation_id=conversation_id, role="assistant").order_by(Message.created_at.desc()).first()
        assert recorded.response_type == "grounded_answer"
        assert set(recorded.kb_entry_ids) == {"fd.utap", "fd.payment_modes", "fd.mid_career", "fd.catalogue_facts"}


@pytest.mark.parametrize("course,expected", [
    ("Dental Assisting", "basic-tier"),
    ("Front Desk Operations", "basic-tier"),
])
def test_funding_respects_course_named_in_request(client, conversation_id, course, expected):
    response = client.post("/api/chat", json={
        "conversation_id": conversation_id, "message": f"Calculate funding for {course}",
    })
    assert response.status_code == 200
    assert expected in response.get_json()["message"]["content"]


def test_funding_new_chat_does_not_use_remembered_course(client, conversation_id):
    response = client.post("/api/chat", json={
        "conversation_id": conversation_id, "message": "Calculate funding for Dental Assisting",
    })
    assert "basic-tier" in response.get_json()["message"]["content"]
    new_chat = client.post("/api/conversations").get_json()["conversation_id"]
    response = client.post("/api/chat", json={
        "conversation_id": new_chat, "message": "Calculate funding",
    })
    reply = response.get_json()["message"]["content"]
    assert "Which course" in reply
    assert "basic-tier" not in reply
    response = client.post("/api/chat", json={
        "conversation_id": new_chat, "message": "cancel",
    })
    assert "cancelled" in response.get_json()["message"]["content"]


@pytest.mark.parametrize("today,payable,active", [
    (date(2026, 11, 15), Decimal("405"), True),
    (date(2026, 11, 16), Decimal("450"), False),
])
def test_front_desk_discount_expiry_updates_chat_and_catalogue(monkeypatch, today, payable, active):
    catalogue = CourseCatalogue()
    # Even a course object obtained during the promotion must be rechecked.
    monkeypatch.setattr(catalogue, "_pricing_today", lambda: date(2026, 11, 1))
    selected = catalogue.course_for("dental-clinic-front-desk-operations")
    monkeypatch.setattr(catalogue, "_pricing_today", lambda: today)
    kb = KnowledgeBase(catalogue)
    conversation = SimpleNamespace(funding_state=None)
    assert "basic-tier" in chat_estimate(conversation, "Calculate funding", kb, course=selected)
    reply = chat_estimate(conversation, "100", kb)
    assert f"Pay now: S${payable - 100:.2f}" in reply
    assert "estimate" not in reply.casefold()
    assert catalogue.course_fee_for(selected) == payable
    assert catalogue.public_summary()["additional_courses"][0]["fee"]["amount"] == payable
    rendered = kb.render()
    assert "15 November 2026" in rendered
    if active:
        assert "Q&M-funded discount (10%): -S$45" in reply
        assert "S$405 nett" in rendered
    else:
        assert "Q&M-funded discount" not in reply
        assert "S$405" not in rendered
        assert "S$405" not in reply
    # Resolving an expired promotion must not rewrite the configured offer.
    assert catalogue._data["additional_courses"][0]["fee"]["amount"] == 405


@pytest.mark.parametrize("credit,ntuc,older,now,later", [
    (100, False, False, "305.00", "0.00"),
    (405, False, False, "0.00", "0.00"),
    (0, True, False, "405.00", "202.50"),
    (0, True, True, "405.00", "202.50"),
])
def test_front_desk_funding_uses_fee_after_discount(app, credit, ntuc, older, now, later):
    with app.app_context():
        result = estimate(app.extensions["knowledge_base"], credit, ntuc, older,
                          course_id="dental-clinic-front-desk-operations")
        assert result["pay_now"] == now
        assert result["possible_reimbursement"] == later


@pytest.mark.parametrize("selection,reimbursement", [("1", "300.00"), ("2", "202.50")])
def test_both_courses_share_guided_utap_flow(client, conversation_id, selection, reimbursement):
    def send(text):
        response = client.post("/api/chat", json={"conversation_id": conversation_id, "message": text})
        assert response.status_code == 200
        return response.get_json()["message"]["content"]
    assert "Which course" in send("Calculate funding")
    assert "basic-tier" in send(selection)
    assert "NTUC" in send("0")
    assert "40" in send("yes")
    result = send("yes")
    assert f"Possible reimbursement later: S${reimbursement}" in result
    assert "Funding details:" in result
    assert "UTAP reimbursement is subject to eligibility, remaining annual entitlement and claim approval." in result


def test_front_desk_credit_cannot_exceed_discounted_fee(app):
    with app.app_context(), pytest.raises(ValueError, match="cannot exceed"):
        estimate(app.extensions["knowledge_base"], credit=406,
                 course_id="dental-clinic-front-desk-operations")


@pytest.mark.parametrize("initial,selection,name,pay_now", [
    ("2", "1.", "Basic Certificate in Dental Assisting", "300.00"),
    ("2", "1", "Basic Certificate in Dental Assisting", "300.00"),
    ("2", "first", "Basic Certificate in Dental Assisting", "300.00"),
    ("1", "2.", "Certificate in Dental Clinic Front Desk Operations", "105.00"),
])
def test_course_choice_after_funding_result_starts_other_course(
    app, client, conversation_id, initial, selection, name, pay_now,
):
    def send(text):
        response = client.post("/api/chat", json={"conversation_id": conversation_id, "message": text})
        assert response.status_code == 200
        return response.get_json()["message"]["content"]
    send("calculate funding")
    send(initial)
    send("300")
    switched = send(selection)
    assert switched.startswith("How much basic-tier SkillsFuture Credit")
    assert "basic-tier SkillsFuture Credit" in switched
    result = send("300")
    assert result.startswith(name)
    assert f"Pay now: S${pay_now}" in result
    assert send("calculate funding").startswith("How much basic-tier SkillsFuture Credit")
    with app.app_context():
        assert db.session.get(Conversation, conversation_id).enrollment_draft is None
        assert StaffEscalation.query.filter_by(conversation_id=conversation_id).count() == 0


def test_small_credit_amount_is_not_treated_as_course_switch(client, conversation_id):
    def send(text):
        return client.post("/api/chat", json={
            "conversation_id": conversation_id, "message": text,
        }).get_json()["message"]["content"]
    send("calculate funding")
    send("2")
    result = send("1.")
    assert result.startswith("Certificate in Dental Clinic Front Desk Operations")
    assert "Basic SkillsFuture Credit used: S$1.00" in result
    assert "Pay now: S$404.00" in result


@pytest.mark.parametrize("text", [
    "What dates are available for Dental Assisting?",
    "I want to enrol in the Basic Certificate in Dental Assisting",
])
def test_other_requests_after_funding_result_are_left_to_normal_routing(text):
    kb = KnowledgeBase(CourseCatalogue())
    previous = render(estimate(kb, 300, course_id="dental-clinic-front-desk-operations"), kb)
    conversation = SimpleNamespace(funding_state=None)
    reply = chat_estimate(conversation, text, kb,
                          prior_messages=[SimpleNamespace(role="assistant", content=previous)])
    assert reply is None
    assert conversation.funding_state is None


def test_learning_requires_explicit_approval(app):
    with app.app_context():
        operations = app.extensions["operations"]
        kb = app.extensions["knowledge_base"]
        conversation = operations.incoming_contact("whatsapp", "synthetic-learning")
        escalation = operations.escalate(conversation.id, "Is parking available?", "unanswered_question")
        db.session.commit()
        operations.staff_reply(conversation.id, "Parking costs S$12.", escalation_id=escalation.id)
        draft = KnowledgeDraft.query.one()
        assert "Parking costs" not in kb.render()
        assert draft.status == "pending"
        assert OutboundMessage.query.one().body == "Parking costs S$12."
        operations.update_content({"approve_draft_id": draft.id})
        assert "Parking costs S$12." in kb.render()
        assert kb.entry("staff." + draft.id)
        assert 12 in kb.fact_allow_list().amounts
        with pytest.raises(ValueError):
            operations.update_content({"approve_draft_id": draft.id})


def test_staff_learning_endpoints_enforce_auth_and_csrf(app):
    with app.app_context():
        operations = app.extensions["operations"]
        conversation = operations.incoming_contact("whatsapp", "synthetic-api-learning")
        escalation = operations.escalate(conversation.id, "Parking enquiry", "unanswered_question", subject="Parking")
        db.session.commit()
        conversation_id, escalation_id = conversation.id, escalation.id
    staff = app.test_client()
    assert staff.get("/api/staff/knowledge-drafts").status_code == 401
    with staff.session_transaction() as session:
        session.update(staff_username="synthetic-staff", staff_role="operations", csrf_token="test-csrf")
    headers = {"X-CSRF-Token": "test-csrf"}
    response = staff.post(f"/api/staff/cases/{conversation_id}/reply", json={"body": "Please ask reception.", "escalation_id": escalation_id}, headers=headers)
    assert response.status_code == 202
    drafts = staff.get("/api/staff/knowledge-drafts").get_json()["knowledge_drafts"]
    assert drafts[0]["topic"] == "Parking"
    payload = {"approve_draft_id": drafts[0]["id"]}
    assert staff.patch("/api/staff/content", json=payload).status_code == 403
    assert staff.patch("/api/staff/content", json=payload, headers=headers).status_code == 200
    assert staff.get("/api/staff/knowledge-drafts").get_json()["knowledge_drafts"] == []
    with app.app_context():
        from backend.services.enquiry.knowledge_base import KnowledgeBase
        assert "Please ask reception." in KnowledgeBase(app.extensions["knowledge_base"].catalogue).render()


def test_source_funnel_counts_only_confirmed_and_fully_paid(app):
    from backend.models.finance import Invoice, Payment
    with app.app_context():
        operations = app.extensions["operations"]
        conversation = operations.incoming_contact("whatsapp", "synthetic-funnel")
        lead = operations.lead_for(conversation.id)
        lead.source, lead.ad_id = "facebook_ad", "ad-conversion"
        draft = EnrollmentDraft(conversation_id=conversation.id, full_name="Synthetic Participant", status="confirmed", confirmed_at=utc_now())
        db.session.add(draft)
        db.session.flush()
        invoice = Invoice(enrollment_id=draft.id, number="SYNTHETIC-FUNNEL", course_name="Synthetic", participant_name=draft.full_name,
                          course_fee=600, net_payable=600, recipient_uen="synthetic", due_date=utc_now().date())
        db.session.add(invoice)
        db.session.flush()
        payment = Payment(enrollment_id=draft.id, invoice_id=invoice.id, amount=300, reference="synthetic-partial", transaction_date=utc_now().date(), confirmed_by="synthetic")
        db.session.add(payment)
        db.session.commit()
        assert analytics()["funnel"] == [{"source": "facebook_ad", "ad_id": "ad-conversion", "enquiry": 1, "enrolled": 1, "paid": 0}]
        payment.amount = 600
        db.session.commit()
        assert analytics()["funnel"][0]["paid"] == 1
        draft.cancelled_at = utc_now()
        db.session.commit()
        assert analytics()["funnel"][0]["enrolled"] == 0


def test_metrics_and_priority_use_sent_followups(app):
    with app.app_context():
        operations = app.extensions["operations"]
        conversation = operations.incoming_contact("whatsapp", "synthetic-metrics")
        lead = operations.lead_for(conversation.id)
        start = utc_now() - timedelta(hours=1)
        db.session.add(Message(conversation_id=conversation.id, role="user", content="What are the dates and fees?", created_at=start))
        db.session.add(Message(conversation_id=conversation.id, role="assistant", content="Approved answer", response_type="grounded_answer", kb_entry_ids=["da.utap"], created_at=start + timedelta(seconds=2)))
        draft = EnrollmentDraft(conversation_id=conversation.id, full_name="Test Participant")
        db.session.add(draft)
        outbound = OutboundMessage(channel="whatsapp", recipient="synthetic-metrics", body="Reminder", sent_at=start + timedelta(minutes=1), status="sent")
        db.session.add(outbound)
        db.session.flush()
        db.session.add(FollowupRun(lead_id=lead.id, sequence=0, step=0, status="sent", delivery_id=outbound.id))
        db.session.add(Message(conversation_id=conversation.id, role="user", content="Thanks", created_at=start + timedelta(minutes=2)))
        db.session.commit()
        result = analytics()
        assert result["containment_rate"] == 1
        assert result["median_first_response_seconds"] == 2
        assert result["followup_reengagement"] == {1: 1}
        assert operations.serialize_lead(lead)["temperature"] == "Hot"
        operations.escalate(conversation.id, "Please confirm parking", "unanswered_question", subject="Parking")
        db.session.commit()
        assert analytics()["escalation_rates_by_category"] == {"unanswered_question": 1}


def test_development_inbound_referral_and_menu(app, fake_ai):
    app.config["AUTOMATION_API_TOKEN"] = "test-token"
    client = app.test_client()
    payload = {"id": "synthetic-ad", "from": "synthetic-ad-sender", "text": {"body": "Hello"},
               "referral": {"source_id": "ad-123", "headline": "Dental assisting", "ctwa_clid": "click-1"}}
    response = client.post("/api/development/inbound", json=payload, headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 202
    with app.app_context():
        assert app.extensions["process_inbox"]() == 1
        from backend.models.operations import Lead
        lead = Lead.query.one()
        assert (lead.source, lead.ad_id, lead.ad_headline) == ("facebook_ad", "ad-123", "Dental assisting")
        assert OutboundMessage.query.one().quick_replies
        assert "Dental assisting" in fake_ai.enquiry_calls[0]["profile"]
        assert "untrusted external text" in fake_ai.enquiry_calls[0]["profile"]
        app.extensions["operations"].incoming_contact("whatsapp", payload["from"])
        assert lead.source == "facebook_ad"


@pytest.mark.parametrize("recent", [True, False])
def test_whatsapp_dispatch_respects_service_window(app, recent):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from backend.models.communications import InboxEvent
    from backend.services.delivery_service import DeliveryService
    app.config.update(WHATSAPP_ACCESS_TOKEN="synthetic-token", WHATSAPP_PHONE_NUMBER_ID="synthetic-phone",
                      WHATSAPP_FOLLOWUP_TEMPLATE="approved_followup", WHATSAPP_TEMPLATE_LANGUAGE="en")
    with app.app_context():
        db.session.add(InboxEvent(channel="whatsapp", external_id="menu-window", payload=json.dumps({"from": "synthetic-recipient"}),
                                  created_at=utc_now() - timedelta(hours=1 if recent else 25)))
        db.session.commit()
        http = Mock()
        http.post.return_value.json.return_value = {"messages": [{"id": "provider-1"}]}
        row = SimpleNamespace(recipient="synthetic-recipient", body="Choose a topic", quick_replies=["Fees", "Dates"])
        DeliveryService(http=http)._whatsapp(row)
        payload = http.post.call_args.kwargs["json"]
        assert payload["type"] == ("interactive" if recent else "template")
        assert row.provider_id == "provider-1"


def test_long_whatsapp_answers_keep_a_separate_menu(app, monkeypatch):
    from backend.integrations_api import ChatService
    from backend.models.communications import InboxEvent
    monkeypatch.setattr(ChatService, "respond", lambda *args: {"message": {"content": "x" * 1100}, "quick_replies": ["Fees", "Dates"]})
    with app.app_context():
        db.session.add(InboxEvent(channel="whatsapp", external_id="synthetic-long-reply", payload=json.dumps({"from": "synthetic-long-sender", "body": "Hello"})))
        db.session.commit()
        assert app.extensions["process_inbox"]() == 1
        rows = OutboundMessage.query.all()
        assert len(rows) == 2
        assert next(row for row in rows if row.purpose == "inbound_reply").body == "x" * 1100
        assert next(row for row in rows if row.purpose == "inbound_menu").quick_replies == ["Fees", "Dates"]


def test_organic_post_referrals_are_not_counted_as_ads(app):
    from backend.models.communications import InboxEvent
    from backend.models.operations import Lead
    with app.app_context():
        db.session.add(InboxEvent(channel="whatsapp", external_id="synthetic-post", payload=json.dumps({
            "from": "synthetic-post-sender", "body": "Hello", "referral": {"source_type": "post", "source_id": "organic-post"}})))
        db.session.commit()
        assert app.extensions["process_inbox"]() == 1
        lead = Lead.query.one()
        assert lead.source == "whatsapp_direct"
        assert lead.ad_id is None


def test_lead_attribution_migration_is_repeatable(app):
    from sqlalchemy import text, inspect
    from backend.services.database_migrations import apply_additive_migrations
    with app.app_context():
        operations = app.extensions["operations"]
        conversation = operations.incoming_contact("whatsapp", "synthetic-legacy")
        with db.engine.begin() as connection:
            for column in ("source", "ad_id", "ad_headline", "ctwa_clid"):
                connection.execute(text(f"ALTER TABLE leads DROP COLUMN {column}"))
        apply_additive_migrations()
        apply_additive_migrations()
        db.session.expire_all()
        lead = operations.lead_for(conversation.id)
        assert lead.source == "whatsapp_direct"
        assert lead.recipient == "synthetic-legacy"
        assert {"source", "ad_id", "ad_headline", "ctwa_clid"} <= {column["name"] for column in inspect(db.engine).get_columns("leads")}
