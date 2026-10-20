import os
from datetime import UTC, datetime

# Tests use a separate database and refuse to truncate any database without the suffix.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://bandwidth:bandwidth@127.0.0.1:5432/bandwidth_test"
)
os.environ["COOKIE_SECURE"] = "false"
os.environ["ISE_MODE"] = "mock"
os.environ["ACCOUNTING_SOURCE"] = "mock"
os.environ["ADMIN_API_TOKEN"] = "test-automation-token"

import psycopg  # noqa: E402
import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from psycopg import sql  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from bandwidth import accounting, api, auth, engine, queue, worker  # noqa: E402
from bandwidth.db import Base, transaction  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def migrate():
    url = make_url(os.environ["DATABASE_URL"])
    assert url.database.endswith("_test"), "Refusing to use a non-test database"
    kwargs = dict(host=url.host, port=url.port, user=url.username, password=url.password)
    with psycopg.connect(dbname="postgres", autocommit=True, **kwargs) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (url.database,)).fetchone():
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(url.database)))
    command.upgrade(Config("alembic.ini"), "head")
    yield


@pytest.fixture(autouse=True)
def reset(migrate):
    with transaction() as db:
        names = ",".join('"' + t.name + '"' for t in Base.metadata.sorted_tables)
        db.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)

    value = Clock()
    for module in (accounting, api, auth, engine, queue, worker):
        monkeypatch.setattr(module, "utcnow", lambda: value.now)
    return value
