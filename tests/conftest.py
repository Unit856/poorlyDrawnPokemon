from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Point the app at a scratch data dir before app.config is imported anywhere.
_TMP = Path(tempfile.mkdtemp(prefix="wtp-test-"))
os.environ.setdefault("WTP_DATA_DIR", str(_TMP))
os.environ.setdefault("WTP_DATABASE_URL", "sqlite:///:memory:")

from app.db import Base, make_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402


@pytest.fixture()
def session():
    import app.models  # noqa: F401  (registers mappers)

    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    db = factory()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()
