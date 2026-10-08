"""Private, deterministic and recoverable Master Invoice Excel export."""

import hashlib
import json
import os
import re
import tempfile
from decimal import Decimal
from io import BytesIO
from pathlib import Path

from filelock import FileLock, Timeout
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.table import Table, TableStyleInfo
from openpyxl.utils import get_column_letter

from backend.extensions import db
from backend.models import EnrollmentDraft, utc_now
from backend.models.finance import Invoice, MasterInvoiceAudit, MasterInvoiceExport
from backend.models.operations import CourseDate
from backend.services.enrollment.validation import validate_field


SHEET_NAME = "Master Invoice List"
WORKBOOK_NAME = "Master_Invoice_List.xlsx"
TABLE_NAME = "MasterInvoiceTable"
EXPORTABLE_STATUSES = {
    "confirmed",
    "awaiting_course_date",
    "enrolled",
    "invoice_sent",
    "awaiting_payment",
    "paid",
    "receipt_issued",
}
HEADERS = (
    "No.",
    "Invoice No.",
    "Course Code",
    "Course Name",
    "Lecture Start Date",
    "Lecture End Date",
    "Student Name",
    "Amount Invoiced",
    "NRIC/FIN",
    "Invoice Date",
    "DCR No.",
    "Email",
    "Invoice Remark",
    "Handphone No.",
    "Date of Birth",
    "Private Note to Accounts",
    "Payment Method",
    "SkillsFuture Amount",
    "PayNow Amount",
    "Payment Total",
    "Enrollment Status",
    "Excel Export Status",
    "Enrollment ID",
    "Intake ID",
    "Demo Intake",
    "Last Updated",
)


