"""Deterministic Excel-template invoice generation with private PDF storage."""

from __future__ import annotations

import email
import hashlib
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import date
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path

from filelock import FileLock, Timeout
from openpyxl import Workbook, load_workbook
from openpyxl.drawing.image import Image
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from backend.extensions import db
from backend.models import EnrollmentDraft, utc_now
from backend.models.finance import FinanceAudit, InvoiceGeneration, MasterInvoiceExport
from backend.services.privacy import mask_nric


ELIGIBLE_STATUSES = {
    "confirmed",
    "enrolled",
    "invoice_sent",
    "awaiting_payment",
    "paid",
    "receipt_issued",
}
SAFE_REASONS = {
    "approval": "The official invoice template requires Q&M approval before customer use.",
    "course_date": "A confirmed course date is required before invoice generation.",
    "master_invoice": "The Master Invoice export must assign an invoice number first.",
    "incomplete": "The confirmed enrollment is missing required validated information.",
    "cancelled": "Cancelled enrollments are not eligible for invoice generation.",
    "template": "The approved invoice template is unavailable.",
    "excel": "Microsoft Excel is unavailable. The invoice can be retried after Excel is available.",
    "conversion": "The invoice PDF could not be generated. The previous valid PDF was preserved.",
}


class ExcelUnavailableError(RuntimeError):
    """Raised when Microsoft Excel automation is not available."""


class ExcelPdfConverter:
    """Windows Excel COM adapter, kept behind a mockable boundary."""

    def convert(self, workbook_path: Path, pdf_path: Path, sheet_name="Invoice"):
        try:
            import pythoncom
            import win32com.client
        except ImportError as error:  # pragma: no cover - platform dependent
            raise ExcelUnavailableError from error

        excel = workbook = None
        pythoncom.CoInitialize()
        try:  # pragma: no cover - exercised only by optional local smoke testing
            try:
                excel = win32com.client.DispatchEx("Excel.Application")
            except Exception as error:
                raise ExcelUnavailableError from error
            excel.Visible = False
            excel.DisplayAlerts = False
            workbook = excel.Workbooks.Open(
                str(workbook_path.resolve()), ReadOnly=False, UpdateLinks=0
            )
            worksheet = workbook.Worksheets(sheet_name)
            worksheet.PageSetup.PrintArea = "$A$1:$K$43"
            worksheet.PageSetup.Orientation = 1  # xlPortrait
            worksheet.PageSetup.PaperSize = 9  # xlPaperA4
            worksheet.PageSetup.Zoom = False
            worksheet.PageSetup.FitToPagesWide = 1
            worksheet.PageSetup.FitToPagesTall = 1
            worksheet.PageSetup.CenterHorizontally = True
            workbook.RefreshAll()
            excel.CalculateFullRebuild()
            worksheet.ExportAsFixedFormat(0, str(pdf_path.resolve()))
        finally:
            if workbook is not None:
                workbook.Close(SaveChanges=False)
            if excel is not None:
                excel.Quit()
            pythoncom.CoUninitialize()
        _normalise_pdf_to_a4(pdf_path)


