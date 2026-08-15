"""Session and engine wiring. Sync SQLAlchemy on purpose.

Runs execute in a separate worker process, not in the API process, so the API never blocks and a
SIGKILL to the worker is a real crash to recover from rather than a simulated one. Row-level locks
and FOR UPDATE SKIP LOCKED are far easier to reason about synchronously, and there is no throughput
argument for async here: this system is model-latency bound, not connection bound.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from ledgerline.config import get_settings

_engine = None
_factory: sessionmaker[Session] | None = None


def engine():
    global _engine
    if _engine is None:
        _engine = create_engine(
            get_settings().database_url,
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=10,
            future=True,
        )
    return _engine


def session_factory() -> sessionmaker[Session]:
    global _factory
    if _factory is None:
        _factory = sessionmaker(bind=engine(), expire_on_commit=False, future=True)
    return _factory


@contextmanager
def session_scope() -> Iterator[Session]:
    session = session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