class MasterInvoiceService:
    """Build one protected workbook from validated database enrollments."""

    def __init__(
        self,
        workbook_path,
        template_path="",
        lock_timeout=10,
        catalogue=None,
        invoice_prefix="DEMO-INV",
        validation_options=None,
    ):
        self.workbook_path = Path(workbook_path)
        self.template_path = Path(template_path) if template_path else None
        self.lock_timeout = float(lock_timeout)
        self.catalogue = catalogue
        self.invoice_prefix = _safe_prefix(invoice_prefix)
        self.validation_options = dict(validation_options or {})

    def queue_and_export(self, enrollment, actor="system"):
        """Compatibility wrapper for the automatic confirmation hook."""
        enrollment_id = (
            enrollment.id if isinstance(enrollment, EnrollmentDraft) else enrollment
        )
        return self.export_confirmed_enrollment(enrollment_id, actor)

    def export_confirmed_enrollment(self, enrollment_id, actor="system"):
        enrollment = db.session.get(EnrollmentDraft, enrollment_id)
        if enrollment is None or self.validation_errors(enrollment):
            return False
        try:
            with self._lock():
                record = self._get_or_create_record(enrollment)
                content_hash = self._content_hash(enrollment, record)
                if (
                    record.status == "exported"
                    and record.content_hash == content_hash
                    and self.workbook_path.is_file()
                ):
                    try:
                        self.validate_workbook()
                        return True
                    except ValueError:
                        pass
                record.status = (
                    "needs_update" if record.exported_at is not None else "pending"
                )
                record.last_error = None
                db.session.commit()
                return self._safe_rebuild_locked(actor, "automatic")
        except Timeout:
            self._mark_failed(actor, "automatic")
            return False

    def update_exported_enrollment(self, enrollment_id, actor="system"):
        enrollment = db.session.get(EnrollmentDraft, enrollment_id)
        record = MasterInvoiceExport.query.filter_by(
            enrollment_id=enrollment_id
        ).one_or_none()
        if enrollment is None:
            return False
        if record is None:
            return self.export_confirmed_enrollment(enrollment_id, actor)
        record.status = "needs_update"
        record.last_error = None
        db.session.commit()
        if enrollment.status != "cancelled" and self.validation_errors(enrollment):
            return False
        return self._run_rebuild(actor, "update")

    def mark_needs_update(self, enrollment_id):
        record = MasterInvoiceExport.query.filter_by(
            enrollment_id=enrollment_id
        ).one_or_none()
        if record is None:
            return False
        record.status = "needs_update"
        record.last_error = None
        record.workbook_row = None
        db.session.commit()
        return True

    def retry_failed_export(self, enrollment_id, actor):
        record = MasterInvoiceExport.query.filter_by(
            enrollment_id=enrollment_id
        ).one_or_none()
        if record is None:
            raise LookupError("Master Invoice export record was not found")
        if record.status not in {"failed", "pending", "needs_update"}:
            return True
        record.status = "pending"
        record.last_error = None
        db.session.commit()
        return self._run_rebuild(actor, "retry")

    def regenerate(self, actor):
        self.rebuild_master_invoice_workbook(actor)
        return self.status()

    def rebuild_master_invoice_workbook(self, actor):
        return self._run_rebuild(actor, "regenerate")

    def retry_failed(self, actor):
        records = db.session.execute(
            db.select(MasterInvoiceExport).where(
                MasterInvoiceExport.status.in_(
                    ("pending", "failed", "needs_update")
                )
            )
        ).scalars()
        for record in records:
            record.status = "pending"
            record.last_error = None
        db.session.commit()
        return self._run_rebuild(actor, "retry")

    def status(self):
        eligible = self._eligible_enrollments()
        pending = MasterInvoiceExport.query.filter_by(status="pending").count()
        failed = MasterInvoiceExport.query.filter_by(status="failed").count()
        needs_update = MasterInvoiceExport.query.filter_by(
            status="needs_update"
        ).count()
        latest = db.session.execute(
            db.select(MasterInvoiceExport.exported_at)
            .where(MasterInvoiceExport.exported_at.is_not(None))
            .order_by(MasterInvoiceExport.exported_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        records = MasterInvoiceExport.query.order_by(
            MasterInvoiceExport.invoice_sequence.asc().nullslast(),
            MasterInvoiceExport.created_at.asc(),
        ).all()
        return {
            "filename": WORKBOOK_NAME,
            "last_generated_at": _iso(latest),
            "confirmed_records": len(eligible),
            "pending_exports": pending,
            "failed_exports": failed,
            "needs_update": needs_update,
            "pending_or_failed": pending + failed + needs_update,
            "download_available": self.workbook_path.is_file(),
            "records": [self._status_record(record) for record in records],
        }

    def download(self, actor):
        if not self.workbook_path.is_file():
            raise LookupError("The Master Invoice List has not been generated yet")
        try:
            with self._lock():
                self.validate_workbook()
                content = self.workbook_path.read_bytes()
        except Timeout as error:
            raise ValueError(
                "The workbook is currently in use. Please try again shortly."
            ) from error
        db.session.add(
            MasterInvoiceAudit(
                action="download",
                actor=actor,
                record_count=len(self._workbook_enrollments()),
            )
        )
        db.session.commit()
        return BytesIO(content)

    def validate_workbook(self, path=None):
        selected = Path(path) if path else self.workbook_path
        if not selected.is_file():
            raise ValueError("The Master Invoice workbook is unavailable.")
        workbook = None
        try:
            workbook = load_workbook(selected, read_only=True, data_only=False)
            if SHEET_NAME not in workbook.sheetnames:
                raise ValueError("The required Master Invoice List sheet is missing.")
            worksheet = workbook[SHEET_NAME]
            headers = tuple(
                worksheet.cell(row=1, column=column).value
                for column in range(1, len(HEADERS) + 1)
            )
            if headers != HEADERS:
                raise ValueError("The Master Invoice List headers are invalid.")
            enrollment_ids = [
                worksheet.cell(row=row, column=23).value
                for row in range(2, worksheet.max_row + 1)
                if worksheet.cell(row=row, column=23).value
            ]
            if len(enrollment_ids) != len(set(enrollment_ids)):
                raise ValueError("The Master Invoice List contains duplicate rows.")
        except ValueError:
            raise
        except Exception as error:
            raise ValueError("The Master Invoice workbook cannot be opened.") from error
        finally:
            if workbook is not None:
                workbook.close()
        return True

    def validation_errors(self, enrollment):
        errors = []
        if (
            enrollment.confirmed_at is None
            or enrollment.status not in EXPORTABLE_STATUSES
        ):
            errors.append("The enrollment has not been successfully confirmed.")
        if self.catalogue is None:
            errors.append("Approved course information is unavailable.")
            return errors
        if enrollment.validation_errors or enrollment.pending_corrections:
            errors.append("The enrollment still contains validation corrections.")
        if enrollment.validation_status != "valid":
            errors.append("Final deterministic validation has not succeeded.")

        for field in (
            "course",
            "full_name",
            "nric",
            "date_of_birth",
            "email",
            "mobile_number",
        ):
            _, error = validate_field(
                field,
                getattr(enrollment, field),
                self.catalogue,
                course_value=enrollment.course,
                **self.validation_options,
            )
            if error:
                errors.append(error)

        fee = Decimal(str(enrollment.course_fee or 0))
        selected_course = self.catalogue.course_for(enrollment.course_id or enrollment.course)
        approved_fee = Decimal(str(self.catalogue.course_fee_for(selected_course) or 0))
        if fee != approved_fee:
            errors.append("The persisted course fee does not match the selected course.")
        if enrollment.intake_id:
            intake = db.session.get(CourseDate, enrollment.intake_id)
            if intake is None:
                errors.append("The selected intake record does not exist.")
            elif selected_course is None or intake.course_id != selected_course.get("id"):
                errors.append("The selected intake does not belong to the selected course.")
        if enrollment.status == "awaiting_course_date":
            if enrollment.preferred_intake_date is not None:
                errors.append("The awaiting-course-date state is inconsistent.")
        elif not enrollment.intake_id or enrollment.preferred_intake_date is None:
            errors.append("A confirmed enrollment requires a selected intake.")
        else:
            intake = db.session.get(CourseDate, enrollment.intake_id)
            if intake is None or intake.date != enrollment.preferred_intake_date:
                errors.append("The selected intake does not match the course schedule.")

        if enrollment.nric and enrollment.course and enrollment.intake_id:
            duplicate = EnrollmentDraft.query.filter(
                EnrollmentDraft.id != enrollment.id,
                EnrollmentDraft.nric == enrollment.nric,
                EnrollmentDraft.course == enrollment.course,
                EnrollmentDraft.intake_id == enrollment.intake_id,
                EnrollmentDraft.confirmed_at.is_not(None),
                EnrollmentDraft.status != "cancelled",
            ).first()
            if duplicate is not None:
                errors.append("A duplicate active enrollment exists for this intake.")
        return list(dict.fromkeys(errors))

    def _run_rebuild(self, actor, action):
        try:
            with self._lock():
                return self._safe_rebuild_locked(actor, action)
        except Timeout:
            self._mark_failed(actor, action)
            return False

    def _safe_rebuild_locked(self, actor, action):
        try:
            self._rebuild_locked(actor, action)
            return True
        except Exception:
            db.session.rollback()
            self._mark_failed(actor, action)
            return False

    def _rebuild_locked(self, actor, action):
        enrollments = self._workbook_enrollments()
        self._ensure_tracking(enrollments)
        records = self._records_for(enrollments)
        self._ensure_invoice_metadata(enrollments, records)
        attempted_at = utc_now()
        for record in records.values():
            record.last_attempted_at = attempted_at
            if record.status != "exported":
                record.status = "pending"
        db.session.commit()

        ordered = sorted(
            enrollments,
            key=lambda enrollment: (
                records[enrollment.id].invoice_sequence or 0,
                enrollment.id,
            ),
        )
        workbook = self._new_workbook()
        worksheet = self._prepare_sheet(workbook)
        invoices = self._invoices(ordered)
        for number, enrollment in enumerate(ordered, start=1):
            self._write_row(
                worksheet,
                number + 1,
                number,
                enrollment,
                records[enrollment.id],
                invoices.get(enrollment.id),
            )
        self._format_sheet(worksheet, len(ordered))
        self._atomic_save(workbook)

        exported_at = utc_now()
        included_ids = {enrollment.id for enrollment in ordered}
        for row_number, enrollment in enumerate(ordered, start=2):
            record = records[enrollment.id]
            content_hash = self._content_hash(enrollment, record)
            if content_hash != record.content_hash:
                record.export_version = (record.export_version or 0) + 1
            record.content_hash = content_hash
            record.workbook_row = row_number
            record.status = "exported"
            record.attempts = (record.attempts or 0) + 1
            record.last_error = None
            record.exported_at = exported_at
        excluded = MasterInvoiceExport.query
        if included_ids:
            excluded = excluded.filter(
                MasterInvoiceExport.enrollment_id.notin_(included_ids)
            )
        for record in excluded.all():
            enrollment = db.session.get(EnrollmentDraft, record.enrollment_id)
            if enrollment is not None and enrollment.status == "cancelled":
                record.status = "removed_after_cancellation"
            elif record.status == "exported":
                record.status = "needs_update"
            record.workbook_row = None
        db.session.add(
            MasterInvoiceAudit(action=action, actor=actor, record_count=len(ordered))
        )
        db.session.commit()

    def _mark_failed(self, actor, action):
        db.session.rollback()
        attempted_at = utc_now()
        records = db.session.execute(
            db.select(MasterInvoiceExport).where(
                MasterInvoiceExport.status.in_(
                    ("pending", "failed", "needs_update")
                )
            )
        ).scalars()
        for record in records:
            record.status = "failed"
            record.attempts = (record.attempts or 0) + 1
            record.last_attempted_at = attempted_at
            record.last_error = (
                "Workbook generation failed or the file is in use. Retry is available."
            )
        db.session.add(
            MasterInvoiceAudit(
                action=f"{action}_failed",
                actor=actor,
                record_count=len(self._eligible_enrollments()),
            )
        )
        db.session.commit()

    def _get_or_create_record(self, enrollment):
        record = MasterInvoiceExport.query.filter_by(
            enrollment_id=enrollment.id
        ).one_or_none()
        if record is None:
            record = MasterInvoiceExport(
                enrollment_id=enrollment.id,
                export_key=enrollment.id,
                status="pending",
            )
            db.session.add(record)
            db.session.flush()
        elif record.export_key is None:
            record.export_key = enrollment.id
        return record

    def _ensure_tracking(self, enrollments):
        for enrollment in enrollments:
            self._get_or_create_record(enrollment)
        db.session.flush()

    @staticmethod
    def _records_for(enrollments):
        ids = [enrollment.id for enrollment in enrollments]
        if not ids:
            return {}
        rows = db.session.execute(
            db.select(MasterInvoiceExport).where(
                MasterInvoiceExport.enrollment_id.in_(ids)
            )
        ).scalars()
        return {row.enrollment_id: row for row in rows}

    def _ensure_invoice_metadata(self, enrollments, records):
        highest = db.session.execute(
            db.select(db.func.max(MasterInvoiceExport.invoice_sequence))
        ).scalar() or 0
        invoices = self._invoices(enrollments)
        for enrollment in sorted(
            enrollments,
            key=lambda row: (row.confirmed_at or row.created_at, row.id),
        ):
            record = records[enrollment.id]
            if record.invoice_sequence is None:
                highest += 1
                record.invoice_sequence = highest
            invoice = invoices.get(enrollment.id)
            if record.invoice_number is None:
                record.invoice_number = (
                    invoice.number
                    if invoice is not None
                    else f"{self.invoice_prefix}-{self._invoice_year(enrollment)}-"
                    f"{record.invoice_sequence:06d}"
                )
            if record.invoice_date is None:
                record.invoice_date = (
                    invoice.created_at.date()
                    if invoice is not None
                    else (enrollment.confirmed_at or enrollment.created_at).date()
                )
        db.session.flush()

    @staticmethod
    def _invoice_year(enrollment):
        return (enrollment.confirmed_at or enrollment.created_at).year

    def _eligible_enrollments(self):
        candidates = EnrollmentDraft.query.filter(
            EnrollmentDraft.confirmed_at.is_not(None),
            EnrollmentDraft.status != "cancelled",
        ).order_by(EnrollmentDraft.confirmed_at.asc(), EnrollmentDraft.id.asc())
        return [row for row in candidates.all() if not self.validation_errors(row)]

    def _workbook_enrollments(self):
        return self._eligible_enrollments()

    @staticmethod
    def _invoices(enrollments):
        ids = [enrollment.id for enrollment in enrollments]
        if not ids:
            return {}
        rows = db.session.execute(
            db.select(Invoice).where(Invoice.enrollment_id.in_(ids))
        ).scalars()
        return {row.enrollment_id: row for row in rows}

    def _new_workbook(self):
        if self.template_path and self.template_path.is_file():
            candidate = load_workbook(self.template_path)
            if SHEET_NAME in candidate.sheetnames:
                for worksheet in list(candidate.worksheets):
                    if worksheet.title != SHEET_NAME:
                        candidate.remove(worksheet)
                return candidate
            candidate.close()
        workbook = Workbook()
        workbook.active.title = SHEET_NAME
        return workbook

    @staticmethod
    def _prepare_sheet(workbook):
        worksheet = workbook[SHEET_NAME]
        for row in worksheet.iter_rows(min_row=2):
            for cell in row:
                if cell.data_type != "f":
                    cell.value = None
        for column, header in enumerate(HEADERS, start=1):
            worksheet.cell(row=1, column=column, value=header)
        return worksheet

    def _write_row(
        self, worksheet, row_number, number, enrollment, record, invoice
    ):
        skillsfuture = (
            Decimal(str(enrollment.skillsfuture_amount))
            if enrollment.skillsfuture_amount is not None
            else None
        )
        paynow = (
            Decimal(str(enrollment.paynow_amount))
            if enrollment.paynow_amount is not None
            else None
        )
        payment_total = (
            (skillsfuture or Decimal("0")) + (paynow or Decimal("0"))
            if skillsfuture is not None or paynow is not None
            else None
        )
        values = (
            number,
            self._safe_text(record.invoice_number),
            self._safe_text(self._course_code(enrollment)),
            self._safe_text(enrollment.course),
            enrollment.intake_start_date or enrollment.preferred_intake_date,
            enrollment.intake_end_date or enrollment.preferred_intake_date,
            self._safe_text(enrollment.full_name),
            Decimal(str(invoice.course_fee if invoice else enrollment.course_fee)),
            self._safe_text(enrollment.nric),
            record.invoice_date,
            None,
            self._safe_text(enrollment.email),
            None,
            self._safe_text(enrollment.mobile_number),
            enrollment.date_of_birth,
            (
                "Payment/refund review required"
                if enrollment.refund_review_required
                else None
            ),
            self._safe_text(_payment_method_label(enrollment.payment_method)),
            skillsfuture,
            paynow,
            payment_total,
            self._safe_text(enrollment.status.replace("_", " ").title()),
            "Exported successfully",
            enrollment.id,
            self._safe_text(enrollment.intake_id),
            "Yes" if enrollment.intake_is_demo else (
                "No" if enrollment.intake_id else None
            ),
            utc_now().replace(tzinfo=None),
        )
        for column, value in enumerate(values, start=1):
            worksheet.cell(row=row_number, column=column, value=value)

    def _course_code(self, enrollment):
        if self.catalogue is None:
            return None
        course = self.catalogue.course_for(enrollment.course_id or enrollment.course)
        return course.get("code") if course else None

    @staticmethod
    def _safe_text(value):
        if value is None:
            return None
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value))
        if text.startswith(("=", "+", "-", "@")):
            return "'" + text
        return text

    @staticmethod
    def _format_sheet(worksheet, data_rows):
        worksheet.freeze_panes = "A2"
        final_row = data_rows + 1
        table_ref = f"A1:{get_column_letter(len(HEADERS))}{final_row}"
        worksheet.auto_filter.ref = table_ref
        widths = {
            "A": 7, "B": 24, "C": 15, "D": 38, "E": 18, "F": 18,
            "G": 28, "H": 18, "I": 16, "J": 16, "K": 15, "L": 32,
            "M": 25, "N": 18, "O": 16, "P": 28, "Q": 24, "R": 19,
            "S": 17, "T": 17, "U": 22, "V": 22, "W": 38, "X": 24,
            "Y": 14, "Z": 22,
        }
        for cell in worksheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill(fill_type="solid", fgColor="548235")
            cell.alignment = Alignment(wrap_text=True, vertical="top")
        for column, width in widths.items():
            worksheet.column_dimensions[column].width = width
        for row in range(2, final_row + 1):
            for column in (5, 6, 10, 15):
                worksheet.cell(row=row, column=column).number_format = "dd-mmm-yyyy"
            worksheet.cell(row=row, column=26).number_format = "dd-mmm-yyyy hh:mm"
            for column in (8, 18, 19, 20):
                worksheet.cell(row=row, column=column).number_format = '"S$"#,##0.00'
            for column in (13, 16):
                worksheet.cell(row=row, column=column).alignment = Alignment(
                    wrap_text=True, vertical="top"
                )
        if TABLE_NAME in worksheet.tables:
            del worksheet.tables[TABLE_NAME]
        if data_rows:
            table = Table(displayName=TABLE_NAME, ref=table_ref)
            table.tableStyleInfo = TableStyleInfo(
                name="TableStyleMedium4",
                showFirstColumn=False,
                showLastColumn=False,
                showRowStripes=True,
                showColumnStripes=False,
            )
            worksheet.add_table(table)

    def _atomic_save(self, workbook):
        temporary = None
        self.workbook_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.NamedTemporaryFile(
                prefix="master-invoice-",
                suffix=".xlsx",
                dir=self.workbook_path.parent,
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
            workbook.save(temporary)
            self.validate_workbook(temporary)
            os.replace(temporary, self.workbook_path)
        finally:
            workbook.close()
            if temporary and temporary.exists():
                temporary.unlink(missing_ok=True)

    def _lock(self):
        self.workbook_path.parent.mkdir(parents=True, exist_ok=True)
        return FileLock(str(self.workbook_path) + ".lock", timeout=self.lock_timeout)

    def _content_hash(self, enrollment, record):
        payload = {
            "enrollment_id": enrollment.id,
            "invoice_number": record.invoice_number,
            "invoice_date": _date_value(record.invoice_date),
            "course": enrollment.course,
            "course_fee": str(enrollment.course_fee),
            "full_name": enrollment.full_name,
            "nric": enrollment.nric,
            "date_of_birth": _date_value(enrollment.date_of_birth),
            "email": enrollment.email,
            "mobile_number": enrollment.mobile_number,
            "intake_id": enrollment.intake_id,
            "intake_start": _date_value(
                enrollment.intake_start_date or enrollment.preferred_intake_date
            ),
            "intake_end": _date_value(
                enrollment.intake_end_date or enrollment.preferred_intake_date
            ),
            "payment_method": enrollment.payment_method,
            "skillsfuture_amount": (
                str(enrollment.skillsfuture_amount)
                if enrollment.skillsfuture_amount is not None
                else None
            ),
            "paynow_amount": (
                str(enrollment.paynow_amount)
                if enrollment.paynow_amount is not None
                else None
            ),
            "status": enrollment.status,
            "cancelled_at": _iso(enrollment.cancelled_at),
            "rescheduled_at": _iso(enrollment.rescheduled_at),
            "previous_intake_id": enrollment.previous_intake_id,
            "refund_review_required": bool(enrollment.refund_review_required),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _status_record(self, record):
        enrollment = record.enrollment
        return {
            "enrollment_id": record.enrollment_id,
            "invoice_number": record.invoice_number,
            "participant_name": enrollment.full_name if enrollment else None,
            "selected_intake": (
                _date_value(
                    enrollment.intake_start_date
                    or enrollment.preferred_intake_date
                )
                if enrollment
                else None
            ),
            "skillsfuture_amount": (
                str(enrollment.skillsfuture_amount or 0) if enrollment else None
            ),
            "paynow_amount": (
                str(enrollment.paynow_amount or 0) if enrollment else None
            ),
            "enrollment_status": enrollment.status if enrollment else None,
            "cancelled_at": _iso(enrollment.cancelled_at) if enrollment else None,
            "rescheduled_at": _iso(enrollment.rescheduled_at) if enrollment else None,
            "refund_review_required": (
                bool(enrollment.refund_review_required) if enrollment else False
            ),
            "status": record.status,
            "exported_at": _iso(record.exported_at),
            "updated_at": _iso(record.updated_at),
            "retry_available": record.status in {
                "failed",
                "pending",
                "needs_update",
            },
            "failure_message": record.last_error,
        }


def _payment_method_label(value):
    return {
        "paynow": "PayNow",
        "skillsfuture_paynow": "SkillsFuture Credit + PayNow",
        "utap": "UTAP reimbursement (paid via PayNow)",
    }.get(value, value or "")


def _safe_prefix(value):
    prefix = re.sub(r"[^A-Z0-9-]", "-", str(value or "").upper()).strip("-")
    return prefix or "DEMO-INV"


def _date_value(value):
    return value.isoformat() if value else None


def _iso(value):
    return value.isoformat().replace("+00:00", "Z") if value else None


__all__ = [
    "HEADERS",
    "MasterInvoiceService",
    "SHEET_NAME",
    "TABLE_NAME",
    "WORKBOOK_NAME",
]
