import os
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base, get_db
from app.main import app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a disposable PostgreSQL test database")
    if not url.split("?", 1)[0].endswith("_test"):
        pytest.fail("TEST_DATABASE_URL database name must end in _test")

    engine = create_engine(url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    local_sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def override_db() -> Generator[Session, None, None]:
        with local_sessions() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    # Tests drive the real worker explicitly, using the disposable test database.
    monkeypatch.setenv("SHIPPING_WORKER_ENABLED", "0")
    try:
        with TestClient(app) as test_client:
            test_client.shipping_sessions = local_sessions
            yield test_client
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)
        engine.dispose()
