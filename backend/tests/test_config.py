"""Configuration loading and safe diagnostic tests."""

from pathlib import Path

from backend import config
from backend.app import create_app
from backend.tests.conftest import FakeAIService


def test_backend_environment_path_is_based_on_config_file():
    assert config.BACKEND_DIR == Path(config.__file__).resolve().parent
    assert config.BACKEND_DIR.is_absolute()


def test_health_diagnostic_reports_booleans_not_values(tmp_path):
    secret = "synthetic-secret-value"
    model = "gpt-5-mini"
    app = create_app(
        {
            "TESTING": True,
            "OPENAI_API_KEY": secret,
            "OPENAI_MODEL": model,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / 'health.db').as_posix()}",
        },
        ai_service=FakeAIService(),
    )
    response = app.test_client().get("/health")
    rendered = response.get_data(as_text=True)
    assert response.get_json()["ai_configuration"] == {
        "api_key_configured": True,
        "model_configured": True,
    }
    assert secret not in rendered
    assert model not in rendered
