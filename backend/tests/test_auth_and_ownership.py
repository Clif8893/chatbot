"""Participant authentication, ownership and staff-boundary regression tests."""

from werkzeug.security import generate_password_hash

from backend.app import create_app
from backend.models import Conversation, User


PASSWORD = "safe-test-password"


def register(client, email, name="Test Participant"):
    return client.post(
        "/api/auth/register",
        json={"full_name": name, "email": email, "password": PASSWORD},
    )


def csrf_post(client, path, token, **kwargs):
    headers = dict(kwargs.pop("headers", {}) or {})
    headers["X-CSRF-Token"] = token
    return client.post(path, headers=headers, **kwargs)


def test_registration_login_and_password_hashing(app):
    client = app.test_client()
    response = register(client, "account@example.com")
    assert response.status_code == 201
    assert response.get_json()["authenticated"] is True

    with app.app_context():
        user = User.query.filter_by(email="account@example.com").one()
        assert user.password_hash != PASSWORD
        assert user.check_password(PASSWORD)

    token = response.get_json()["csrf_token"]
    assert csrf_post(client, "/api/auth/logout", token).status_code == 200
    login = client.post(
        "/api/auth/login",
        json={"email": "account@example.com", "password": PASSWORD},
    )
    assert login.status_code == 200
    assert login.get_json()["user"]["email"] == "account@example.com"


def test_chatbot_endpoints_require_participant_session(app):
    client = app.test_client()
    assert client.post("/api/conversations").status_code == 401
    assert client.get("/api/conversations/unknown/messages").status_code == 401
    assert client.post(
        "/api/chat", json={"conversation_id": "unknown", "message": "Hello"}
    ).status_code == 401


def test_conversation_ownership_and_cross_user_rejection(app):
    owner = app.test_client()
    other = app.test_client()
    owner_auth = register(owner, "owner@example.com").get_json()
    other_auth = register(other, "other@example.com").get_json()
    created = csrf_post(owner, "/api/conversations", owner_auth["csrf_token"])
    conversation_id = created.get_json()["conversation_id"]

    assert owner.get(f"/api/conversations/{conversation_id}/messages").status_code == 200
    assert other.get(f"/api/conversations/{conversation_id}/messages").status_code == 404
    rejected = csrf_post(
        other,
        "/api/chat",
        other_auth["csrf_token"],
        json={"conversation_id": conversation_id, "message": "What is the fee?"},
    )
    assert rejected.status_code == 404

    with app.app_context():
        conversation = Conversation.query.filter_by(id=conversation_id).one()
        owner_user = User.query.filter_by(email="owner@example.com").one()
        assert conversation.user_id == owner_user.id


def test_conversation_can_resume_after_logout_and_login(app):
    client = app.test_client()
    auth = register(client, "resume-owner@example.com").get_json()
    created = csrf_post(client, "/api/conversations", auth["csrf_token"])
    conversation_id = created.get_json()["conversation_id"]
    assert csrf_post(client, "/api/auth/logout", auth["csrf_token"]).status_code == 200
    assert client.get(f"/api/conversations/{conversation_id}/messages").status_code == 401

    login = client.post(
        "/api/auth/login",
        json={"email": "resume-owner@example.com", "password": PASSWORD},
    )
    assert login.status_code == 200
    assert client.get(f"/api/conversations/{conversation_id}/messages").status_code == 200


def test_logout_csrf_and_staff_participant_boundaries(tmp_path, fake_ai):
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "boundary-test-secret",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / 'boundaries.db').as_posix()}",
            "STAFF_ACCOUNTS": {
                "operator": {
                    "password_hash": generate_password_hash("staff-password"),
                    "role": "operations",
                }
            },
        },
        ai_service=fake_ai,
    )
    participant = app.test_client()
    auth = register(participant, "boundary@example.com").get_json()
    assert participant.get("/api/staff/leads").status_code == 401
    assert participant.post("/api/auth/logout").status_code == 403

    staff = app.test_client()
    login = staff.post(
        "/api/staff/login",
        json={"username": "operator", "password": "staff-password"},
    )
    assert login.status_code == 200
    assert staff.post("/api/conversations").status_code == 401
    assert csrf_post(participant, "/api/auth/logout", auth["csrf_token"]).status_code == 200


def test_expired_session_cannot_resume_owned_conversation(app):
    client = app.test_client()
    auth = register(client, "expired@example.com").get_json()
    created = csrf_post(client, "/api/conversations", auth["csrf_token"])
    conversation_id = created.get_json()["conversation_id"]
    with client.session_transaction() as stored_session:
        stored_session.clear()
    assert client.get(f"/api/conversations/{conversation_id}/messages").status_code == 401