class InvoiceTemplateService:
    """Generate one idempotent invoice PDF from the official workbook template."""

    sheet_name = "Invoice"

    def __init__(
        self,
        source_path,
        storage_root,
        *,
        attachment_name="College & CN Template.xlsx",
        approved=False,
        enabled=True,
        converter=None,
        lock_timeout=10,
        catalogue=None,
    ):
        self.source_path = Path(source_path) if source_path else None
        self.storage_root = Path(storage_root)
        self.invoice_dir = self.storage_root / "invoices"
        self.attachment_name = attachment_name
        self.approved = bool(approved)
        self.enabled = bool(enabled)
        self.converter = converter or ExcelPdfConverter()
        self.lock_timeout = float(lock_timeout)
        self.catalogue = catalogue

    def generate(self, enrollment, actor="system", *, force=False):
        """Generate or regenerate safely; never propagate a recoverable failure."""
        if not self.enabled:
            return None
        enrollment = self._enrollment(enrollment)
        export = MasterInvoiceExport.query.filter_by(
            enrollment_id=enrollment.id
        ).one_or_none()
        if export is None or not export.invoice_number:
            return self._mark_without_number(enrollment, "master_invoice", actor)
        if not enrollment.friendly_reference:
            year = (enrollment.confirmed_at or enrollment.created_at).year
            sequence = export.invoice_sequence or 1
            enrollment.friendly_reference = f"ENR-{year}-{sequence:06d}"
            db.session.commit()

        self.invoice_dir.mkdir(parents=True, exist_ok=True)
        try:
            with self._lock(export.invoice_number):
                record = self._record(enrollment, export.invoice_number)
                blocker = self._blocker(enrollment)
                if blocker:
                    self._mark(record, "needs_review", SAFE_REASONS[blocker], actor)
                    db.session.commit()
                    return record
                db.session.refresh(record)
                destination = self._destination(record.invoice_number)
                if (
                    not force
                    and record.status == "generated"
                    and record.storage_key
                    and destination.is_file()
                    and self._valid_existing(record, destination)
                ):
                    return record
                self._attempt(record)
                db.session.commit()
                return self._generate_locked(record, enrollment, destination, actor)
        except Timeout:
            record = self.for_enrollment(enrollment.id)
            if record:
                self._mark(record, "failed", "Invoice generation is busy. Please retry.", actor)
                db.session.commit()
            return record

    def retry(self, enrollment, actor="staff"):
        return self.generate(enrollment, actor, force=True)

    def get(self, generation_id):
        record = db.session.get(InvoiceGeneration, generation_id)
        if record is None:
            raise LookupError("Invoice document was not found")
        return record

    def for_enrollment(self, enrollment_id):
        return InvoiceGeneration.query.filter_by(
            enrollment_id=enrollment_id
        ).one_or_none()

    def document_bytes(self, record, actor="staff"):
        if not record.storage_key:
            raise LookupError("A generated invoice PDF is not available")
        path = self._path_for_key(record.storage_key)
        if not path.is_file() or not self._valid_existing(record, path):
            raise LookupError("The generated invoice PDF is unavailable")
        enrollment = db.session.get(EnrollmentDraft, record.enrollment_id)
        self._audit(enrollment, "template_invoice_downloaded", actor, version=record.version)
        return path.read_bytes()

    def serialize(self, record):
        if record is None:
            return None
        return {
            "id": record.id,
            "invoice_number": record.invoice_number,
            "status": record.status,
            "generated_at": _iso(record.generated_at),
            "last_attempted_at": _iso(record.last_attempted_at),
            "attempt_count": record.attempt_count,
            "failure_message": record.failure_reason,
            "version": record.version,
            "download_url": (
                f"/api/staff/invoice-documents/{record.id}/download"
                if record.storage_key and record.sha256
                else None
            ),
        }

    def _generate_locked(self, record, enrollment, destination, actor):
        old_bytes = destination.read_bytes() if destination.is_file() else None
        temp_destination = destination.with_suffix(".pdf.tmp")
        try:
            template_bytes = self._template_bytes()
            with tempfile.TemporaryDirectory(
                prefix="qm-invoice-", dir=str(self.storage_root)
            ) as directory:
                work_dir = Path(directory)
                workbook_path = work_dir / "invoice-working-copy.xlsx"
                pdf_path = work_dir / "invoice-output.pdf"
                workbook_path.write_bytes(template_bytes)
                self._populate(workbook_path, enrollment, record.invoice_number)
                self._verify_workbook(workbook_path, enrollment, record.invoice_number)
                self.converter.convert(workbook_path, pdf_path, self.sheet_name)
                content = pdf_path.read_bytes() if pdf_path.is_file() else b""
                self._validate_pdf(content, record.invoice_number, enrollment.full_name)
                temp_destination.write_bytes(content)
                os.replace(temp_destination, destination)
            record.storage_key = destination.relative_to(self.storage_root).as_posix()
            record.status = "generated"
            record.failure_reason = None
            record.sha256 = hashlib.sha256(content).hexdigest()
            record.version += 1
            record.generated_at = utc_now()
            self._audit(
                enrollment,
                "template_invoice_regenerated" if old_bytes else "template_invoice_generated",
                actor,
                version=record.version,
            )
            db.session.commit()
            return record
        except FileNotFoundError:
            return self._failed(record, enrollment, SAFE_REASONS["template"], actor)
        except ExcelUnavailableError:
            return self._failed(record, enrollment, SAFE_REASONS["excel"], actor)
        except Exception:
            return self._failed(record, enrollment, SAFE_REASONS["conversion"], actor)
        finally:
            temp_destination.unlink(missing_ok=True)

    def _failed(self, record, enrollment, reason, actor):
        record.status = "failed"
        record.failure_reason = reason
        self._audit(enrollment, "template_invoice_generation_failed", actor, reason=reason)
        db.session.commit()
        return record

    def _record(self, enrollment, invoice_number):
        existing = self.for_enrollment(enrollment.id)
        if existing:
            if existing.invoice_number != invoice_number:
                raise ValueError("The enrollment invoice number cannot be changed")
            return existing
        record = InvoiceGeneration(
            enrollment_id=enrollment.id,
            invoice_number=invoice_number,
            status="pending",
        )
        db.session.add(record)
        db.session.flush()
        return record

    def _mark_without_number(self, enrollment, reason, actor):
        # The invoice number is required by the non-null/unique database contract,
        # so no incomplete document record is invented before Master Invoice export.
        self._audit(enrollment, "template_invoice_generation_deferred", actor, reason=reason)
        db.session.commit()
        return None

    def _blocker(self, enrollment):
        if enrollment.status == "cancelled":
            return "cancelled"
        if enrollment.status not in ELIGIBLE_STATUSES or not enrollment.confirmed_at:
            return "incomplete"
        if not all(
            (
                enrollment.full_name,
                enrollment.nric,
                enrollment.email,
                enrollment.mobile_number,
                enrollment.course,
                enrollment.course_fee is not None,
                enrollment.payment_method,
                enrollment.skillsfuture_amount is not None,
                enrollment.paynow_amount is not None,
            )
        ):
            return "incomplete"
        if not (enrollment.intake_start_date or enrollment.preferred_intake_date):
            return "course_date"
        try:
            fee = _money(enrollment.course_fee)
            skillsfuture = _money(enrollment.skillsfuture_amount)
            paynow = _money(enrollment.paynow_amount)
        except ValueError:
            return "incomplete"
        if fee <= 0 or skillsfuture + paynow != fee:
            return "incomplete"
        if (
            "infection control" in str(enrollment.course).casefold()
            and (enrollment.payment_method != "paynow" or skillsfuture != 0)
        ):
            return "incomplete"
        if not self.approved:
            return "approval"
        return None

    def _populate(self, workbook_path, enrollment, invoice_number):
        # Build a clean runtime workbook instead of resaving the legacy source
        # package. The supplied attachment contains drawing relationships that
        # openpyxl cannot round-trip into a file Microsoft Excel will accept.
        # Company wording and the approved logo are still taken from the
        # supplied source materials; the source attachment is never modified.
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = self.sheet_name
        invoice_date = self._invoice_date(enrollment.id)
        intake_date = enrollment.intake_start_date or enrollment.preferred_intake_date
        intake_end = enrollment.intake_end_date or intake_date

        green = "007A64"
        pale = "EAF5F1"
        grey = "5B6B68"
        thin = Side(style="thin", color="C9D8D3")
        for column, width in {
            "A": 18, "B": 17, "C": 12, "D": 12, "E": 12, "F": 12,
            "G": 14, "H": 18, "I": 20, "J": 12, "K": 12,
        }.items():
            sheet.column_dimensions[column].width = width
        for row in range(1, 46):
            sheet.row_dimensions[row].height = 19
        sheet.row_dimensions[1].height = 34
        sheet.row_dimensions[2].height = 28
        sheet.merge_cells("A1:D3")
        logo_path = Path(__file__).resolve().parents[1] / "assets" / "email" / "qm-training-logo.png"
        if logo_path.is_file():
            logo = Image(_trim_logo(logo_path))
            logo.width = 110
            logo.height = 70
            sheet.add_image(logo, "A1")
        sheet.merge_cells("F1:K2")
        sheet["F1"] = "TAX / COURSE INVOICE"
        sheet["F1"].font = Font(name="Aptos Display", size=22, bold=True, color=green)
        sheet["F1"].alignment = Alignment(horizontal="right", vertical="center")
        sheet.merge_cells("F3:K3")
        sheet["F3"] = "Q & M College of Dentistry Pte. Ltd."
        sheet["F3"].font = Font(name="Aptos", size=10, bold=True, color=grey)
        sheet["F3"].alignment = Alignment(horizontal="right")

        sheet.merge_cells("A5:K5")
        sheet["A5"] = "COURSE ENROLLMENT INVOICE"
        _section(sheet["A5"], green)
        _label_value(sheet, 7, "A", "Invoice number:", "C", invoice_number, end="E")
        _label_value(sheet, 7, "G", "Invoice date:", "I", invoice_date, end="K", date_cell=True)
        _label_value(
            sheet, 9, "A", "Enrollment reference:", "C",
            enrollment.friendly_reference or enrollment.id, end="E"
        )
        _label_value(sheet, 9, "G", "Payment status:", "I", "Awaiting staff verification", end="K")

        sheet.merge_cells("A12:K12")
        sheet["A12"] = "BILL TO"
        _section(sheet["A12"], green)
        _label_value(sheet, 14, "A", "Participant name:", "C", enrollment.full_name, end="F")
        _label_value(sheet, 14, "G", "NRIC/FIN:", "I", mask_nric(enrollment.nric), end="K")
        _label_value(sheet, 16, "A", "Email:", "C", enrollment.email, end="F")
        _label_value(sheet, 16, "G", "Mobile:", "I", enrollment.mobile_number, end="K")

        sheet.merge_cells("A18:K18")
        sheet["A18"] = "COURSE DETAILS"
        _section(sheet["A18"], green)
        _label_value(sheet, 22, "A", "Course:", "C", enrollment.course, end="K")
        course = self.catalogue.course_for(enrollment.course_id or enrollment.course) if self.catalogue else None
        course_code = course.get("code") if course else None
        _label_value(sheet, 24, "A", "Course code:", "C", course_code, end="E")
        _label_value(sheet, 24, "G", "Training time:", "I", "9:30am–5:30pm", end="K")
        _label_value(sheet, 26, "A", "Training date(s):", "C", _date_range(intake_date, intake_end), end="E")
        _label_value(sheet, 26, "G", "Venue:", "I", "Confirmed with selected intake", end="K")

        sheet.merge_cells("A29:K29")
        sheet["A29"] = "PAYMENT SUMMARY"
        _section(sheet["A29"], green)
        payment_rows = (
            (30, "Course fee", _money(enrollment.course_fee)),
            (31, "SkillsFuture amount", _money(enrollment.skillsfuture_amount)),
            (33, "PayNow amount", _money(enrollment.paynow_amount)),
            (36, "Total", _money(enrollment.skillsfuture_amount) + _money(enrollment.paynow_amount)),
        )
        for row, label, amount in payment_rows:
            sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=7)
            sheet.cell(row, 1, label).font = Font(name="Aptos", size=10, bold=row == 36)
            sheet.merge_cells(start_row=row, start_column=8, end_row=row, end_column=11)
            amount_cell = sheet.cell(
                row,
                8,
                "=SUM(H31,H33)" if row == 36 else float(amount),
            )
            amount_cell.number_format = '"S$"#,##0.00'
            amount_cell.alignment = Alignment(horizontal="right")
            amount_cell.font = Font(name="Aptos", size=10, bold=row == 36)
            for cell in sheet[row][0:11]:
                cell.border = Border(bottom=thin)
                if row == 36:
                    cell.fill = PatternFill("solid", fgColor=pale)

        sheet.merge_cells("A39:K40")
        sheet["A39"] = (
            "Please retain this invoice for your records. For assistance, reply to "
            "your Q&M Training email or contact the course administration team."
        )
        sheet["A39"].alignment = Alignment(wrap_text=True, vertical="top")
        sheet["A39"].font = Font(name="Aptos", size=9, color=grey)
        sheet.merge_cells("A43:K43")
        sheet["A43"] = "Thank you for choosing Q&M Training."
        sheet["A43"].alignment = Alignment(horizontal="center")
        sheet["A43"].font = Font(name="Aptos", size=10, bold=True, color=green)

        # Deterministic verification values remain outside the print area.
        for row in range(46, 55):
            sheet.row_dimensions[row].hidden = True
        sheet["B46"] = _safe_excel_text(enrollment.full_name)
        sheet["B47"] = _safe_excel_text(mask_nric(enrollment.nric))
        sheet["I46"] = _safe_excel_text(invoice_number)
        sheet["I47"] = invoice_date
        sheet["A48"] = _safe_excel_text(enrollment.course)
        sheet["I48"] = float(_money(enrollment.course_fee))
        sheet["A49"] = intake_date
        sheet["I50"] = "=SUM(H31,H33)"
        sheet["I51"] = float(_money(enrollment.skillsfuture_amount))
        sheet["I52"] = float(_money(enrollment.paynow_amount))

        sheet.sheet_view.showGridLines = False
        sheet.print_area = "A1:K43"
        sheet.page_setup.orientation = "portrait"
        sheet.page_setup.paperSize = sheet.PAPERSIZE_A4
        sheet.page_setup.fitToWidth = 1
        sheet.page_setup.fitToHeight = 1
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
        sheet.print_options.horizontalCentered = True
        sheet.page_margins.left = 0.25
        sheet.page_margins.right = 0.25
        sheet.page_margins.top = 0.3
        sheet.page_margins.bottom = 0.3

        workbook.calculation.fullCalcOnLoad = True
        workbook.calculation.forceFullCalc = True
        workbook.save(workbook_path)

    def _verify_workbook(self, workbook_path, enrollment, invoice_number):
        workbook = load_workbook(workbook_path, data_only=False, read_only=False)
        try:
            sheet = workbook[self.sheet_name]
            expected = (
                invoice_number,
                enrollment.full_name,
                enrollment.course,
                mask_nric(enrollment.nric),
            )
            visible = " ".join(
                str(cell.value) for row in sheet.iter_rows(min_row=1, max_row=43)
                for cell in row if cell.value is not None
            )
            if any(value not in visible for value in expected):
                raise ValueError("Runtime invoice workbook verification failed")
            if "#######" in visible or sheet.print_area != "'Invoice'!$A$1:$K$43":
                raise ValueError("Runtime invoice print layout is invalid")
            if sheet.page_setup.fitToWidth != 1 or sheet.page_setup.fitToHeight != 1:
                raise ValueError("Runtime invoice is not configured as one page")
        finally:
            workbook.close()

    def _invoice_date(self, enrollment_id):
        export = MasterInvoiceExport.query.filter_by(
            enrollment_id=enrollment_id
        ).one()
        if export.invoice_date is None:
            raise ValueError("Master Invoice date is unavailable")
        return export.invoice_date

    def _template_bytes(self):
        if not self.source_path or not self.source_path.is_file():
            raise FileNotFoundError
        if self.source_path.suffix.casefold() in {".xlsx", ".xlsm"}:
            return self.source_path.read_bytes()
        if self.source_path.suffix.casefold() != ".eml":
            raise FileNotFoundError
        message = email.message_from_bytes(self.source_path.read_bytes())
        for part in message.walk():
            if part.get_filename() == self.attachment_name:
                content = part.get_payload(decode=True)
                if content:
                    return content
        raise FileNotFoundError

    def _validate_pdf(self, content, invoice_number, participant_name):
        if not content.startswith(b"%PDF-") or len(content) < 100:
            raise ValueError("Converter did not produce a valid PDF")
        try:
            try:
                from pypdf import PdfReader
            except ImportError:
                from PyPDF2 import PdfReader
            reader = PdfReader(BytesIO(content))
            if len(reader.pages) != 1:
                raise ValueError("Generated invoice PDF must contain exactly one page")
            box = reader.pages[0].mediabox
            width = float(box.width)
            height = float(box.height)
            if not (590 <= width <= 600 and 837 <= height <= 847):
                raise ValueError("Generated invoice PDF must use A4 portrait paper")
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception as error:
            raise ValueError("Generated PDF could not be opened") from error
        if invoice_number not in text or participant_name not in text or "#######" in text:
            raise ValueError("Generated PDF content could not be verified")

    def _valid_existing(self, record, path):
        try:
            content = path.read_bytes()
            return (
                bool(record.sha256)
                and hashlib.sha256(content).hexdigest() == record.sha256
                and content.startswith(b"%PDF-")
            )
        except OSError:
            return False

    def _destination(self, invoice_number):
        filename = _safe_filename(invoice_number) + ".pdf"
        result = (self.invoice_dir / filename).resolve()
        root = self.invoice_dir.resolve()
        if root not in result.parents:
            raise ValueError("Unsafe invoice filename")
        return result

    def _path_for_key(self, storage_key):
        result = (self.storage_root / storage_key).resolve()
        root = self.storage_root.resolve()
        if root not in result.parents:
            raise LookupError("Invoice document path is invalid")
        return result

    @contextmanager
    def _lock(self, invoice_number):
        lock_path = self.invoice_dir / (_safe_filename(invoice_number) + ".lock")
        with FileLock(str(lock_path), timeout=self.lock_timeout):
            yield

    def _attempt(self, record):
        record.status = "pending"
        record.attempt_count += 1
        record.last_attempted_at = utc_now()
        record.failure_reason = None

    def _mark(self, record, status, reason, actor):
        record.status = status
        record.attempt_count += 1
        record.last_attempted_at = utc_now()
        record.failure_reason = reason
        self._audit(
            record.enrollment,
            "template_invoice_needs_review" if status == "needs_review" else "template_invoice_generation_failed",
            actor,
            reason=reason,
        )

    @staticmethod
    def _audit(enrollment, action, actor, **details):
        import json

        db.session.add(
            FinanceAudit(
                enrollment_id=enrollment.id,
                action=action,
                actor=actor,
                details_json=json.dumps(details, sort_keys=True),
            )
        )

    @staticmethod
    def _enrollment(value):
        if isinstance(value, EnrollmentDraft):
            return value
        enrollment = db.session.get(EnrollmentDraft, value)
        if enrollment is None:
            raise LookupError("Enrollment not found")
        return enrollment


