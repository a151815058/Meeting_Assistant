import os

os.environ.setdefault("FLASK_ENV", "testing")
os.environ.setdefault("TOKEN_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")

import pytest
from sqlalchemy import text

from app import create_app
from app.extensions import db as _db


@pytest.fixture()
def app():
    application = create_app("testing")
    with application.app_context():
        # pgvector must already be installed in the test database by a superuser (README);
        # IF NOT EXISTS then needs no privilege.
        with _db.engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()
        # Every test builds its own engine; close its pooled connections or a long suite
        # exhausts PostgreSQL's max_connections.
        _db.engine.dispose()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def db(app):
    return _db
