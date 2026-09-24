"""Opt-in smoke test; excluded from normal runs to prevent real API calls."""

import os

import pytest

from backend.app import create_app
from backend.tests.conftest import authenticated_client, send


@pytest.mark.skipif(
    os.getenv("RUN_REAL_OPENAI_TEST") != "1",
    reason="Set RUN_REAL_OPENAI_TEST=1 explicitly to call the real provider.",
)
def test_real_faq_smoke(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / 'real.db').as_posix()}",
        }
    )
    client = authenticated_client(app)
    conversation_id = client.post("/api/conversations").get_json()["conversation_id"]
    response = send(client, conversation_id, "What is the course duration?")
    assert response.status_code == 200
    assert "2 days" in response.get_json()["message"]["content"]