def _money(value):
    try:
        result = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError) as error:
        raise ValueError("Invalid money value") from error
    if not result.is_finite() or result < 0:
        raise ValueError("Invalid money value")
    return result


def _section(cell, colour):
    cell.fill = PatternFill("solid", fgColor=colour)
    cell.font = Font(name="Aptos", size=11, bold=True, color="FFFFFF")
    cell.alignment = Alignment(vertical="center")


def _label_value(
    sheet, row, label_column, label, value_column, value, *, end, date_cell=False
):
    label_cell = sheet[f"{label_column}{row}"]
    label_cell.value = label
    label_cell.font = Font(name="Aptos", size=9, bold=True, color="5B6B68")
    start_index = sheet[f"{value_column}{row}"].column
    end_index = sheet[f"{end}{row}"].column
    if end_index > start_index:
        sheet.merge_cells(
            start_row=row,
            start_column=start_index,
            end_row=row,
            end_column=end_index,
        )
    value_cell = sheet.cell(row=row, column=start_index, value=_safe_excel_text(value) if not date_cell else value)
    value_cell.font = Font(name="Aptos", size=10)
    value_cell.alignment = Alignment(vertical="center", wrap_text=True)
    if date_cell:
        value_cell.number_format = "dd mmmm yyyy"


