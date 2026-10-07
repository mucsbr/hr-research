"""Domain clients; database isolation is inherited from the guarded parent fixture."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.security import bootstrap_admin, current_user
from app.models import User
from app.sentiment_api import router


@pytest.fixture
def sentiment_client(db):
    bootstrap_admin(db)
    user = db.query(User).first()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[current_user] = lambda: user
    with TestClient(app) as client:
        yield client


@pytest.fixture
def anonymous_sentiment_client(db):
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def news_client(db):
    from app.news_api import router as news_router
    bootstrap_admin(db)
    user = db.query(User).first()
    app = FastAPI()
    app.include_router(news_router)
    app.dependency_overrides[current_user] = lambda: user
    with TestClient(app) as client:
        yield client


@pytest.fixture
def analytics_client(db):
    from app.analytics_api import router as analytics_router
    from app.assistant_api import router as assistant_router
    bootstrap_admin(db)
    user = db.query(User).first()
    app = FastAPI()
    app.include_router(analytics_router)
    app.include_router(assistant_router)
    app.dependency_overrides[current_user] = lambda: user
    with TestClient(app) as client:
        yield client
