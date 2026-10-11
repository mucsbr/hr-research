import os

import pytest
from cryptography.fernet import Fernet
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.config import ROOT, get_settings
from app.db import Base, get_engine, session_factory
from app import models  # noqa: F401
from app import sentiment_models  # noqa: F401
from app import notification_models  # noqa: F401
from app import news_models  # noqa: F401
from app import recruitment_management_models  # noqa: F401
from app import maintenance_models  # noqa: F401
from app import auto_maintenance_models  # noqa: F401
from app import checkpoint_models  # noqa: F401
from app import adapter_code_models  # noqa: F401
from app import adapter_runtime_models  # noqa: F401
from app import retention_models  # noqa: F401
from app import probe_models  # noqa: F401
from app import model_settings  # noqa: F401


@pytest.fixture(scope="session")
def engine():
    test_url = get_settings().test_database_url.get_secret_value()
    if not test_url or not make_url(test_url).database.endswith("_test"):
        pytest.fail("Integration tests require an explicit TEST_DATABASE_URL ending with _test")
    overrides = {
        "DATABASE_URL": test_url,
        "APP_ENV": "test",
        "LLM_API_KEY": "unit-test-not-a-real-key",
        "LLM_BASE_URL": "https://models.example.invalid/v1",
        "LLM_MODEL": "glm-5.3",  # Explicit Mock fixture, never a product allowlist.
        "LLM_FALLBACK_MODEL": "deepseek-flash",
        "LLM_REASONING_EFFORT": "low",
        "LLM_DAILY_REQUEST_LIMIT": "12",  # Mock requests only; APP_ENV=test blocks real model transport.
        "ADMIN_PASSWORD": "unit-test-password-only",
        "APP_SECRET": "unit-test-only-signing-secret-" * 3,
        "ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "BETTAFISH_REPO_DIR": "",
        "BETTAFISH_PYTHON": "",
        "BETTAFISH_LICENSE_CONFIRMED": "false",
        "SMTP_HOST": "",
        "SMTP_USERNAME": "",
        "SMTP_PASSWORD": "",
        "SMTP_FROM": "",
        "ADAPTER_RUNNER_URL": "",
        "ADAPTER_RUNNER_TOKEN": "",
        "ADAPTER_RUNNER_RUNTIME_DIGEST": "",
        "DISCOVERY_INTERNAL_SEARCH_URL": "",
    }
    original = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    get_settings.cache_clear()
    get_engine.cache_clear()
    engine = get_engine()
    # This is the explicitly dedicated *_test database, never the application database.
    Base.metadata.drop_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    command.upgrade(Config(str(ROOT / "backend" / "alembic.ini")), "head")
    yield engine
    engine.dispose()
    for key, value in original.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_settings.cache_clear()
    get_engine.cache_clear()


@pytest.fixture
def db(engine, tmp_path):
    names = ", ".join('"' + table.name + '"' for table in Base.metadata.sorted_tables)
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE " + names + " RESTART IDENTITY CASCADE"))
    settings = get_settings()
    old_storage = settings.storage_dir
    settings.storage_dir = tmp_path / "artifacts"
    with session_factory()() as session:
        yield session
    settings.storage_dir = old_storage


@pytest.fixture
def client(db):
    from app.main import app
    with TestClient(app) as client:
        yield client


@pytest.fixture
def signed_client(client):
    settings = get_settings()
    response = client.post("/api/v1/auth/login", json={
        "username": settings.admin_username,
        "password": settings.admin_password.get_secret_value(),
    })
    assert response.status_code == 200
    return client