def _date_range(start, end):
    if start == end:
        return f"{start.day} {start.strftime('%B %Y')}"
    if start.year == end.year and start.month == end.month:
        return f"{start.day}–{end.day} {start.strftime('%B %Y')}"
    if start.year == end.year:
        return f"{start.day} {start.strftime('%B')}–{end.day} {end.strftime('%B %Y')}"
    return f"{start.day} {start.strftime('%B %Y')}–{end.day} {end.strftime('%B %Y')}"


def _safe_excel_text(value):
    result = str(value or "").strip()
    if result[:1] in {"=", "+", "-", "@", "\t", "\r", "\n"}:
        result = "'" + result
    return result


def _trim_logo(path):
    """Return an in-memory PNG with only the logo's meaningful white-bounded area."""
    from PIL import Image as PillowImage, ImageChops

    with PillowImage.open(path).convert("RGB") as source:
        background = PillowImage.new("RGB", source.size, "white")
        difference = ImageChops.difference(source, background)
        bounds = difference.getbbox()
        if bounds:
            left, top, right, bottom = bounds
            padding = 6
            bounds = (
                max(0, left - padding),
                max(0, top - padding),
                min(source.width, right + padding),
                min(source.height, bottom + padding),
            )
            source = source.crop(bounds)
        output = BytesIO()
        source.save(output, format="PNG", optimize=True)
        output.seek(0)
        return output


