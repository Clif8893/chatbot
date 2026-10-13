"""Development intake selection, persistence and export regressions."""

import json
from datetime import date, timedelta
from decimal import Decimal

import pytest
from openpyxl import load_workbook

from backend.app import create_app
from backend.extensions import db
from backend.models import Conversation, EnrollmentDraft, User
from backend.models.finance import MasterInvoiceExport
from backend.models.operations import CourseDate
from backend.services.course_catalogue import CATALOGUE_PATH, CourseCatalogue
from backend.services.master_invoice_service import HEADERS, SHEET_NAME, TABLE_NAME
from backend.tests.conftest import authenticated_client, send


APPROVED_RECORDS = (
    ("dental-assisting-2026-11-18", "2026-11-18", "2026-11-19", "18–19 November 2026"),
    ("dental-assisting-2026-11-28", "2026-11-28", "2026-11-29", "28–29 November 2026"),
    ("dental-assisting-2026-12-16", "2026-12-16", "2026-12-17", "16–17 December 2026"),
)


def demo_app(tmp_path, fake_ai, *, catalogue=None, enabled=True, name="demo.db"):
    return create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "demo-intake-test-secret",
            "OPENAI_API_KEY": "",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / name).as_posix()}",
            "ENABLE_DEMO_INTAKES": enabled,
            # Keep the fixed 2026 demonstration catalogue deterministic. Runtime
            # behaviour still rejects historical intakes against Singapore today.
            "ENROLLMENT_CURRENT_DATE": date(2026, 10, 1),
        },
        ai_service=fake_ai,
        catalogue=catalogue,
    )


def begin_intake_selection(client):
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    reply = send(
        client, conversation_id, "I want to enroll in Dental Assisting"
    ).get_json()
    return conversation_id, reply


def test_approved_records_are_structured_unique_valid_and_consecutive():
    catalogue = CourseCatalogue(enable_demo_intakes=True)
    records = [
        item for item in catalogue.approved_intakes
        if item["course_id"] == "dental-assisting-basic"
    ]

    assert [
        (item["id"], item["start_date"], item["end_date"], item["display_date"])
        for item in records
    ] == list(APPROVED_RECORDS)
    assert len({item["id"] for item in records}) == 3
    assert all(item["is_demo"] is False for item in records)
    assert all(item["status"] == "open" for item in records)
    for item in records:
        assert date.fromisoformat(item["end_date"]) == (
            date.fromisoformat(item["start_date"]) + timedelta(days=1)
        )


def test_approved_dates_are_available_regardless_of_demo_flag(tmp_path, fake_ai):
    disabled = CourseCatalogue()
    enabled = CourseCatalogue(enable_demo_intakes=True)
    assert len(disabled.intake_dates) == 6
    assert "demo-2026" not in disabled.agent_context()
    assert [
        item["start_date"]
        for item in enabled.selectable_intakes_for("basic-dental-assisting")
    ] == [record[1] for record in APPROVED_RECORDS]

    app = demo_app(tmp_path, fake_ai, enabled=False)
    client = authenticated_client(app)
    created = client.post("/api/conversations").get_json()
    assert created["demo_intakes_enabled"] is False
    offer = send(
        client,
        created["conversation_id"],
        "I want to enroll in Dental Assisting",
    ).get_json()
    assert "18–19 November 2026" in offer["message"]["content"]

    env_example = (CATALOGUE_PATH.parents[1] / ".env.example").read_text(
        encoding="utf-8"
    )
    assert "ENABLE_DEMO_INTAKES=true" in env_example


def test_flow_lists_approved_intakes(tmp_path, fake_ai):
    app = demo_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id, selection = begin_intake_selection(client)

    assert selection["enrollment"]["demo_intakes_enabled"] is True
    content = selection["message"]["content"]
    assert "Testing note" not in content
    assert "prototype" not in content.casefold()
    assert content.count("Available Intake Dates") == 1
    assert "Available Intake Dates" in content
    for index, record in enumerate(APPROVED_RECORDS, start=1):
        assert f"{index}. {record[3]}" in content
    assert "reply with the intake number or the dates you prefer" in content
    assert conversation_id


@pytest.mark.parametrize(
    ("selection", "expected_id"),
    [("1", APPROVED_RECORDS[0][0]), ("2", APPROVED_RECORDS[1][0]), ("3", APPROVED_RECORDS[2][0])],
)
def test_numbered_selection_persists_structured_intake(
    tmp_path, fake_ai, selection, expected_id
):
    app = demo_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id, _ = begin_intake_selection(client)
    response = send(client, conversation_id, selection).get_json()

    selected = response["enrollment"]["selected_intake"]
    assert selected["id"] == expected_id
    assert selected["is_demo"] is False
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        expected = next(record for record in APPROVED_RECORDS if record[0] == expected_id)
        assert draft.intake_id == expected_id
        assert draft.intake_start_date.isoformat() == expected[1]
        assert draft.intake_end_date.isoformat() == expected[2]
        assert draft.preferred_intake_date == draft.intake_start_date
        assert draft.intake_is_demo is False


