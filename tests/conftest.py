import os
import uuid

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from easyem import models  # noqa: F401  (registers tables)
from easyem.api.deps import get_db
from easyem.api.ratelimit import limiter
from easyem.db import Base
from easyem.main import app
from easyem.notifications import MemoryEmailBackend, set_mailer


@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(eng, "connect")
    def _fk(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture()
def db(engine):
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()
    try:
        yield session
        session.commit()
    finally:
        session.close()


@pytest.fixture(autouse=True)
def outbox():
    """Collect email instead of printing it, and let tests read the token a
    real user would receive.

    Reaching into the database to fake verification is what hid the fact that
    no email was ever sent. Tests now go through the same door as users.
    """
    backend = MemoryEmailBackend()
    set_mailer(backend)
    yield backend
    set_mailer(None)


@pytest.fixture(autouse=True)
def reset_rate_limits():
    """The limiter is process-wide; without this, tests poison each other."""
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture()
def client(db):
    def _override():
        yield db

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def account(db):
    from easyem.models import Account, AccountType

    acc = Account(type=AccountType.personal, name="Test account")
    db.add(acc)
    db.flush()
    return acc


def new_job_id() -> uuid.UUID:
    return uuid.uuid4()
