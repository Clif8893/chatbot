"""Compatibility coverage retained from the original Stage 1 test module."""


def test_backend_remains_api_only(client):
    assert client.get("/").status_code == 404


def test_health_remains_available(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"


def test_chat_preflight_allows_local_vite(client):
    response = client.options(
        "/api/chat",
        headers={
            "Origin": "http://127.0.0.1:5173",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == "http://127.0.0.1:5173"
