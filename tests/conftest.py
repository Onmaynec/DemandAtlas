import os
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from backend.app.db import Base, SessionLocal
from backend.app import models  # noqa: F401

os.environ["DEMO_MODE"] = "1"
os.environ.pop("FORECAST_URL", None)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "db.sqlite"),
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    SessionLocal.configure(bind=engine)
    yield
    SessionLocal.remove() if hasattr(SessionLocal, "remove") else None
    engine.dispose()
