"""Branded PDFs from immutable invoice data and approved configurable assets."""

from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from flask import current_app
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from backend.services.privacy import mask_nric
from backend.models import utc_now


class DocumentService:
    def _asset(self, key):
        configured = current_app.config.get(key, "")
        if key == "FINANCE_SIGNATURE_PATH":
            configured = configured or current_app.config.get("INVOICE_SIGNATURE_PATH", "")
        if not configured:
            return None
        path = Path(configured)
        if not path.is_file():
            raise ValueError(f"Configured {key} asset is unavailable")
        return path

    def render(self, invoice, kind="invoice", number=None, payment=None, reason=None):
        buffer = BytesIO()
        styles = getSampleStyleSheet()
        styles.add(ParagraphStyle(name="Muted", parent=styles["Normal"], textColor=colors.HexColor("#536477"), leading=15))
        styles.add(ParagraphStyle(name="Right", parent=styles["Normal"], alignment=TA_RIGHT))
        navy = colors.HexColor("#16354d")
        teal = colors.HexColor("#167b7b")
        styles["Title"].textColor = navy
        styles["Heading2"].textColor = teal
        doc = SimpleDocTemplate(buffer, pagesize=(210 * mm, 297 * mm), leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm, bottomMargin=22 * mm,
                                title=f"Q&M {kind.replace('_', ' ').title()} {number or invoice.number}")
        def p(value, style="Normal"):
            return Paragraph(escape(str(value or "")), styles[style])
        company = current_app.config.get("FINANCE_COMPANY_NAME", "Q&M Dental Group (Singapore) Limited")
        story = [p(company, "Heading2"), p("COURSE ADMINISTRATION", "Muted"), Spacer(1, 6 * mm), p(kind.replace("_", " ").upper(), "Title")]
        document_number = number or invoice.number
        story.extend([p(f"Document number: {document_number}", "Muted"), p(f"Issued: {(invoice.created_at if kind == 'invoice' else utc_now()).date().isoformat()}", "Muted"), Spacer(1, 5 * mm)])
        draft = invoice.enrollment
        for heading, value in [("Participant", invoice.participant_name), ("NRIC / FIN", mask_nric(draft.nric)), ("Email", draft.email), ("Course", invoice.course_name), ("Course date", invoice.course_date.isoformat() if invoice.course_date else "To be confirmed by Q&M")]:
            story.append(p(f"{heading}: {value}"))
            story.append(Spacer(1, 2 * mm))
        story.append(Spacer(1, 6 * mm))
        funding_label = "SkillsFuture Credit confirmed" if kind == "receipt" else "Proposed SkillsFuture Credit claim*"
        fee_rows = [[p("FEE BREAKDOWN"), p("SGD", "Right")], [p("Course fee (nett)"), p(f"{invoice.course_fee:.2f}", "Right")], [p(funding_label), p(f"{invoice.skillsfuture_amount:.2f}", "Right")], [p("PayNow payable"), p(f"{invoice.net_payable:.2f}", "Right")]]
        if kind == "receipt":
            fee_rows.append([p("Full course fee received"), p(f"{invoice.course_fee:.2f}", "Right")])
        if kind == "credit_note":
            fee_rows.append([p("Invoice cancelled - course fee credited"), p(f"{invoice.course_fee:.2f}", "Right")])
        table = Table(fee_rows, colWidths=[124 * mm, 46 * mm])
        table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e6f1f1")), ("BOTTOMPADDING", (0, 0), (-1, -1), 7), ("TOPPADDING", (0, 0), (-1, -1), 7), ("LINEBELOW", (0, -1), (-1, -1), 1, teal), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        funding_note = "All invoice payment components have been recorded as confirmed by the payment verification or accounts reconciliation workflow." if kind == "receipt" else "*A proposed SkillsFuture Credit claim is subject to approval. Credit is not treated as received until accounts confirms the claim."
        story.extend([table, Spacer(1, 5 * mm), p(funding_note, "Muted"), Spacer(1, 6 * mm)])
        if kind == "invoice":
            story.extend([p("PAYMENT INSTRUCTIONS", "Heading2"), p(f"PayNow UEN: {invoice.recipient_uen}"), p(f"Include invoice {invoice.number} as the payment reference."), p(f"Payment due: {invoice.due_date.isoformat()}"), Spacer(1, 3 * mm)])
            qr = self._asset("PAYNOW_QR_PATH")
            if qr:
                story.append(Image(str(qr), width=26 * mm, height=26 * mm, kind="proportional"))
            else:
                story.append(p("PayNow QR asset awaits approval. Use the UEN above and confirm the recipient with Q&M before payment.", "Muted"))
            portal = current_app.config.get("SKILLSFUTURE_CLAIM_URL", "https://sfc.myskillsfuture.gov.sg/claim")
            story.extend([Spacer(1, 3 * mm), p("SkillsFuture: check your own balance and submit a claim through the official MySkillsFuture portal. No Singpass credentials are collected."), p(portal, "Muted")])
        elif kind == "receipt":
            story.extend([p("PAYMENT CONFIRMATION", "Heading2"), p(f"Transaction reference: {payment.reference}"), p(f"Transaction date: {payment.transaction_date.isoformat()}"), p("This receipt confirms the recorded course payment.", "Muted")])
        else:
            story.extend([p("CANCELLATION", "Heading2"), p(f"Original invoice: {invoice.number}"), p(f"Reason: {reason}"), p("A credit note cancels the invoice. It does not confirm any refund or cancel a government claim.", "Muted")])
        signature = self._asset("FINANCE_SIGNATURE_PATH")
        stamp = self._asset("COMPANY_STAMP_PATH")
        authorization = [Spacer(1, 4 * mm)]
        if signature:
            assets = [Image(str(signature), width=45 * mm, height=12 * mm, kind="proportional")]
            if stamp:
                assets.append(Image(str(stamp), width=30 * mm, height=15 * mm, kind="proportional"))
            authorization.extend([Table([assets], hAlign='LEFT'), p("Authorized signature", "Muted")])
        else:
            authorization.append(p("DRAFT - approved signature asset is not configured.", "Muted"))
        story.append(KeepTogether(authorization))
        def footer(canvas, document):
            canvas.setStrokeColor(teal)
            canvas.line(20 * mm, 16 * mm, 190 * mm, 16 * mm)
            canvas.setFont("Helvetica", 8)
            canvas.setFillColor(navy)
            canvas.drawString(20 * mm, 11 * mm, f"UEN {invoice.recipient_uen} | {document_number}")
            canvas.drawRightString(190 * mm, 11 * mm, f"Page {document.page}")
        doc.build(story, onFirstPage=footer, onLaterPages=footer)
        return buffer.getvalue(), signature is not None

    def render_claim_instructions(self, course):
        buffer = BytesIO()
        styles = getSampleStyleSheet()
        document = SimpleDocTemplate(buffer, pagesize=(210 * mm, 297 * mm), leftMargin=20 * mm, rightMargin=20 * mm)
        paragraphs = [Paragraph('Q&amp;M Course Administration', styles['Heading2']), Paragraph('SkillsFuture claim instructions', styles['Title'])]
        lines = [course['name'], course['skillsfuture']['guidance'],
                 '1. Sign in independently to the official MySkillsFuture portal. Never share your Singpass password or OTP with course administration.',
                 '2. Check the available balance in your basic-tier SkillsFuture Credit. Mid-Career Credits are not accepted for this course.',
                 '3. Select the approved course and confirmed intake, and submit your claim using the attached invoice and the amount you confirmed with course administration.',
                 course['skillsfuture']['claim_page'],
                 '4. Pay any remaining amount through PayNow using the approved UEN and QR supplied with your invoice. Include your invoice number as the payment reference.',
                 '5. Email your claim or payment screenshot to course administration. Email is preferred; WhatsApp evidence is also accepted. Pending claims require accounts confirmation.',
                 'Participants are responsible for checking eligibility and completing their own claim. Q&M does not access your government account or submit claims for you.']
        for line in lines:
            paragraphs.extend([Spacer(1, 4 * mm), Paragraph(escape(line), styles['Normal'])])
        document.build(paragraphs)
        return buffer.getvalue()
