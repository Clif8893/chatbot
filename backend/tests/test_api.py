"""REST contract, persistence, privacy, and failure tests."""

import pytest

from backend.app import create_app
from backend.tests.conftest import FailingAIService, authenticated_client, send


def test_health_endpoint(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"
    assert response.get_json()["ai_configuration"] == {
        "api_key_configured": False,
        "model_configured": True,
    }


def test_backend_does_not_render_frontend(client):
    assert client.get("/").status_code == 404


def test_create_conversation(client):
    response = client.post("/api/conversations")
    payload = response.get_json()
    assert response.status_code == 201
    assert payload["conversation_id"]
    assert payload["status"] == "active"
    assert payload["created_at"].endswith("Z")


def test_vite_origin_is_allowed(client):
    response = client.options(
        "/api/chat",
        headers={
            "Origin": "http://127.0.0.1:5173",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == "http://127.0.0.1:5173"


@pytest.mark.parametrize(
    ("body", "content_type", "status", "code"),
    [
        ("plain", "text/plain", 415, "INVALID_CONTENT_TYPE"),
        ("[]", "application/json", 400, "INVALID_JSON"),
        ('{"message": 42}', "application/json", 400, "INVALID_MESSAGE"),
        ('{"message": "   ", "conversation_id": "x"}', "application/json", 400, "EMPTY_MESSAGE"),
        ('{"message": "hello"}', "application/json", 400, "INVALID_CONVERSATION_ID"),
    ],
)
def test_invalid_chat_requests(client, body, content_type, status, code):
    response = client.post("/api/chat", data=body, content_type=content_type)
    assert response.status_code == status
    assert response.get_json()["error"]["code"] == code


def test_unknown_conversation_is_rejected(client):
    response = send(client, "not-a-real-id", "course duration")
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "CONVERSATION_NOT_FOUND"


def test_messages_are_persisted_and_reloadable(client, conversation_id):
    assert send(
        client,
        conversation_id,
        "What is the duration of the Basic Certificate in Dental Assisting?",
    ).status_code == 200
    history = client.get(f"/api/conversations/{conversation_id}/messages").get_json()
    messages = history["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert [message["sequence"] for message in messages] == [1, 2]
    assert [message["outcome_status"] for message in messages] == ["success", "success"]
    assert all(message["agent_name"] == "faq_agent" for message in messages)
    assert all(message["detected_intent"] == "faq" for message in messages)
    assert all(message["error_category"] is None for message in messages)
    assert "two consecutive training days" in messages[1]["content"].casefold()


def test_previous_chat_list_is_owned_ordered_and_masks_nric(app, client):
    first_id = client.post("/api/conversations").get_json()["conversation_id"]
    second_id = client.post("/api/conversations").get_json()["conversation_id"]
    send(client, first_id, "My NRIC is S1234567D")
    send(client, second_id, "What is the course fee?")

    payload = client.get("/api/conversations").get_json()

    assert [item["conversation_id"] for item in payload["conversations"]] == [
        second_id,
        first_id,
    ]
    rendered = str(payload)
    assert "S1234567D" not in rendered

    other = authenticated_client(app, "history-other@example.com")
    assert other.get("/api/conversations").get_json()["conversations"] == []


def test_history_survives_a_new_application_instance(tmp_path, fake_ai):
    database_uri = f"sqlite:///{(tmp_path / 'persistent.db').as_posix()}"
    first_app = create_app(
        {"TESTING": True, "SQLALCHEMY_DATABASE_URI": database_uri},
        ai_service=fake_ai,
    )
    first_client = authenticated_client(first_app, "resume@example.com")
    conversation_id = first_client.post("/api/conversations").get_json()[
        "conversation_id"
    ]
    send(first_client, conversation_id, "What is the course duration?")

    second_app = create_app(
        {"TESTING": True, "SQLALCHEMY_DATABASE_URI": database_uri},
        ai_service=fake_ai,
    )
    second_client = second_app.test_client()
    login = second_client.post(
        "/api/auth/login",
        json={"email": "resume@example.com", "password": "test-password-123"},
    )
    csrf = login.get_json()["csrf_token"]
    reloaded = second_client.get(
        f"/api/conversations/{conversation_id}/messages"
    )
    assert reloaded.status_code == 200
    messages = reloaded.get_json()["messages"]
    assert len(messages) == 2
    assert [message["sequence"] for message in messages] == [1, 2]
    assert [message["outcome_status"] for message in messages] == ["success", "success"]


def test_pending_course_offer_survives_restart_without_automatic_selection(tmp_path, fake_ai):
    database_uri = f"sqlite:///{(tmp_path / 'pending-offer.db').as_posix()}"
    first_app = create_app(
        {"TESTING": True, "SQLALCHEMY_DATABASE_URI": database_uri},
        ai_service=fake_ai,
    )
    first_client = authenticated_client(first_app, "pending@example.com")
    conversation_id = first_client.post("/api/conversations").get_json()[
        "conversation_id"
    ]
    offer = send(first_client, conversation_id, "I want to enroll").get_json()
    assert "Please choose a course" in offer["message"]["content"]

    second_app = create_app(
        {"TESTING": True, "SQLALCHEMY_DATABASE_URI": database_uri},
        ai_service=fake_ai,
    )
    second_client = second_app.test_client()
    login = second_client.post(
        "/api/auth/login",
        json={"email": "pending@example.com", "password": "test-password-123"},
    ).get_json()
    original_open = second_client.open

    def open_with_csrf(*args, **kwargs):
        if str(kwargs.get("method", "GET")).upper() not in {"GET", "HEAD", "OPTIONS"}:
            headers = dict(kwargs.get("headers") or {})
            headers.setdefault("X-CSRF-Token", login["csrf_token"])
            kwargs["headers"] = headers
        return original_open(*args, **kwargs)

    second_client.open = open_with_csrf
    listed = second_client.get("/api/conversations").get_json()["conversations"]
    assert listed[0]["conversation_id"] == conversation_id
    resumed = send(second_client, conversation_id, "yes please").get_json()
    assert resumed["routing"]["intent"] == "enrollment"
    assert "Please choose a course" in resumed["message"]["content"]
    assert resumed["enrollment"]["status"] is None


def test_enrollment_progress_survives_application_restart(tmp_path, fake_ai):
    database_uri = f"sqlite:///{(tmp_path / 'resumable.db').as_posix()}"
    first_app = create_app(
        {"TESTING": True, "SQLALCHEMY_DATABASE_URI": database_uri},
        ai_service=fake_ai,
    )
    first_client = authenticated_client(first_app, "progress@example.com")
    conversation_id = first_client.post("/api/conversations").get_json()[
        "conversation_id"
    ]
    send(first_client, conversation_id, "I want to enroll in Dental Assisting")
    send(first_client, conversation_id, "1")
    send(first_client, conversation_id, "My name is Test Student")

    second_app = create_app(
        {"TESTING": True, "SQLALCHEMY_DATABASE_URI": database_uri},
        ai_service=fake_ai,
    )
    second_client = second_app.test_client()
    login = second_client.post(
        "/api/auth/login",
        json={"email": "progress@example.com", "password": "test-password-123"},
    )
    csrf = login.get_json()["csrf_token"]
    original_open = second_client.open
    def open_with_csrf(*args, **kwargs):
        if str(kwargs.get("method", "GET")).upper() not in {"GET", "HEAD", "OPTIONS"}:
            headers = dict(kwargs.get("headers") or {})
            headers.setdefault("X-CSRF-Token", csrf)
            kwargs["headers"] = headers
        return original_open(*args, **kwargs)
    second_client.open = open_with_csrf
    history = second_client.get(
        f"/api/conversations/{conversation_id}/messages"
    ).get_json()
    assert history["active_intent"] == "enrollment"
    assert history["enrollment"]["status"] == "collecting"
    assert "nric" in history["enrollment"]["missing_fields"]

    resumed = send(
        second_client, conversation_id, "My NRIC is S1234567D"
    ).get_json()
    assert "NRIC/FIN: S*******D" in resumed["message"]["content"]
    assert "Full name as shown" not in resumed["message"]["content"]
    assert "date of birth" in resumed["message"]["content"].casefold()


def test_nric_is_masked_in_history_agent_context_and_logs(
    client, conversation_id, fake_ai, caplog
):
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "1")
    send(client, conversation_id, "My NRIC is S1234567D")
    send(client, conversation_id, "My email is test.student@example.com")
    history = client.get(f"/api/conversations/{conversation_id}/messages").get_json()
    rendered = " ".join(message["content"] for message in history["messages"])
    assert "S1234567D" not in rendered
    assert "S*******D" in rendered
    assert "S1234567D" not in fake_ai.classify_calls[-1]["message"]
    assert "S1234567D" not in fake_ai.classify_calls[-1]["context"]
    enrollment_call = fake_ai.enrollment_calls[-1]
    assert "S1234567D" not in enrollment_call["message"]
    assert "S1234567D" not in enrollment_call["context"]
    assert "S1234567D" not in str(enrollment_call["draft_state"])
    assert "S1234567D" not in caplog.text


def test_identity_and_contact_values_are_redacted_at_ai_and_log_boundaries(
    client, conversation_id, fake_ai, caplog
):
    send(client, conversation_id, "I want to enroll in Dental Assisting")
    send(client, conversation_id, "1")
    message = (
        "Full name: Avery Tan; NRIC: S1234567D; Date of birth: 2000-05-15; "
        "Email: avery.private@example.com; Mobile: 91234567"
    )
    assert send(client, conversation_id, message).status_code == 200

    call = fake_ai.enrollment_calls[-1]
    protected_values = (
        "Avery Tan",
        "S1234567D",
        "2000-05-15",
        "avery.private@example.com",
        "91234567",
    )
    for value in protected_values:
        assert value not in call["message"]
        assert value not in call["context"]
        assert value not in caplog.text


def test_context_is_bounded(tmp_path, fake_ai):
    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / 'bounded.db').as_posix()}",
            "CONVERSATION_CONTEXT_MESSAGE_LIMIT": 3,
        },
        ai_service=fake_ai,
    )
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    for question in ("course duration?", "course fee?", "course location?", "course capacity?"):
        send(client, conversation_id, question)
    context_lines = fake_ai.classify_calls[-1]["context"].splitlines()
    message_markers = [
        line for line in context_lines if line.startswith(("user: ", "assistant: "))
    ]
    assert len(message_markers) <= 3


def test_missing_api_configuration_returns_safe_error(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "OPENAI_API_KEY": "",
            "OPENAI_MODEL": "",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / 'missing.db').as_posix()}",
        }
    )
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    response = send(client, conversation_id, "What is the fee?")
    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "AI_CONFIGURATION_ERROR"


def test_provider_failure_does_not_leak_details_or_secret(tmp_path):
    secret = "unit-test-secret-must-never-appear"
    app = create_app(
        {
            "TESTING": True,
            "OPENAI_API_KEY": secret,
            "OPENAI_MODEL": "gpt-5-mini",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / 'failure.db').as_posix()}",
        },
        ai_service=FailingAIService(),
    )
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    response = send(client, conversation_id, "Is parking included with the course?")
    rendered = response.get_data(as_text=True)
    assert response.status_code == 502
    assert "provider detail" not in rendered
    assert secret not in rendered
    history = client.get(f"/api/conversations/{conversation_id}/messages").get_json()
    assert len(history["messages"]) == 1
    failed_turn = history["messages"][0]
    assert failed_turn["role"] == "user"
    assert failed_turn["sequence"] == 1
    assert failed_turn["outcome_status"] == "error"
    assert failed_turn["error_category"] == "ai_service"