@pytest.mark.parametrize(
    ("selection", "expected_id"),
    [
        ("Option 3", APPROVED_RECORDS[2][0]),
        ("The third one", APPROVED_RECORDS[2][0]),
        ("16 December", APPROVED_RECORDS[2][0]),
        ("The weekend intake", APPROVED_RECORDS[1][0]),
    ],
)
def test_persisted_followup_resolves_natural_third_choice(
    tmp_path, fake_ai, selection, expected_id
):
    app = demo_app(tmp_path, fake_ai, name=f"natural-{abs(hash(selection))}.db")
    client = authenticated_client(app)
    conversation_id, offered = begin_intake_selection(client)
    assert offered["message"]["content"].count("Available Intake Dates") == 1
    response = send(client, conversation_id, selection).get_json()
    assert response["enrollment"]["selected_intake"]["id"] == expected_id
    assert "Which detail would you like to correct" not in response["message"]["content"]


def test_displayed_choices_and_numeric_selection_survive_restart_and_login(
    tmp_path, fake_ai
):
    database_name = "pending-intake-restart.db"
    app = demo_app(tmp_path, fake_ai, name=database_name)
    owner = authenticated_client(app, email="pending-intake@example.com")
    conversation_id, _ = begin_intake_selection(owner)
    with app.app_context():
        conversation = db.session.get(Conversation, conversation_id)
        assert conversation.pending_intake_options == [item[0] for item in APPROVED_RECORDS]

    restarted = demo_app(tmp_path, fake_ai, name=database_name)
    resumed = restarted.test_client()
    login = resumed.post(
        "/api/auth/login",
        json={"email": owner.test_email, "password": owner.test_password},
    )
    assert login.status_code == 200
    token = login.get_json()["csrf_token"]
    selected = resumed.post(
        "/api/chat",
        json={"conversation_id": conversation_id, "message": "3"},
        headers={"X-CSRF-Token": token},
    ).get_json()
    assert selected["enrollment"]["selected_intake"]["id"] == APPROVED_RECORDS[2][0]


def test_displayed_numeric_choice_is_rejected_if_it_closes_before_selection(
    tmp_path, fake_ai
):
    app = demo_app(tmp_path, fake_ai, name="closed-after-display.db")
    client = authenticated_client(app)
    conversation_id, _ = begin_intake_selection(client)
    with app.app_context():
        row = db.session.get(CourseDate, APPROVED_RECORDS[2][0])
        row.status = "closed"
        db.session.commit()

    response = send(client, conversation_id, "3").get_json()
    assert "intake is closed" in response["message"]["content"]
    assert response["enrollment"]["selected_intake"] is None


@pytest.mark.parametrize(
    ("selection", "expected_id"),
    [
        ("2026-11-18", APPROVED_RECORDS[0][0]),
        ("18–19 November 2026", APPROVED_RECORDS[0][0]),
        ("I choose 28 November", APPROVED_RECORDS[1][0]),
        ("weekend intake on 28 November", APPROVED_RECORDS[1][0]),
    ],
)
def test_date_range_and_natural_language_selection(
    tmp_path, fake_ai, selection, expected_id
):
    app = demo_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id, _ = begin_intake_selection(client)
    response = send(client, conversation_id, selection).get_json()
    assert response["enrollment"]["selected_intake"]["id"] == expected_id


def test_arbitrary_closed_and_ambiguous_intakes_are_rejected(tmp_path, fake_ai):
    base = json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))
    base["course"]["intakes"][1]["status"] = "closed"
    base["course"]["intakes"].append(
        {
            "id": "dental-assisting-2026-11-23",
            "course_id": "dental-assisting-basic",
            "start_date": "2026-11-23",
            "end_date": "2026-11-24",
            "display_date": "23–24 November 2026",
            "intake_type": "Weekdays",
            "capacity": 20,
            "participant_count": 0,
            "status": "open",
            "is_demo": False,
        }
    )
    path = tmp_path / "intake-edge-cases.json"
    path.write_text(json.dumps(base), encoding="utf-8")
    catalogue = CourseCatalogue(path, enable_demo_intakes=True)

    closed_app = demo_app(tmp_path, fake_ai, catalogue=catalogue, name="closed.db")
    closed_client = authenticated_client(closed_app)
    conversation_id, _ = begin_intake_selection(closed_client)
    closed = send(closed_client, conversation_id, "2026-11-28").get_json()
    assert "intake is closed" in closed["message"]["content"]
    assert closed["enrollment"]["selected_intake"] is None

    ambiguous = send(closed_client, conversation_id, "the November intake").get_json()
    assert "more than one intake" in ambiguous["message"]["content"]
    arbitrary = send(closed_client, conversation_id, "Intake: 22 October 2026").get_json()
    assert "not one of the available intakes" in arbitrary["message"]["content"]


