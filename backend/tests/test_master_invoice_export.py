"""Secure Master Invoice List export and staff-access regressions."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook, load_workbook
from werkzeug.security import generate_password_hash

from backend.app import create_app
from backend.extensions import db
from backend.models import EnrollmentDraft, utc_now
from backend.models.finance import MasterInvoiceAudit, MasterInvoiceExport
from backend.models.operations import CourseDate
from backend.services.master_invoice_service import HEADERS, SHEET_NAME, TABLE_NAME
from backend.tests.conftest import authenticated_client


def export_app(tmp_path, fake_ai, *, template_path=""):
    return create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "master-invoice-test-secret",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / 'export.db').as_posix()}",
            "MASTER_INVOICE_PATH": str(tmp_path / "private" / "Master_Invoice_List.xlsx"),
            "MASTER_INVOICE_TEMPLATE_PATH": str(template_path),
            "STAFF_ACCOUNTS": {
                "accountant": {
                    "password_hash": generate_password_hash("staff-password"),
                    "role": "accountant",
                },
                "operator": {
                    "password_hash": generate_password_hash("staff-password"),
                    "role": "operations",
                },
            },
        },
        ai_service=fake_ai,
    )


def add_draft(
    app,
    client,
    *,
    confirmed=True,
    name="Test Student",
    split=True,
    date_of_birth=date(2000, 5, 15),
    skillsfuture_amount=None,
    paynow_amount=None,
    payment_method=None,
    intake=None,
):
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    with app.app_context():
        draft = EnrollmentDraft(
            conversation_id=conversation_id,
            course="2-Day Basic Certificate in Dental Assisting",
            course_fee=Decimal("600"),
            full_name=name,
            nric="S1234567D",
            date_of_birth=date_of_birth,
            email="test.student@example.com",
            mobile_number="91234567",
            payment_method=payment_method or (
                "skillsfuture_paynow" if split else "paynow"
            ),
            skillsfuture_amount=(
                Decimal("400") if split else Decimal("0")
            ) if skillsfuture_amount is None else Decimal(str(skillsfuture_amount)),
            paynow_amount=(
                Decimal("200") if split else Decimal("600")
            ) if paynow_amount is None else Decimal(str(paynow_amount)),
            status=(
                "confirmed" if confirmed and intake else
                "awaiting_course_date" if confirmed else "collecting"
            ),
            validation_status="valid" if confirmed else "incomplete",
            confirmed_at=utc_now() if confirmed else None,
        )
        if intake:
            row = db.session.get(CourseDate, intake[0])
            if row is None:
                row = CourseDate(
                    id=intake[0],
                    date=intake[1],
                    end_date=intake[2],
                    label=intake[3],
                    capacity=20,
                    status="open",
                    is_demo=True,
                )
                db.session.add(row)
            draft.intake_id = row.id
            draft.preferred_intake_date = row.date
            draft.intake_start_date = row.date
            draft.intake_end_date = row.end_date
            draft.intake_is_demo = row.is_demo
        db.session.add(draft)
        db.session.commit()
        return draft.id


def staff_login(client, username="accountant"):
    response = client.post(
        "/api/staff/login",
        json={"username": username, "password": "staff-password"},
    )
    assert response.status_code == 200
    return response.get_json()["csrf_token"]


def csrf_post(client, path, token):
    return client.post(path, json={}, headers={"X-CSRF-Token": token})


def test_confirmed_rows_are_idempotent_formatted_and_rebuilt(tmp_path, fake_ai):
    app = export_app(tmp_path, fake_ai)
    participant = authenticated_client(app)
    first_id = add_draft(app, participant, name="Formula Attempt")
    second_id = add_draft(app, participant, name="Unicode Student 李", split=False)
    add_draft(app, participant, confirmed=False, name="Unconfirmed Student")
    service = app.extensions["master_invoice"]

    with app.app_context():
        first = db.session.get(EnrollmentDraft, first_id)
        assert service.queue_and_export(first) is True
        assert service.queue_and_export(first) is True
        service.regenerate("accountant")
        assert MasterInvoiceExport.query.count() == 2

    workbook = load_workbook(service.workbook_path)
    sheet = workbook[SHEET_NAME]
    assert tuple(cell.value for cell in sheet[1]) == HEADERS
    assert sheet.max_row == 3
    assert sheet.freeze_panes == "A2"
    assert sheet.auto_filter.ref == "A1:Z3"
    assert TABLE_NAME in sheet.tables
    assert {sheet[2][22].value, sheet[3][22].value} == {first_id, second_id}
    assert service._safe_text("=Formula Attempt").startswith("'")
    assert sheet[2][7].number_format == '"S$"#,##0.00'
    assert sheet[2][14].number_format == "dd-mmm-yyyy"
    assert sheet[2][8].value == "S1234567D"
    assert sheet[2][16].value == "SkillsFuture Credit + PayNow"
    assert sheet[2][17].value == 400
    assert sheet[2][18].value == 200
    assert sheet[2][19].value == 600
    assert sheet[2][1].value.startswith("DEMO-INV-")
    assert sheet[2][2].value == "BCDA-2D"
    assert sheet[2][9].value is not None

    service.workbook_path.unlink()
    with app.app_context():
        result = service.regenerate("accountant")
        assert result["confirmed_records"] == 2
        assert service.workbook_path.is_file()


def test_export_failure_keeps_confirmation_pending_and_retryable(
    tmp_path, fake_ai, monkeypatch
):
    app = export_app(tmp_path, fake_ai)
    participant = authenticated_client(app)
    enrollment_id = add_draft(app, participant)
    service = app.extensions["master_invoice"]
    original_save = service._atomic_save
    monkeypatch.setattr(
        service,
        "_atomic_save",
        lambda workbook: (_ for _ in ()).throw(PermissionError("locked")),
    )

    with app.app_context():
        draft = db.session.get(EnrollmentDraft, enrollment_id)
        assert service.queue_and_export(draft) is False
        db.session.expire_all()
        assert db.session.get(EnrollmentDraft, enrollment_id).confirmed_at is not None
        export = MasterInvoiceExport.query.filter_by(
            enrollment_id=enrollment_id
        ).one()
        assert export.status == "failed"
        assert export.last_error and "Retry" in export.last_error

    monkeypatch.setattr(service, "_atomic_save", original_save)
    with app.app_context():
        assert service.retry_failed("accountant") is True
        assert MasterInvoiceExport.query.one().status == "exported"


def test_master_invoice_endpoints_are_accounting_only_and_audited(
    tmp_path, fake_ai
):
    app = export_app(tmp_path, fake_ai)
    participant = authenticated_client(app)
    enrollment_id = add_draft(app, participant)
    with app.app_context():
        app.extensions["master_invoice"].queue_and_export(
            db.session.get(EnrollmentDraft, enrollment_id)
        )

    assert participant.get("/api/staff/master-invoice/download").status_code == 401
    assert participant.post(
        f"/api/staff/master-invoice/retry/{enrollment_id}", json={}
    ).status_code == 401
    operator = app.test_client()
    operator_token = staff_login(operator, "operator")
    assert operator.get("/api/staff/master-invoice/download").status_code == 403
    assert csrf_post(
        operator,
        f"/api/staff/master-invoice/retry/{enrollment_id}",
        operator_token,
    ).status_code == 403

    accountant = app.test_client()
    token = staff_login(accountant)
    status = accountant.get("/api/staff/master-invoice/status")
    assert status.status_code == 200
    assert status.get_json()["master_invoice"]["confirmed_records"] == 1
    assert status.get_json()["master_invoice"]["pending_exports"] == 0
    assert status.get_json()["master_invoice"]["failed_exports"] == 0
    download = accountant.get("/api/staff/master-invoice/download")
    assert download.status_code == 200
    assert download.data.startswith(b"PK")
    assert "Master_Invoice_List.xlsx" in download.headers["Content-Disposition"]
    assert csrf_post(
        accountant, "/api/staff/master-invoice/regenerate", token
    ).status_code == 200
    assert csrf_post(
        accountant,
        f"/api/staff/master-invoice/retry/{enrollment_id}",
        token,
    ).status_code == 200
    with app.app_context():
        actions = [row.action for row in MasterInvoiceAudit.query.all()]
        assert "download" in actions
        assert "regenerate" in actions


def test_incomplete_and_invalid_dob_are_rejected_but_historical_payment_is_preserved(
    tmp_path, fake_ai
):
    app = export_app(tmp_path, fake_ai)
    participant = authenticated_client(app)
    incomplete_id = add_draft(app, participant, confirmed=False)
    future_dob_id = add_draft(
        app,
        participant,
        name="Future Date Student",
        date_of_birth=date.today() + timedelta(days=1),
    )
    invalid_payment_id = add_draft(
        app,
        participant,
        name="Invalid Payment Student",
        skillsfuture_amount="500",
        paynow_amount="300",
    )
    with app.app_context():
        service = app.extensions["master_invoice"]
        for enrollment_id in (incomplete_id, future_dob_id):
            assert service.export_confirmed_enrollment(enrollment_id) is False
        assert service.export_confirmed_enrollment(invalid_payment_id) is True
        assert MasterInvoiceExport.query.count() == 1
        assert service.workbook_path.exists()
    workbook = load_workbook(service.workbook_path)
    sheet = workbook[SHEET_NAME]
    assert sheet.cell(2, 18).value == 500
    assert sheet.cell(2, 19).value == 300
    assert sheet.cell(2, 20).value == 800


def test_supported_payment_allocations_export_as_numeric_cells(tmp_path, fake_ai):
    app = export_app(tmp_path, fake_ai)
    participant = authenticated_client(app)
    split_id = add_draft(app, participant, name="Split Payment Student")
    paynow_id = add_draft(app, participant, name="PayNow Student", split=False)
    skillsfuture_id = add_draft(
        app,
        participant,
        name="SkillsFuture Student",
        payment_method="skillsfuture_paynow",
        skillsfuture_amount="600",
        paynow_amount="0",
    )
    with app.app_context():
        service = app.extensions["master_invoice"]
        for enrollment_id in (split_id, paynow_id, skillsfuture_id):
            assert service.export_confirmed_enrollment(enrollment_id) is True
    workbook = load_workbook(service.workbook_path)
    rows = {
        workbook[SHEET_NAME].cell(row=row, column=23).value: row
        for row in range(2, workbook[SHEET_NAME].max_row + 1)
    }
    sheet = workbook[SHEET_NAME]
    assert (sheet.cell(rows[split_id], 18).value, sheet.cell(rows[split_id], 19).value) == (400, 200)
    assert (sheet.cell(rows[paynow_id], 18).value, sheet.cell(rows[paynow_id], 19).value) == (0, 600)
    assert (sheet.cell(rows[skillsfuture_id], 18).value, sheet.cell(rows[skillsfuture_id], 19).value) == (600, 0)
    for row in rows.values():
        assert sheet.cell(row, 20).value == 600
        assert sheet.cell(row, 20).data_type == "n"


def test_selected_intake_and_excel_date_cells_export_correctly(tmp_path, fake_ai):
    app = export_app(tmp_path, fake_ai)
    participant = authenticated_client(app)
    enrollment_id = add_draft(
        app,
        participant,
        intake=(
            "test-intake-2026-10-10",
            date(2026, 10, 10),
            date(2026, 10, 11),
            "Weekend",
        ),
    )
    with app.app_context():
        service = app.extensions["master_invoice"]
        assert service.export_confirmed_enrollment(enrollment_id)
    sheet = load_workbook(service.workbook_path)[SHEET_NAME]
    assert sheet.cell(2, 5).value.date() == date(2026, 10, 10)
    assert sheet.cell(2, 6).value.date() == date(2026, 10, 11)
    assert sheet.cell(2, 10).value.date() <= date.today()
    assert sheet.cell(2, 15).value.date() == date(2000, 5, 15)
    for column in (5, 6, 10, 15):
        assert sheet.cell(2, column).is_date


def test_restart_reuses_invoice_number_and_row_without_duplicates(tmp_path, fake_ai):
    app = export_app(tmp_path, fake_ai)
    participant = authenticated_client(app, email="restart-export@example.com")
    enrollment_id = add_draft(app, participant)
    with app.app_context():
        service = app.extensions["master_invoice"]
        assert service.export_confirmed_enrollment(enrollment_id)
        original_number = MasterInvoiceExport.query.one().invoice_number

    restarted = export_app(tmp_path, fake_ai)
    with restarted.app_context():
        service = restarted.extensions["master_invoice"]
        assert service.export_confirmed_enrollment(enrollment_id)
        record = MasterInvoiceExport.query.one()
        assert record.invoice_number == original_number
        assert record.export_version == 1
    sheet = load_workbook(service.workbook_path)[SHEET_NAME]
    assert sheet.max_row == 2


def test_correction_and_intake_assignment_update_the_existing_row(tmp_path, fake_ai):
    app = export_app(tmp_path, fake_ai)
    participant = authenticated_client(app)
    enrollment_id = add_draft(app, participant, name="Original Student")
    with app.app_context():
        service = app.extensions["master_invoice"]
        assert service.export_confirmed_enrollment(enrollment_id)
        record = MasterInvoiceExport.query.one()
        invoice_number = record.invoice_number
        draft = db.session.get(EnrollmentDraft, enrollment_id)
        draft.full_name = "Corrected Student"
        intake = CourseDate(
            id="assigned-intake",
            date=date(2026, 11, 4),
            end_date=date(2026, 11, 5),
            label="Weekday",
            capacity=20,
            status="open",
            is_demo=True,
        )
        db.session.add(intake)
        draft.intake_id = intake.id
        draft.preferred_intake_date = intake.date
        draft.intake_start_date = intake.date
        draft.intake_end_date = intake.end_date
        draft.intake_is_demo = True
        draft.status = "confirmed"
        db.session.commit()
        assert service.update_exported_enrollment(enrollment_id, "staff")
        record = MasterInvoiceExport.query.one()
        assert record.invoice_number == invoice_number
        assert record.export_version == 2
    sheet = load_workbook(service.workbook_path)[SHEET_NAME]
    assert sheet.max_row == 2
    assert sheet.cell(2, 7).value == "Corrected Student"
    assert sheet.cell(2, 5).value.date() == date(2026, 11, 4)


def test_duplicate_names_use_separate_enrollment_ids(tmp_path, fake_ai):
    app = export_app(tmp_path, fake_ai)
    participant = authenticated_client(app)
    first_id = add_draft(app, participant, name="Same Name")
    second_id = add_draft(app, participant, name="Same Name")
    with app.app_context():
        service = app.extensions["master_invoice"]
        assert service.rebuild_master_invoice_workbook("test")
        assert MasterInvoiceExport.query.count() == 2
        assert len({row.invoice_number for row in MasterInvoiceExport.query.all()}) == 2
    sheet = load_workbook(service.workbook_path)[SHEET_NAME]
    assert {sheet.cell(2, 23).value, sheet.cell(3, 23).value} == {first_id, second_id}


def test_concurrent_attempts_produce_one_record_and_one_row(tmp_path, fake_ai):
    app = export_app(tmp_path, fake_ai)
    participant = authenticated_client(app)
    enrollment_id = add_draft(app, participant)

    def attempt():
        with app.app_context():
            return app.extensions["master_invoice"].export_confirmed_enrollment(
                enrollment_id
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: attempt(), range(2)))
    assert all(results)
    with app.app_context():
        assert MasterInvoiceExport.query.count() == 1
    assert load_workbook(app.extensions["master_invoice"].workbook_path)[SHEET_NAME].max_row == 2


def test_formula_injection_prefixes_are_escaped():
    for value in ("=1+1", "+SUM(A1:A2)", "-2+3", "@command"):
        assert app_safe_text(value).startswith("'")


def test_approved_master_template_formula_is_preserved(tmp_path, fake_ai):
    template_path = tmp_path / "approved-master-template.xlsx"
    template = Workbook()
    master = template.active
    master.title = SHEET_NAME
    for column, header in enumerate(HEADERS, start=1):
        master.cell(row=1, column=column, value=header)
    master.cell(row=10, column=26, value="=SUM(H2:H9)")
    rejected = template.create_sheet("Unrelated Private Data")
    rejected.cell(row=1, column=1, value="must not be copied")
    template.save(template_path)
    template.close()

    app = export_app(tmp_path, fake_ai, template_path=template_path)
    participant = authenticated_client(app)
    enrollment_id = add_draft(app, participant)
    with app.app_context():
        assert app.extensions["master_invoice"].export_confirmed_enrollment(
            enrollment_id
        )

    workbook = load_workbook(app.extensions["master_invoice"].workbook_path)
    try:
        assert workbook.sheetnames == [SHEET_NAME]
        assert workbook[SHEET_NAME].cell(row=10, column=26).value == "=SUM(H2:H9)"
        assert workbook[SHEET_NAME].cell(row=10, column=26).data_type == "f"
    finally:
        workbook.close()


def app_safe_text(value):
    from backend.services.master_invoice_service import MasterInvoiceService

    return MasterInvoiceService._safe_text(value)


def test_failed_update_preserves_previous_workbook(tmp_path, fake_ai, monkeypatch):
    app = export_app(tmp_path, fake_ai)
    participant = authenticated_client(app)
    enrollment_id = add_draft(app, participant)
    service = app.extensions["master_invoice"]
    with app.app_context():
        assert service.export_confirmed_enrollment(enrollment_id)
        previous = service.workbook_path.read_bytes()
        draft = db.session.get(EnrollmentDraft, enrollment_id)
        draft.full_name = "Updated Student"
        db.session.commit()
        original_replace = __import__("os").replace
        monkeypatch.setattr(
            "backend.services.master_invoice_service.os.replace",
            lambda source, target: (_ for _ in ()).throw(PermissionError("open")),
        )
        assert service.update_exported_enrollment(enrollment_id, "staff") is False
        assert service.workbook_path.read_bytes() == previous
        assert MasterInvoiceExport.query.one().status == "failed"
        monkeypatch.setattr(
            "backend.services.master_invoice_service.os.replace", original_replace
        )
        assert service.retry_failed_export(enrollment_id, "staff")
    assert load_workbook(service.workbook_path)[SHEET_NAME].cell(2, 7).value == "Updated Student"


def test_cancellation_removes_existing_row_from_active_workbook(tmp_path, fake_ai):
    app = export_app(tmp_path, fake_ai)
    participant = authenticated_client(app)
    enrollment_id = add_draft(app, participant)
    with app.app_context():
        service = app.extensions["master_invoice"]
        assert service.export_confirmed_enrollment(enrollment_id)
        draft = db.session.get(EnrollmentDraft, enrollment_id)
        draft.status = "cancelled"
        draft.confirmed_at = None
        db.session.commit()
        assert service.update_exported_enrollment(enrollment_id, "staff")
    sheet = load_workbook(service.workbook_path)[SHEET_NAME]
    assert sheet.max_row == 1
    assert sheet.cell(2, 21).value is None
    with app.app_context():
        assert MasterInvoiceExport.query.one().status == "removed_after_cancellation"


def test_status_api_masks_sensitive_data_and_exposes_retry_state(tmp_path, fake_ai):
    app = export_app(tmp_path, fake_ai)
    participant = authenticated_client(app)
    enrollment_id = add_draft(app, participant)
    with app.app_context():
        assert app.extensions["master_invoice"].export_confirmed_enrollment(
            enrollment_id
        )
        payload = app.extensions["master_invoice"].status()
        assert "S1234567D" not in str(payload)
        assert payload["records"][0]["status"] == "exported"
        assert payload["records"][0]["retry_available"] is False


def test_export_does_not_call_ai_and_generated_files_are_ignored(tmp_path, fake_ai):
    app = export_app(tmp_path, fake_ai)
    participant = authenticated_client(app)
    enrollment_id = add_draft(app, participant)
    before = (
        len(fake_ai.classify_calls),
        len(fake_ai.faq_calls),
        len(fake_ai.enrollment_calls),
    )
    with app.app_context():
        assert app.extensions["master_invoice"].export_confirmed_enrollment(
            enrollment_id
        )
    after = (
        len(fake_ai.classify_calls),
        len(fake_ai.faq_calls),
        len(fake_ai.enrollment_calls),
    )
    assert after == before
    ignore = Path(".gitignore").read_text(encoding="utf-8")
    assert "*.xlsx" in ignore
    assert "backend/data/private/" in ignore
    assert "backend/data/exports/" in ignore


def test_in_memory_test_app_never_uses_runtime_master_workbook(fake_ai):
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "memory-export-isolation",
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        },
        ai_service=fake_ai,
    )
    configured = Path(app.config["MASTER_INVOICE_PATH"]).resolve()
    runtime = Path("backend/data/private/Master_Invoice_List.xlsx").resolve()
    assert configured != runtime
    assert runtime.parent not in configured.parents
