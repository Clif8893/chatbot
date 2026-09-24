"""Static checks for preserving structured chatbot replies in the existing UI."""

from pathlib import Path


FRONTEND_ROOT = Path(__file__).resolve().parents[2] / "frontend" / "src"


def test_message_bubbles_preserve_paragraphs_bullets_and_line_breaks():
    component = (FRONTEND_ROOT / "components" / "MessageBubble.jsx").read_text(
        encoding="utf-8"
    )
    stylesheet = (FRONTEND_ROOT / "styles.css").read_text(encoding="utf-8")

    assert "{message.text}" in component
    assert ".message-bubble p" in stylesheet
    assert "white-space: pre-wrap" in stylesheet


def test_message_bubbles_render_only_explicit_accessible_image_metadata():
    component = (FRONTEND_ROOT / "components" / "MessageBubble.jsx").read_text(
        encoding="utf-8"
    )
    chatbot = (FRONTEND_ROOT / "components" / "ChatbotPage.jsx").read_text(
        encoding="utf-8"
    )
    stylesheet = (FRONTEND_ROOT / "styles.css").read_text(encoding="utf-8")

    assert "message.imageUrl && message.imageAlt" in component
    assert "src={message.imageUrl}" in component
    assert "alt={message.imageAlt}" in component
    assert 'target="_blank"' in component
    assert "message.image_url" in chatbot
    assert "message.image_alt" in chatbot
    assert ".message-attachment-image" in stylesheet
    assert "width: 100%" in stylesheet


def test_staff_portal_exposes_private_master_invoice_controls_only_in_accounts():
    portal = (FRONTEND_ROOT / "components" / "StaffPortal.jsx").read_text(
        encoding="utf-8"
    )
    assert 'tab === "accounts"' in portal
    assert "MasterInvoicePanel" in portal
    assert "/api/staff/master-invoice/download" in portal
    assert "/api/staff/master-invoice/regenerate" in portal
    assert "/api/staff/master-invoice/retry" in portal
    assert "Pending exports / updates required" in portal
    assert "record.retry_available" in portal
    assert "record.enrollment_id" in portal
    assert "Pending exports" in portal
    assert "Failed exports" in portal
    assert "intake_display" in portal
    assert "intake_is_demo" in portal
    assert "payment_allocation" in portal
    assert "payment_verification_status" in portal
    assert "cancellation_requested_at" in portal
    assert "cancelled_at" in portal
    assert "cancellation_reason" in portal
    assert "reschedule_requested_at" in portal
    assert "rescheduled_at" in portal
    assert "previous_intake_display" in portal
    assert "refund_review_required" in portal


def test_demo_mode_notice_is_visible_only_when_backend_metadata_enables_it():
    header = (FRONTEND_ROOT / "components" / "ChatHeader.jsx").read_text(
        encoding="utf-8"
    )
    chatbot = (FRONTEND_ROOT / "components" / "ChatbotPage.jsx").read_text(
        encoding="utf-8"
    )
    stylesheet = (FRONTEND_ROOT / "styles.css").read_text(encoding="utf-8")

    assert "Demo intake dates" not in header
    assert "demo_intakes_enabled" in chatbot
    assert ".demo-mode-badge" in stylesheet
