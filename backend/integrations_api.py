"""Authenticated Meta/email ingress and durable, replay-safe processing."""
from base64 import b64decode
import hashlib
import hmac
import json
from urllib.parse import urlparse

import requests
from flask import jsonify, request, session
from sqlalchemy.exc import IntegrityError

from backend.extensions import db
from backend.models import EnrollmentDraft
from backend.models.communications import InboxEvent, OutboundMessage
from backend.models.finance import Invoice
from backend.services.chat_service import ChatService
from backend.services.security import staff_required, EncryptedStorage


CONSENT_PROMPT = (
    "Before we process payment evidence, please confirm that Q&M may use the "
    "submitted details for enrollment and payment administration."
)


def register_integrations(app, repository, catalogue, ai_factory, consent_state):
    operations, finance, delivery = (app.extensions[k] for k in ("operations", "finance", "delivery"))
    invoice_email = app.extensions["invoice_email"]

    def authorized():
        configured = app.config.get("AUTOMATION_API_TOKEN", "")
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
        return bool(configured and hmac.compare_digest(configured, supplied))

    def accept_event(channel, external_id, payload):
        if not isinstance(external_id, str) or not external_id or len(external_id) > 255:
            raise ValueError("A unique provider message id is required.")
        existing = db.session.execute(db.select(InboxEvent).where(InboxEvent.external_id == external_id)).scalar_one_or_none()
        if existing:
            return existing, False
        row = InboxEvent(channel=channel, external_id=external_id, payload=json.dumps(payload))
        db.session.add(row)
        try:
            db.session.commit()
            return row, True
        except IntegrityError:
            db.session.rollback()
            return db.session.execute(db.select(InboxEvent).where(InboxEvent.external_id == external_id)).scalar_one(), False

    @app.get("/webhooks/whatsapp")
    def meta_verify():
        token = app.config.get("WHATSAPP_VERIFY_TOKEN", "")
        if token and request.args.get("hub.mode") == "subscribe" and hmac.compare_digest(request.args.get("hub.verify_token", ""), token):
            return request.args.get("hub.challenge", ""), 200, {"Content-Type": "text/plain"}
        return jsonify(error={"code": "INVALID_VERIFICATION", "message": "Webhook verification failed."}), 403

    @app.post("/webhooks/whatsapp")
    def meta_receive():
        secret = app.config.get("WHATSAPP_APP_SECRET", "")
        expected = "sha256=" + hmac.new(secret.encode(), request.get_data(), hashlib.sha256).hexdigest()
        if not secret or not hmac.compare_digest(expected, request.headers.get("X-Hub-Signature-256", "")):
            return jsonify(error={"code": "INVALID_SIGNATURE", "message": "Webhook signature is invalid."}), 403
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("entry", []), list):
            return jsonify(error={"message": "Invalid webhook payload."}), 400
        accepted = 0
        try:
            for entry in payload.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    if value.get("metadata", {}).get("phone_number_id") != app.config["WHATSAPP_PHONE_NUMBER_ID"]:
                        continue
                    for status in value.get("statuses", []):
                        delivery.provider_status(status.get("id"), status.get("status"))
                    for message in value.get("messages", []):
                        _, created = accept_event("whatsapp", "meta:" + message["id"], message)
                        accepted += int(created)
        except (KeyError, TypeError, AttributeError, ValueError):
            return jsonify(error={"message": "Invalid webhook payload."}), 400
        return jsonify(accepted=accepted), 200

    @app.post("/webhooks/email")
    @app.post("/api/development/inbound")
    def email_or_development():
        if not authorized() or (request.path.startswith("/api/development") and app.config.get("PRODUCTION")):
            return jsonify(error={"code": "AUTH_REQUIRED", "message": "Integration access is not available."}), 403
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("from"), str) or not payload["from"].strip():
            return jsonify(error={"message": "A sender and JSON payload are required."}), 400
        channel = "email" if request.path == "/webhooks/email" else "whatsapp"
        try:
            row, created = accept_event(channel, channel + ":" + str(payload.get("id", "")), payload)
            if not payload.get("id"):
                db.session.delete(row)
                db.session.commit()
                raise ValueError("A unique message id is required.")
            return jsonify(event_id=row.id, duplicate=not created), 202
        except ValueError as error:
            return jsonify(error={"message": str(error)}), 400

    def media_bytes(media_id):
        if not isinstance(media_id, str) or not media_id.isdigit():
            raise ValueError("Invalid Meta media identifier.")
        headers = {"Authorization": "Bearer " + app.config["WHATSAPP_ACCESS_TOKEN"]}
        metadata = requests.get(f"https://graph.facebook.com/{app.config['WHATSAPP_API_VERSION']}/{media_id}", headers=headers, timeout=20)
        metadata.raise_for_status()
        info = metadata.json()
        url = info.get("url", "")
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in {"lookaside.fbsbx.com", "graph.facebook.com"}:
            raise ValueError("Meta returned an unexpected media host.")
        response = requests.get(url, headers=headers, timeout=20, stream=True, allow_redirects=False)
        response.raise_for_status()
        result = bytearray()
        for chunk in response.iter_content(65536):
            result.extend(chunk)
            if len(result) > app.config["MAX_CONTENT_LENGTH"]:
                raise ValueError("Evidence exceeds the maximum upload size.")
        return bytes(result), info.get("mime_type", "image/png")

    def process_event(row):
        payload = json.loads(row.payload)
        sender = payload["from"]
        linked_id = None
        if row.channel == "email":
            invoice_number = payload.get("invoice_number")
            invoice = db.session.execute(db.select(Invoice).where(Invoice.number == invoice_number)).scalar_one_or_none() if invoice_number else None
            if invoice and invoice.enrollment.email.casefold() == sender.casefold():
                linked_id = invoice.enrollment.conversation_id
            elif not invoice_number:
                matches = db.session.execute(db.select(EnrollmentDraft).where(EnrollmentDraft.email == sender)).scalars().all()
                if len(matches) == 1:
                    linked_id = matches[0].conversation_id
        conversation = operations.incoming_contact(row.channel, sender, conversation_id=linked_id)
        image = None
        mime = "image/png"
        attachment = payload.get("attachment") or payload.get("image")
        if payload.get('_attachment_key'):
            image = EncryptedStorage().read(payload['_attachment_key'])
            mime = payload.get('_attachment_mime', 'image/png')
        elif attachment:
            if attachment.get("data_base64"):
                image = b64decode(attachment["data_base64"], validate=True)
                mime = attachment.get("mime_type", "image/png")
            elif attachment.get("id"):
                image, mime = media_bytes(attachment["id"])
        payload['_conversation_id'] = conversation.id
        if image is not None and not payload.get('_attachment_key'):
            payload['_attachment_key'] = EncryptedStorage().save(image)
            payload['_attachment_mime'] = mime
        row.payload = json.dumps(payload)
        db.session.commit()
        text = payload.get("body") or payload.get("text", {}).get("body") or (attachment or {}).get("caption") or ""
        if not isinstance(text, str):
            raise ValueError("Message body must be text.")
        if image is not None:
            conversation.pending_followup = None
        if image is not None and not consent_state(conversation.id)["accepted"]:
            operations.escalate(conversation.id, "Payment evidence received before consent; obtain consent before processing.", "consent")
            repository.add_message(conversation, "user", text or "Payment evidence submitted before consent.", agent_name="consent")
            conversation.status = 'awaiting_consent'
            conversation.pending_followup = 'consent'
            reply = CONSENT_PROMPT
            repository.add_message(conversation, "assistant", reply, agent_name="consent")
        elif image is not None:
            repository.add_message(conversation, "user", text or "Payment evidence submitted.", agent_name="payment_verification", detected_intent="payment_submission")
            if conversation.enrollment_draft and (row.channel != "email" or not payload.get("invoice_number") or linked_id):
                payment_type = payload.get("payment_type") or ("skillsfuture" if any(word in text.casefold() for word in ("skillsfuture", "sfc")) else "paynow")
                try:
                    result = finance.submit_payment(conversation.enrollment_draft, image, mime, channel=row.channel,
                                                    source_event_id=row.external_id, payment_type=payment_type)
                    if result['verdict'] == 'confirmed':
                        record = finance.serialize_enrollment(conversation.enrollment_draft)
                        if record['status'] in {'Paid', 'Receipt Issued'}:
                            reply = 'Payment evidence received. Your invoice is fully paid. ' + (
                                'Staff will help with receipt delivery.' if record['documents_blocked'] else 'Your receipt email is being processed.'
                            )
                        else:
                            reply = (f"Payment evidence received. This payment component is confirmed. "
                                     f"Your invoice still has S${record['outstanding_amount']} outstanding. "
                                     "Any SkillsFuture component requires accounts confirmation. A receipt follows full settlement.")
                    else:
                        reply = "Payment evidence received. Our staff will review it before confirmation."
                except ValueError as error:
                    operations.escalate(conversation.id, str(error), "payment_evidence")
                    reply = str(error) + ". Our staff will help you review this submission."
            else:
                operations.escalate(conversation.id, "Payment evidence could not be associated with an invoice.", "unmatched_payment")
                reply = "Your evidence requires staff review because the participant or invoice could not be matched."
            repository.add_message(conversation, "assistant", reply, agent_name="payment_verification", detected_intent="payment_submission")
        else:
            service = ChatService(
                repository=repository,
                catalogue=catalogue,
                ai_service=ai_factory(),
                context_limit=app.config["CONVERSATION_CONTEXT_MESSAGE_LIMIT"],
                master_invoice_service=app.extensions.get("master_invoice"),
                invoice_template_service=app.extensions.get("invoice_template"),
                invoice_email_service=invoice_email,
                intake_service=app.extensions.get("intakes"),
                enrollment_management_service=app.extensions.get(
                    "enrollment_management"
                ),
            )
            response = service.respond(conversation, text or "Hello")
            reply = response["message"]["content"]
        delivery.queue(row.channel, sender, "Q&M course enquiry" if row.channel == "email" else None,
                       reply, conversation_id=conversation.id, purpose="inbound_reply")
        lead = operations.lead_for(conversation.id)
        if lead.status not in {"enrolled", "escalated", "declined"}:
            operations.awaiting_response(conversation.id)
        row.status = "processed"
        db.session.commit()

    def process_inbox(limit=50):
        processed = 0
        for _ in range(limit):
            row = db.session.execute(db.select(InboxEvent).where(InboxEvent.status == "pending")
                                     .order_by(InboxEvent.created_at).with_for_update(skip_locked=True).limit(1)).scalar_one_or_none()
            if row is None:
                break
            claimed = db.session.execute(db.update(InboxEvent).where(InboxEvent.id == row.id, InboxEvent.status == 'pending')
                          .values(status='processing', attempts=InboxEvent.attempts + 1))
            db.session.commit()
            if claimed.rowcount != 1:
                continue
            db.session.refresh(row)
            try:
                process_event(row)
            except Exception:
                db.session.rollback()
                row = db.session.get(InboxEvent, row.id)
                row.status = "failed"
                db.session.commit()
            processed += 1
        return processed

    def run_jobs(dispatch=False):
        processed = process_inbox()
        followups = operations.run_followups()
        finance.run_balance_reminders()
        finance.queue_nightly_report()
        if app.config.get('RETENTION_ENABLED'):
            from backend.services.retention_service import RetentionService
            RetentionService().run(dry_run=False)
        db.session.commit()
        invoice_attempts = invoice_email.process() if dispatch else 0
        sent = delivery.dispatch() if dispatch else 0
        finance.sync_delivery_status()
        db.session.commit()
        return {"inbox_processed": processed, "followups_queued": followups["queued"], "deliveries_attempted": sent + invoice_attempts, "invoice_email_attempts": invoice_attempts}

    app.extensions["process_inbox"] = process_inbox
    app.extensions["run_jobs"] = run_jobs

    @app.post("/api/automation/run")
    def automation_run():
        if not authorized():
            return jsonify(error={"code": "AUTH_REQUIRED", "message": "Automation authorization required."}), 403
        # Dispatch is an explicit integration operation; tests and local review queue only.
        return jsonify(run_jobs(dispatch=True))

    @app.post('/api/automation/<job>')
    def automation_job(job):
        if not authorized():
            return jsonify(error={'message': 'Automation authorization required.'}), 403
        if job == 'inbox':
            return jsonify(processed=process_inbox())
        if job == 'followups':
            return jsonify(operations.run_followups())
        if job == 'delivery':
            invoice_attempts = invoice_email.process()
            attempts = delivery.dispatch()
            finance.sync_delivery_status()
            db.session.commit()
            return jsonify(deliveries_attempted=attempts + invoice_attempts, invoice_email_attempts=invoice_attempts)
        if job == 'accounts':
            reminders = finance.run_balance_reminders()
            report = finance.queue_nightly_report()
            db.session.commit()
            return jsonify(balance_reminders=reminders, report_queued=bool(report))
        if job == 'retention':
            from backend.services.retention_service import RetentionService
            return jsonify(RetentionService().run(dry_run=not app.config.get('RETENTION_ENABLED')))
        return jsonify(error={'message': 'Unknown automation job.'}), 404

    @app.get("/api/staff/deliveries")
    @staff_required
    def staff_deliveries():
        rows = list(db.session.execute(db.select(OutboundMessage).order_by(OutboundMessage.created_at.desc()).limit(250)).scalars())
        if session.get("staff_role") not in {"accountant", "admin"}:
            rows = [row for row in rows if row.email_type != "invoice"]
        return jsonify(deliveries=[invoice_email.serialize_delivery(r) if r.email_type == "invoice" else
                                   {"id": r.id, "channel": r.channel, "status": r.status, "purpose": r.purpose,
                                    "conversation_id": r.conversation_id, "error": r.error, "attempts": r.attempts}
                                   for r in rows])

    @app.get("/api/staff/inbox")
    @staff_required
    def staff_inbox():
        rows = db.session.execute(db.select(InboxEvent).order_by(InboxEvent.created_at.desc()).limit(250)).scalars()
        return jsonify(events=[{"id": r.id, "channel": r.channel, "status": r.status, "attempts": r.attempts,
                                "conversation_id": json.loads(r.payload).get('_conversation_id'),
                                "has_attachment": bool(json.loads(r.payload).get('_attachment_key'))} for r in rows])

    @app.get("/api/staff/inbox/<event_id>/attachment")
    @staff_required(roles=("accountant", "admin"))
    def inbox_attachment(event_id):
        from io import BytesIO
        from flask import send_file
        row = db.session.get(InboxEvent, event_id)
        payload = json.loads(row.payload) if row else {}
        if not payload.get('_attachment_key'):
            return jsonify(error={"message": "Attachment not found."}), 404
        return send_file(BytesIO(EncryptedStorage().read(payload['_attachment_key'])), mimetype=payload['_attachment_mime'], as_attachment=True, download_name="inbound-evidence")

    @app.post("/api/staff/inbox/<event_id>/retry")
    @staff_required(roles=("operations", "admin"))
    def inbox_retry(event_id):
        row = db.session.get(InboxEvent, event_id)
        if not row or row.status not in {'failed', 'processing'}:
            return jsonify(error={"message": "Only failed or interrupted events can be retried after review."}), 400
        operations.audit('inbox_retry_requested', event_id, actor=session['staff_username'])
        row.status = 'pending'
        db.session.commit()
        return jsonify(status='pending')

    @app.post("/api/staff/deliveries/<delivery_id>/retry")
    @staff_required
    def delivery_retry(delivery_id):
        row = db.session.get(OutboundMessage, delivery_id)
        if row and row.email_type == "invoice":
            if session.get("staff_role") not in {"accountant", "admin"}:
                return jsonify(error={"message": "Your role cannot send invoice documents."}), 403
            try:
                retried = invoice_email.staff_retry(
                    delivery_id, session["staff_username"]
                )
            except (LookupError, RuntimeError) as error:
                db.session.rollback()
                return jsonify(error={"message": str(error)}), 400
            return jsonify(status=retried.status)
        if not row or row.status not in {'failed', 'sending'}:
            return jsonify(error={"message": "Only failed or interrupted deliveries can be retried after review."}), 400
        operations.audit('delivery_retry_requested', delivery_id, conversation_id=row.conversation_id, actor=session['staff_username'])
        row.status, row.error = 'queued', None
        db.session.commit()
        return jsonify(status='queued')
