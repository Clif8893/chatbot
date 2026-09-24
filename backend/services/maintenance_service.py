"""Guarded local-development cleanup for enrollment test data."""

from __future__ import annotations

import shutil
import sqlite3
import re
from datetime import datetime
from pathlib import Path

from sqlalchemy import text

from backend.extensions import db


CONFIRMATION_PHRASE = "CLEAR-ALL-TEST-ENROLLMENTS"


class MaintenanceService:
    def __init__(self, master_invoice, storage_dir, production=False):
        self.master_invoice = master_invoice
        self.storage_dir = Path(storage_dir).resolve()
        self.production = bool(production)

    def dry_run(self):
        enrollment_count = self._count("enrollment_drafts")
        conversation_count = db.session.execute(
            text(
                "SELECT COUNT(DISTINCT conversation_id) FROM enrollment_drafts "
                "WHERE conversation_id IS NOT NULL"
            )
        ).scalar_one()
        tables = (
            "outbound_messages",
            "invoice_generations",
            "master_invoice_exports",
            "financial_documents",
            "payment_evidence",
            "payment_reviews",
            "payments",
            "invoices",
            "credit_note_requests",
            "finance_cases",
            "finance_audit",
            "enrollment_drafts",
        )
        counts = {table: self._count(table) for table in tables}
        counts["enrollment_conversations"] = conversation_count
        counts["messages"] = db.session.execute(
            text(
                "SELECT COUNT(*) FROM messages WHERE conversation_id IN "
                "(SELECT conversation_id FROM enrollment_drafts)"
            )
        ).scalar_one()
        counts["generated_invoice_pdfs"] = len(
            list((self.storage_dir / "invoices").glob("*.pdf"))
        ) if (self.storage_dir / "invoices").is_dir() else 0
        counts["private_runtime_artifacts"] = len(
            [
                path for path in self.storage_dir.iterdir()
                if path.is_file() and re.fullmatch(
                    r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", path.name, re.I
                )
            ]
        ) if self.storage_dir.is_dir() else 0
        counts["users_preserved"] = self._count("users")
        counts["course_dates_preserved"] = self._count("course_dates")
        counts["enrollment_total"] = enrollment_count
        return counts

    def clear(self, confirmation):
        if self.production:
            raise RuntimeError("Test enrollment cleanup is disabled in production.")
        if confirmation != CONFIRMATION_PHRASE:
            raise ValueError(
                f"Refusing cleanup without --confirm {CONFIRMATION_PHRASE}."
            )
        before = self.dry_run()
        backup = self._backup()
        db.session.execute(text("DROP TABLE IF EXISTS temp.cleanup_conversations"))
        db.session.execute(
            text(
                "CREATE TEMP TABLE cleanup_conversations AS SELECT DISTINCT "
                "conversation_id AS id FROM enrollment_drafts "
                "WHERE conversation_id IS NOT NULL"
            )
        )
        conversation_sql = "SELECT id FROM cleanup_conversations"
        # Foreign-key-safe deletion. Users and course-date configuration remain.
        for statement in (
            "DELETE FROM payment_reviews WHERE enrollment_id IN (SELECT id FROM enrollment_drafts)",
            "DELETE FROM payments WHERE enrollment_id IN (SELECT id FROM enrollment_drafts)",
            "DELETE FROM credit_note_requests WHERE enrollment_id IN (SELECT id FROM enrollment_drafts)",
            "DELETE FROM financial_documents WHERE enrollment_id IN (SELECT id FROM enrollment_drafts)",
            "DELETE FROM payment_evidence WHERE enrollment_id IN (SELECT id FROM enrollment_drafts)",
            "DELETE FROM outbound_messages WHERE enrollment_id IN (SELECT id FROM enrollment_drafts) "
            "OR conversation_id IN (" + conversation_sql + ") "
            "OR purpose = 'invoice_live_test'",
            "DELETE FROM invoice_generations WHERE enrollment_id IN (SELECT id FROM enrollment_drafts)",
            "DELETE FROM invoices WHERE enrollment_id IN (SELECT id FROM enrollment_drafts)",
            "DELETE FROM finance_cases WHERE enrollment_id IN (SELECT id FROM enrollment_drafts)",
            "DELETE FROM finance_audit WHERE enrollment_id IN (SELECT id FROM enrollment_drafts)",
            "DELETE FROM master_invoice_exports WHERE enrollment_id IN (SELECT id FROM enrollment_drafts)",
            "DELETE FROM followup_runs WHERE lead_id IN (SELECT id FROM leads WHERE conversation_id IN (" + conversation_sql + "))",
            "DELETE FROM consent_records WHERE conversation_id IN (" + conversation_sql + ")",
            "DELETE FROM contact_mappings WHERE conversation_id IN (" + conversation_sql + ")",
            "DELETE FROM leads WHERE conversation_id IN (" + conversation_sql + ")",
            "DELETE FROM operations_audit WHERE conversation_id IN (" + conversation_sql + ")",
            "DELETE FROM staff_escalations WHERE conversation_id IN (" + conversation_sql + ")",
            "DELETE FROM messages WHERE conversation_id IN (" + conversation_sql + ")",
            "DELETE FROM enrollment_drafts",
            "DELETE FROM conversations WHERE id IN (" + conversation_sql + ")",
        ):
            db.session.execute(text(statement))
        db.session.execute(text("UPDATE course_dates SET participant_count = 0"))
        db.session.execute(text("DROP TABLE cleanup_conversations"))
        db.session.commit()
        invoice_dir = self.storage_dir / "invoices"
        removed_pdfs = 0
        if invoice_dir.is_dir():
            for path in invoice_dir.glob("*.pdf"):
                path.unlink()
                removed_pdfs += 1
        removed_private_artifacts = 0
        if self.storage_dir.is_dir():
            for path in self.storage_dir.iterdir():
                if path.is_file() and re.fullmatch(
                    r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", path.name, re.I
                ):
                    path.unlink()
                    removed_private_artifacts += 1
        self.master_invoice.rebuild_master_invoice_workbook("maintenance-cleanup")
        after = self.dry_run()
        return {
            "before": before,
            "after": after,
            "backup": backup,
            "removed_invoice_pdfs": removed_pdfs,
            "removed_private_artifacts": removed_private_artifacts,
        }

    def _backup(self):
        database_path = Path(db.engine.url.database or "").resolve()
        if db.engine.url.get_backend_name() != "sqlite" or not database_path.is_file():
            raise RuntimeError("Cleanup requires the local SQLite development database.")
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_dir = database_path.parent / "backups" / f"enrollment-cleanup-{timestamp}"
        backup_dir.mkdir(parents=True, exist_ok=False)
        destination = backup_dir / database_path.name
        source_connection = sqlite3.connect(database_path)
        backup_connection = sqlite3.connect(destination)
        try:
            source_connection.backup(backup_connection)
        finally:
            backup_connection.close()
            source_connection.close()
        workbook = Path(self.master_invoice.workbook_path)
        workbook_backup = None
        if workbook.is_file():
            workbook_backup = backup_dir / workbook.name
            shutil.copy2(workbook, workbook_backup)
        invoice_dir = self.storage_dir / "invoices"
        pdf_backup = None
        if invoice_dir.is_dir() and any(invoice_dir.glob("*.pdf")):
            pdf_backup = backup_dir / "invoice-pdfs"
            shutil.copytree(invoice_dir, pdf_backup)
        runtime_backup = None
        runtime_files = [
            path for path in self.storage_dir.iterdir()
            if path.is_file() and re.fullmatch(
                r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", path.name, re.I
            )
        ] if self.storage_dir.is_dir() else []
        if runtime_files:
            runtime_backup = backup_dir / "private-runtime-artifacts"
            runtime_backup.mkdir()
            for path in runtime_files:
                shutil.copy2(path, runtime_backup / path.name)
        return {
            "directory": str(backup_dir),
            "database": str(destination),
            "workbook": str(workbook_backup) if workbook_backup else None,
            "invoice_pdfs": str(pdf_backup) if pdf_backup else None,
            "private_runtime_artifacts": str(runtime_backup) if runtime_backup else None,
        }

    @staticmethod
    def _count(table):
        return db.session.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar_one()


__all__ = ["CONFIRMATION_PHRASE", "MaintenanceService"]
