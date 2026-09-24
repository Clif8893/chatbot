"""Flask REST API for the Q&M CrewAI enquiry and enrollment prototype."""

from io import BytesIO
from pathlib import Path
import tempfile

import click
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.utils import secure_filename

from backend.agents.schemas import AIConfigurationError, AIServiceError
from backend.config import Config
from backend.extensions import db
from backend.models import (  # noqa: F401
    Conversation,
    EnrollmentDraft,
    communications,
    finance,
    operations,
    quality,
)
from backend.finance_api import create_finance_blueprint
from backend.integrations_api import register_integrations
from backend.operations_api import create_operations_blueprint
from backend.quality_api import register_quality
from backend.services.chat_service import (
    ChatService,
    clean_customer_copy,
    serialize_enrollment,
    serialize_message,
)
from backend.services.conversation_repository import ConversationRepository
from backend.services.course_catalogue import CourseCatalogue
from backend.services.current_enrollment_service import CurrentEnrollmentResolver
from backend.services.crewai_service import CrewAIService
from backend.services.database_migrations import apply_additive_migrations
from backend.services.delivery_service import DeliveryService
from backend.services.enrollment_management_service import EnrollmentManagementService
from backend.services.finance_service import FinanceService
from backend.services.invoice_template_service import InvoiceTemplateService
from backend.services.invoice_email_service import (
    InvoiceEmailError,
    InvoiceEmailService,
    ResendCooldownError,
)
from backend.services.master_invoice_service import MasterInvoiceService
from backend.services.maintenance_service import MaintenanceService
from backend.services.intake_service import IntakeService
from backend.services.operations_service import OperationsService
from backend.services.privacy import mask_nrics_in_text
from backend.services.security import (
    EncryptedStorage,
    current_participant_id,
    initialize_crypto,
    participant_required,
    register_participant_auth,
    register_staff_auth,
    staff_required,
)