def test_intake_survives_faq_greeting_reload_logout_and_is_owner_scoped(
    tmp_path, fake_ai
):
    app = demo_app(tmp_path, fake_ai)
    owner = authenticated_client(app, email="demo-owner@example.com")
    conversation_id, _ = begin_intake_selection(owner)
    send(owner, conversation_id, "2")

    faq = send(owner, conversation_id, "Can I use SkillsFuture?").get_json()
    assert "SkillsFuture" in faq["message"]["content"]
    greeting = send(owner, conversation_id, "Good morning").get_json()
    assert greeting["message"]["content"].startswith("Good morning")
    history = owner.get(f"/api/conversations/{conversation_id}/messages").get_json()
    assert history["enrollment"]["selected_intake"]["id"] == APPROVED_RECORDS[1][0]

    assert owner.post("/api/auth/logout").status_code == 200
    login = owner.post(
        "/api/auth/login",
        json={"email": owner.test_email, "password": owner.test_password},
    )
    assert login.status_code == 200
    resumed = owner.get(f"/api/conversations/{conversation_id}/messages")
    assert resumed.get_json()["enrollment"]["selected_intake"]["id"] == APPROVED_RECORDS[1][0]

    other = authenticated_client(app, email="other-demo-user@example.com")
    assert other.get(f"/api/conversations/{conversation_id}/messages").status_code == 404
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        owner_user = User.query.filter_by(email="demo-owner@example.com").one()
        assert draft.conversation.user_id == owner_user.id


def test_intake_correction_changes_only_intake_fields(tmp_path, fake_ai):
    app = demo_app(tmp_path, fake_ai)
    client = authenticated_client(app)
    conversation_id, _ = begin_intake_selection(client)
    send(client, conversation_id, "1")
    send(client, conversation_id, "Full name: Test Student")

    corrected = send(client, conversation_id, "Correct intake: 3").get_json()
    assert corrected["enrollment"]["selected_intake"]["id"] == APPROVED_RECORDS[2][0]
    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.full_name == "Test Student"
        assert draft.nric is None


def _complete_enrollment(client, conversation_id):
    send(client, conversation_id, "1")
    return send(
        client,
        conversation_id,
        "Full name: Test Student; NRIC/FIN: S1234567D; "
        "Date of birth: 15 May 2000; Email: test.student@example.com; "
        "Mobile number: 91234567",
    ).get_json()


def test_confirmation_and_excel_export_are_single_owner_scoped_and_idempotent(
    tmp_path, fake_ai
):
    app = demo_app(tmp_path, fake_ai)
    client = authenticated_client(app, email="confirmed-demo@example.com")
    conversation_id, _ = begin_intake_selection(client)
    review = _complete_enrollment(client, conversation_id)

    assert review["enrollment"]["status"] == "awaiting_confirmation"
    assert "Intake: 18–19 November 2026" in review["message"]["content"]
    assert "prototype" not in review["message"]["content"].casefold()
    service = app.extensions["master_invoice"]
    assert not service.workbook_path.exists()
    with app.app_context():
        assert MasterInvoiceExport.query.count() == 0

    first = send(client, conversation_id, "Confirm").get_json()
    second = send(client, conversation_id, "Confirm").get_json()
    assert first["enrollment"]["status"] == "confirmed"
    assert second["enrollment"]["status"] == "confirmed"

    with app.app_context():
        draft = EnrollmentDraft.query.one()
        assert draft.conversation.user.email == "confirmed-demo@example.com"
        assert draft.confirmed_at is not None
        assert MasterInvoiceExport.query.count() == 1
        assert MasterInvoiceExport.query.one().attempts == 1

    workbook = load_workbook(service.workbook_path)
    sheet = workbook[SHEET_NAME]
    assert tuple(cell.value for cell in sheet[1]) == HEADERS
    assert sheet.max_row == 2
    assert sheet[2][3].value == "Basic Certificate in Dental Assisting"
    assert sheet[2][4].value.date().isoformat() == "2026-11-18"
    assert sheet[2][5].value.date().isoformat() == "2026-11-19"
    assert sheet[2][7].value == 600
    assert sheet[2][8].value == "S1234567D"
    assert sheet[2][16].value is None
    assert sheet[2][17].value is None
    assert sheet[2][18].value is None
    assert sheet[2][19].value is None
    assert sheet[2][22].value == draft.id
    assert sheet[2][23].value == APPROVED_RECORDS[0][0]
    assert sheet[2][24].value == "No"
    assert sheet[2][21].value == "Exported successfully"
    assert TABLE_NAME in sheet.tables