def _normalise_pdf_to_a4(pdf_path):
    """Preserve Excel's rendered page while normalising printer-dependent paper size."""
    try:
        from pypdf import PdfReader, PdfWriter, Transformation
    except ImportError:  # pragma: no cover - legacy dependency fallback
        from PyPDF2 import PdfReader, PdfWriter, Transformation

    a4_width = 595.2756
    a4_height = 841.8898
    reader = PdfReader(str(pdf_path))
    if len(reader.pages) != 1:
        return
    source = reader.pages[0]
    width = float(source.mediabox.width)
    height = float(source.mediabox.height)
    if abs(width - a4_width) <= 1 and abs(height - a4_height) <= 1:
        return
    scale = min(a4_width / width, a4_height / height)
    offset_x = (a4_width - width * scale) / 2
    offset_y = (a4_height - height * scale) / 2
    writer = PdfWriter()
    target = writer.add_blank_page(width=a4_width, height=a4_height)
    target.merge_transformed_page(
        source,
        Transformation().scale(scale).translate(offset_x, offset_y),
    )
    temporary = Path(pdf_path).with_suffix(".a4.tmp")
    try:
        with temporary.open("wb") as output:
            writer.write(output)
        os.replace(temporary, pdf_path)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_filename(value):
    result = re.sub(r"[^A-Za-z0-9._-]", "_", str(value or ""))
    if not result or result in {".", ".."}:
        raise ValueError("Invalid invoice number")
    return result[:120]


def _iso(value):
    return value.isoformat() if value else None


__all__ = [
    "ExcelPdfConverter",
    "ExcelUnavailableError",
    "InvoiceTemplateService",
]