def create_app(test_config=None, ai_service=None, catalogue=None):
    """Create the REST API; tests may inject a model-free agent substitute."""
    app = Flask(
        __name__,
        static_folder=None,
        instance_path=str(Path(__file__).resolve().parent / "data"),
    )
    app.config.from_object(Config)
    if test_config:
        app.config.update(test_config)
    if (
        app.config.get("TESTING")
        and test_config
        and "ENABLE_DEMO_INTAKES" not in test_config
    ):
        app.config["ENABLE_DEMO_INTAKES"] = False
    if (
        app.config.get("TESTING")
        and test_config
        and "INVOICE_GENERATION_ENABLED" not in test_config
    ):
        app.config["INVOICE_GENERATION_ENABLED"] = False
    if (
        app.config.get("TESTING")
        and test_config
        and "EMAIL_DELIVERY_ENABLED" not in test_config
    ):
        app.config["EMAIL_DELIVERY_ENABLED"] = False
    if (
        app.config.get("TESTING")
        and test_config
        and "EMAIL_LIVE_TEST_ENABLED" not in test_config
    ):
        app.config["EMAIL_LIVE_TEST_ENABLED"] = False
    if (
        app.config.get("TESTING")
        and test_config
        and "MASTER_INVOICE_PATH" not in test_config
    ):
        database_uri = app.config.get("SQLALCHEMY_DATABASE_URI", "")
        if database_uri.startswith("sqlite:///"):
            database_file = database_uri.removeprefix("sqlite:///")
            if database_file == ":memory:":
                app.config["MASTER_INVOICE_PATH"] = str(
                    Path(tempfile.mkdtemp(prefix="qm-master-invoice-test-"))
                    / "Master_Invoice_List.xlsx"
                )
            else:
                app.config["MASTER_INVOICE_PATH"] = str(
                    Path(database_file).with_name("Master_Invoice_List.xlsx")
                )
    if app.config.get("PRODUCTION") and app.config["SECRET_KEY"] == "development-only-change-me":
        raise RuntimeError("A private FLASK_SECRET_KEY is required in production.")

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    initialize_crypto(app)
    db.init_app(app)
    CORS(app, origins=app.config["CORS_ORIGINS"], supports_credentials=True)
    register_participant_auth(app)
    register_staff_auth(app)
    register_quality(app)

    repository = ConversationRepository()
    catalogue = catalogue or CourseCatalogue(
        enable_demo_intakes=app.config.get("ENABLE_DEMO_INTAKES", False)
    )
    app.extensions["agent_service"] = ai_service
    app.extensions["catalogue"] = catalogue
    app.extensions["repository"] = repository
    intake_service = IntakeService(
        timezone_name=app.config["ENROLLMENT_TIMEZONE"],
        current_date=app.config.get("ENROLLMENT_CURRENT_DATE"),
    )
    delivery = DeliveryService()
    finance_service = FinanceService(catalogue, delivery, EncryptedStorage())
    enrollment_validation_options = {
        "minimum_age": app.config["MINIMUM_ENROLLMENT_AGE"],
        "maximum_age": app.config["MAXIMUM_ENROLLMENT_AGE"],
        "as_of_date": app.config.get("ENROLLMENT_CURRENT_DATE"),
        "timezone_name": app.config["ENROLLMENT_TIMEZONE"],
        "mobile_country_code": app.config["MOBILE_COUNTRY_CODE"],
    }
    master_invoice_service = MasterInvoiceService(
        app.config["MASTER_INVOICE_PATH"],
        app.config["MASTER_INVOICE_TEMPLATE_PATH"],
        app.config["MASTER_INVOICE_LOCK_TIMEOUT_SECONDS"],
        catalogue,
        invoice_prefix=app.config["MASTER_INVOICE_NUMBER_PREFIX"],
        validation_options=enrollment_validation_options,
    )
    invoice_template_service = InvoiceTemplateService(
        app.config["INVOICE_TEMPLATE_SOURCE_PATH"],
        app.config["STORAGE_DIR"],
        attachment_name=app.config["INVOICE_TEMPLATE_ATTACHMENT_NAME"],
        approved=app.config["INVOICE_TEMPLATE_APPROVED"],
        enabled=app.config["INVOICE_GENERATION_ENABLED"],
        lock_timeout=app.config["INVOICE_LOCK_TIMEOUT_SECONDS"],
        catalogue=catalogue,
    )
    invoice_email_service = InvoiceEmailService(delivery, invoice_template_service)
    enrollment_resolver = CurrentEnrollmentResolver(catalogue)
    maintenance_service = MaintenanceService(
        master_invoice_service,
        app.config["STORAGE_DIR"],
        production=app.config.get("PRODUCTION", False),
    )
    finance_service.invoice_templates = invoice_template_service
    finance_service.invoice_email = invoice_email_service
    enrollment_management_service = EnrollmentManagementService(
        catalogue,
        intake_service,
        master_invoice_service,
        invoice_email_service,
        invoice_template_service,
        enrollment_resolver,
    )
    operations_service = OperationsService(
        catalogue,
        delivery,
        followup_hours=app.config["FOLLOWUP_HOURS"],
        finance_service=finance_service,
        intake_service=intake_service,
        master_invoice_service=master_invoice_service,
    )
    app.extensions.update(
        delivery=delivery,
        finance=finance_service,
        master_invoice=master_invoice_service,
        invoice_template=invoice_template_service,
        invoice_email=invoice_email_service,
        maintenance=maintenance_service,
        enrollment_management=enrollment_management_service,
        enrollment_resolver=enrollment_resolver,
        operations=operations_service,
        intakes=intake_service,
    )

    with app.app_context():
        db.create_all()
        apply_additive_migrations()
        intake_service.seed_intakes(catalogue.approved_intakes)
        if app.config.get("ENABLE_DEMO_INTAKES"):
            intake_service.seed_demo_intakes(catalogue.demo_intakes)
        catalogue.bind_intake_provider(intake_service.catalogue_items)

    app.register_blueprint(
        create_operations_blueprint(operations_service, staff_required)
    )
    app.register_blueprint(
        create_finance_blueprint(
            finance_service,
            staff_required,
            master_invoice_service,
            invoice_template_service,
            invoice_email_service,
        )
    )

    def current_ai_service():
        service = app.extensions.get("agent_service")
        if service is None:
            service = CrewAIService(
                api_key=app.config["OPENAI_API_KEY"],
                model=app.config["OPENAI_MODEL"],
            )
            app.extensions["agent_service"] = service
        return service

    @app.post("/api/conversations")
    @participant_required
    def create_conversation():
        conversation = repository.create(user_id=current_participant_id())
        operations_service.lead_for(conversation.id)
        db.session.commit()
        return (
            jsonify(
                {
                    "conversation_id": conversation.id,
                    "status": conversation.status,
                    "created_at": _iso_timestamp(conversation.created_at),
                    "demo_intakes_enabled": catalogue.enable_demo_intakes,
                }
            ),
            201,
        )

    @app.get("/api/conversations")
    @participant_required
    def list_conversations():
        conversations = repository.list_for_user(current_participant_id())
        return jsonify(
            {
                "conversations": [
                    _conversation_summary(conversation, repository)
                    for conversation in conversations
                ]
            }
        )

    @app.get("/api/conversations/<conversation_id>/messages")
    @participant_required
    def conversation_messages(conversation_id):
        conversation = repository.get_for_user(
            conversation_id, current_participant_id()
        )
        if conversation is None:
            return _error_response(
                "CONVERSATION_NOT_FOUND", "Conversation was not found.", 404
            )
        return jsonify(
            {
                "conversation_id": conversation.id,
                "messages": [
                    serialize_message(message)
                    for message in repository.all_messages(conversation)
                ],
                "active_intent": conversation.active_intent,
                "enrollment": serialize_enrollment(
                    conversation.enrollment_draft, catalogue
                ),
            }
        )

    @app.post("/api/chat")
    @participant_required
    def chat():
        validation_error = _validate_chat_request(request)
        if validation_error:
            return validation_error

        payload = request.get_json()
        conversation = repository.get_for_user(
            payload["conversation_id"], current_participant_id()
        )
        if conversation is None:
            return _error_response(
                "CONVERSATION_NOT_FOUND", "Conversation was not found.", 404
            )

        try:
            service = ChatService(
                repository=repository,
                catalogue=catalogue,
                ai_service=current_ai_service(),
                context_limit=app.config["CONVERSATION_CONTEXT_MESSAGE_LIMIT"],
                master_invoice_service=master_invoice_service,
                invoice_template_service=invoice_template_service,
                invoice_email_service=invoice_email_service,
                intake_service=intake_service,
                enrollment_management_service=enrollment_management_service,
                enrollment_resolver=enrollment_resolver,
                validation_options=enrollment_validation_options,
            )
            return jsonify(service.respond(conversation, payload["message"].strip()))
        except AIConfigurationError:
            db.session.rollback()
            return _error_response(
                "AI_CONFIGURATION_ERROR",
                "The assistant is temporarily unavailable. Please try again later.",
                503,
            )
        except AIServiceError:
            db.session.rollback()
            return _error_response(
                "AI_SERVICE_ERROR",
                "The assistant is temporarily unavailable. Please try again.",
                502,
            )
        except SQLAlchemyError:
            db.session.rollback()
            return _error_response(
                "DATABASE_ERROR",
                "The conversation could not be saved. Please try again.",
                500,
            )

    def consent_state(conversation_id):
        consent = operations_service.consent_for(conversation_id)
        return {
            "accepted": bool(consent and consent.granted),
            "version": app.config["CONSENT_VERSION"],
        }

    def participant_invoice_state(draft):
        if draft is None:
            return None
        if draft.status == "cancelled" or not draft.confirmed_at:
            return {
                "status": "unavailable",
                "invoice_number": None,
                "failure_message": None,
                "download_url": None,
            }
        record = invoice_template_service.for_enrollment(draft.id)
        if record is None:
            return {
                "status": "pending",
                "invoice_number": None,
                "failure_message": None,
                "download_url": None,
            }
        result = invoice_template_service.serialize(record)
        result["download_url"] = (
            f"/api/participant/enrollments/{draft.id}/invoice"
            if record.status == "generated" and record.storage_key and record.sha256
            else None
        )
        return result

    @app.post("/api/conversations/<conversation_id>/consent")
    @participant_required
    def participant_consent(conversation_id):
        conversation = repository.get_for_user(
            conversation_id, current_participant_id()
        )
        if conversation is None:
            return _error_response("NOT_FOUND", "Conversation was not found.", 404)
        payload = request.get_json(silent=True) or {}
        if type(payload.get("accepted")) is not bool:
            return _error_response(
                "INVALID_CONSENT", "Consent must be true or false.", 400
            )
        operations_service.set_consent(
            conversation_id,
            payload["accepted"],
            version=app.config["CONSENT_VERSION"],
        )
        db.session.commit()
        return jsonify(consent_state(conversation_id))

    @app.get("/api/conversations/<conversation_id>/status")
    @participant_required
    def participant_status(conversation_id):
        conversation = repository.get_for_user(
            conversation_id, current_participant_id()
        )
        if conversation is None:
            return _error_response("NOT_FOUND", "Conversation was not found.", 404)
        draft = conversation.enrollment_draft
        finance_payload = (
            finance_service.serialize_case(finance_service.get_case(draft))
            if draft
            else None
        )
        if finance_payload is not None:
            finance_payload["invoice_document"] = participant_invoice_state(draft)
            finance_payload["invoice_email"] = invoice_email_service.serialize(draft)
        return jsonify(
            conversation_id=conversation_id,
            consent=consent_state(conversation_id),
            enrollment=serialize_enrollment(draft, catalogue),
            finance=finance_payload,
        )

    @app.get("/api/participant/enrollments/<enrollment_id>/invoice")
    @participant_required
    def participant_invoice_download(enrollment_id):
        draft = db.session.execute(
            db.select(EnrollmentDraft)
            .join(Conversation, Conversation.id == EnrollmentDraft.conversation_id)
            .where(
                EnrollmentDraft.id == enrollment_id,
                Conversation.user_id == current_participant_id(),
            )
        ).scalar_one_or_none()
        if draft is None:
            return _error_response("NOT_FOUND", "Invoice was not found.", 404)
        state = participant_invoice_state(draft)
        if state["status"] == "unavailable":
            return _error_response(
                "INVOICE_UNAVAILABLE",
                "An invoice is unavailable for this enrollment.",
                409,
            )
        if state["status"] == "pending":
            return (
                jsonify(
                    invoice_document=state,
                    message="Your invoice is still being generated.",
                ),
                202,
            )
        if state["status"] == "needs_review":
            return _error_response(
                "INVOICE_NEEDS_REVIEW",
                "Your invoice needs staff review before download.",
                409,
            )
        if state["status"] != "generated":
            return _error_response(
                "INVOICE_GENERATION_FAILED",
                "Invoice generation was not completed. Please check again after staff review.",
                503,
            )
        record = invoice_template_service.for_enrollment(draft.id)
        try:
            content = invoice_template_service.document_bytes(
                record, actor="participant"
            )
            db.session.commit()
        except LookupError:
            db.session.rollback()
            return _error_response(
                "INVOICE_GENERATION_FAILED",
                "The invoice file is temporarily unavailable. Please check again later.",
                503,
            )
        filename = secure_filename(record.filename) or "invoice.pdf"
        if not filename.casefold().endswith(".pdf"):
            filename += ".pdf"
        response = send_file(
            BytesIO(content),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=filename,
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/participant/enrollments/<enrollment_id>/invoice-email/resend")
    @participant_required
    def participant_invoice_email_resend(enrollment_id):
        payload = request.get_json(silent=True)
        if payload is None:
            payload = {}
        if not isinstance(payload, dict) or set(payload) - {"confirm"}:
            return _error_response(
                "INVALID_REQUEST",
                "Invoice email resend accepts confirmation only and never a recipient.",
                400,
            )
        draft = db.session.execute(
            db.select(EnrollmentDraft)
            .join(Conversation, Conversation.id == EnrollmentDraft.conversation_id)
            .where(
                EnrollmentDraft.id == enrollment_id,
                Conversation.user_id == current_participant_id(),
            )
        ).scalar_one_or_none()
        if draft is None:
            return _error_response("NOT_FOUND", "Invoice was not found.", 404)
        if payload.get("confirm") is not True:
            return (
                jsonify(
                    confirmation_required=True,
                    message="Please confirm that you want the latest invoice resent to the registered email address.",
                ),
                202,
            )
        try:
            delivery_record = invoice_email_service.participant_resend(draft)
            return jsonify(
                invoice_email=invoice_email_service.serialize(draft),
                delivery_id=delivery_record.id,
            )
        except ResendCooldownError as error:
            db.session.rollback()
            return _error_response("RESEND_COOLDOWN", str(error), 429)
        except InvoiceEmailError as error:
            db.session.rollback()
            return _error_response("INVOICE_EMAIL_UNAVAILABLE", str(error), 409)

    @app.post("/api/conversations/<conversation_id>/evidence")
    @participant_required
    def participant_evidence(conversation_id):
        conversation = repository.get_for_user(
            conversation_id, current_participant_id()
        )
        if conversation is None or conversation.enrollment_draft is None:
            return _error_response("NOT_FOUND", "Enrollment was not found.", 404)
        upload = request.files.get("file")
        if upload is None:
            return _error_response(
                "INVALID_UPLOAD", "Attach a payment screenshot.", 400
            )
        try:
            result = finance_service.submit_payment(
                conversation.enrollment_draft,
                upload.read(),
                upload.mimetype,
                channel="web",
                payment_type=request.form.get("payment_type", "paynow"),
            )
            db.session.commit()
            return jsonify(result), 201
        except ValueError as error:
            db.session.rollback()
            return _error_response("INVALID_EVIDENCE", str(error), 400)

    @app.get(
        "/api/conversations/<conversation_id>/documents/<document_id>"
    )
    @participant_required
    def participant_document(conversation_id, document_id):
        document = db.session.get(finance.FinancialDocument, document_id)
        conversation = repository.get_for_user(
            conversation_id, current_participant_id()
        )
        if (
            not document
            or not conversation
            or not conversation.enrollment_draft
            or document.enrollment_id != conversation.enrollment_draft.id
        ):
            return _error_response("NOT_FOUND", "Document was not found.", 404)
        response = send_file(
            BytesIO(EncryptedStorage().read(document.storage_key)),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=document.filename,
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    register_integrations(
        app, repository, catalogue, current_ai_service, consent_state
    )

    @app.get("/health")
    def health():
        return jsonify(
            {
                "service": "q-and-m-training-chatbot",
                "stage": "course-enquiry-enrollment-assistant",
                "status": "ok",
                "ai_configuration": {
                    "api_key_configured": bool(app.config["OPENAI_API_KEY"]),
                    "model_configured": bool(app.config["OPENAI_MODEL"]),
                },
            }
        )

    @app.cli.command("rebuild-master-invoice")
    def rebuild_master_invoice():
        """Rebuild the private workbook from validated database records."""
        if not master_invoice_service.rebuild_master_invoice_workbook(
            "flask-cli-recovery"
        ):
            raise click.ClickException(
                "Master Invoice rebuild failed. Check the staff export status and retry."
            )
        count = master_invoice_service.status()["confirmed_records"]
        click.echo(f"Master Invoice workbook rebuilt with {count} confirmed records.")

    @app.cli.command("clear-test-enrollment-data")
    @click.option("--confirm", default="", help="Required destructive confirmation phrase.")
    def clear_test_enrollment_data(confirm):
        """Back up and remove local development enrollment records."""
        dry_run = maintenance_service.dry_run()
        click.echo("Cleanup dry run:")
        for key, value in sorted(dry_run.items()):
            click.echo(f"  {key}: {value}")
        try:
            result = maintenance_service.clear(confirm)
        except (ValueError, RuntimeError) as error:
            raise click.ClickException(str(error)) from error
        click.echo(f"Backup directory: {result['backup']['directory']}")
        click.echo(f"Removed invoice PDFs: {result['removed_invoice_pdfs']}")
        click.echo(f"Remaining enrollments: {result['after']['enrollment_total']}")

    @app.cli.command("process-invoice-emails")
    @click.option("--limit", default=100, type=click.IntRange(1, 500))
    def process_invoice_emails(limit):
        """Process already queued invoice-email outbox records."""
        attempted = invoice_email_service.process(limit=limit)
        click.echo(f"Invoice email deliveries attempted: {attempted}")

    @app.cli.command("diagnose-invoice-email")
    def diagnose_invoice_email():
        """Verify Gmail SMTP through authentication without sending a message."""
        password = app.config.get("SMTP_PASSWORD", "")
        try:
            from backend.services.delivery_service import normalize_smtp_password

            normalised_length = len(normalize_smtp_password(password))
        except Exception:
            normalised_length = 0
        click.echo(f"Delivery enabled: {bool(app.config.get('EMAIL_DELIVERY_ENABLED'))}")
        click.echo(f"Sender configured: {bool(app.config.get('EMAIL_FROM_ADDRESS'))}")
        click.echo(f"Password present: {'yes' if password else 'no'}")
        click.echo(f"Normalised password length: {normalised_length}")
        report = delivery.diagnose_email()
        click.echo(f"DNS connection: {'ok' if report['dns_resolution'] else 'failed'}")
        click.echo(f"STARTTLS: {'ok' if report['starttls'] else 'failed'}")
        click.echo(f"Authentication: {'ok' if report['authentication'] else 'failed'}")
        click.echo(f"Safe failure category: {report['failure_category'] or 'none'}")
        if report["smtp_response_code"] is not None:
            click.echo(f"SMTP response code: {report['smtp_response_code']}")
        if not report["authentication"]:
            raise click.ClickException("SMTP authentication diagnostic did not succeed.")

    @app.cli.command("send-test-invoice-email")
    def send_test_invoice_email():
        """Send one explicitly enabled fictional development invoice."""
        recipient = app.config.get("EMAIL_TEST_RECIPIENT", "")
        if not app.config.get("EMAIL_LIVE_TEST_ENABLED"):
            raise click.ClickException("Live invoice email testing is disabled.")
        if not app.config.get("EMAIL_DELIVERY_ENABLED"):
            raise click.ClickException("Email delivery is disabled.")
        if not recipient or any(character in recipient for character in "\r\n,;"):
            raise click.ClickException("Configure exactly one valid EMAIL_TEST_RECIPIENT.")
        sender = app.config.get("EMAIL_FROM_ADDRESS", "")
        if (
            not sender
            or sender != app.config.get("SMTP_USERNAME")
            or not app.config.get("SMTP_PASSWORD")
        ):
            raise click.ClickException(
                "The authorised Gmail sender and Google app password are not configured."
            )
        try:
            delivery_record = invoice_email_service.queue_live_test(recipient)
        except InvoiceEmailError as error:
            raise click.ClickException(str(error)) from error
        click.echo(
            "Development invoice email sent successfully. "
            f"Provider message ID: {delivery_record.provider_id or 'unavailable'}"
        )

    return app


def _validate_chat_request(incoming_request):
    if not incoming_request.is_json:
        return _error_response(
            "INVALID_CONTENT_TYPE", "Request body must be JSON.", 415
        )
    payload = incoming_request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _error_response(
            "INVALID_JSON", "Request body must contain a valid JSON object.", 400
        )
    message = payload.get("message")
    if not isinstance(message, str):
        return _error_response("INVALID_MESSAGE", "Message must be a text value.", 400)
    if not message.strip():
        return _error_response("EMPTY_MESSAGE", "Message cannot be empty.", 400)
    conversation_id = payload.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        return _error_response(
            "INVALID_CONVERSATION_ID",
            "A conversation_id text value is required.",
            400,
        )
    return None


def _error_response(code, message, status_code):
    return jsonify({"error": {"code": code, "message": message}}), status_code


def _iso_timestamp(value):
    if value.tzinfo is None:
        from datetime import timezone

        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def _conversation_summary(conversation, repository):
    last_message = repository.last_message(conversation)
    preview = "New conversation"
    if last_message:
        preview = mask_nrics_in_text(last_message.content)
        if last_message.role == "assistant":
            preview = clean_customer_copy(preview)
        preview = " ".join(preview.split())[:120]
    return {
        "conversation_id": conversation.id,
        "status": conversation.status,
        "active_intent": conversation.active_intent,
        "enrollment_status": (
            conversation.enrollment_draft.status
            if conversation.enrollment_draft
            else None
        ),
        "preview": preview,
        "created_at": _iso_timestamp(conversation.created_at),
        "updated_at": _iso_timestamp(conversation.updated_at),
    }


app = create_app()


if __name__ == "__main__":
    app.run(port=app.config["PORT"])
