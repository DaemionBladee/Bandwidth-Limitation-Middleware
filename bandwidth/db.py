from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from bandwidth.config import settings


def utcnow():
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


engine = create_engine(settings().database_url, pool_pre_ping=True)
Session = sessionmaker(engine, expire_on_commit=False)


@contextmanager
def transaction():
    with Session.begin() as session:
        yield session
