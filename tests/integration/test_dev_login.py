import re

import pytest

from app import create_app
from app.config import ProductionConfig
from app.models.audit import AuditLog
from app.models.user import User

FORM = {"email": "dev@localhost.test", "display_name": "開發測試"}


@pytest.fixture()
def dev_login_on(app):
    app.config["DEV_LOGIN_ENABLED"] = True
    return app


def _csrf_token(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


# --- TC-29: dev login ---------------------------------------------------------------

def test_dev_login_is_disabled_by_default(client, app):
    assert app.config["DEV_LOGIN_ENABLED"] is False
    assert client.post("/auth/dev-login", data=FORM).status_code == 404
    assert "開發模式登入" not in client.get("/auth/login").get_data(as_text=True)


def test_dev_login_creates_user_logs_in_and_audits(client, dev_login_on):
    assert "開發模式登入" in client.get("/auth/login").get_data(as_text=True)

    resp = client.post("/auth/dev-login", data=FORM)

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/meetings/")
    assert client.get("/meetings/").status_code == 200  # session is authenticated
    with dev_login_on.app_context():
        user = User.query.filter_by(email="dev@localhost.test").one()
        assert user.display_name == "開發測試"
        audit = AuditLog.query.filter_by(action="auth.dev_login").one()
        assert audit.actor_user_id == user.id


def test_dev_login_reuses_existing_user(client, dev_login_on):
    client.post("/auth/dev-login", data=FORM)
    client.get("/auth/logout")
    client.post("/auth/dev-login", data={**FORM, "email": "DEV@localhost.test"})

    with dev_login_on.app_context():
        assert User.query.filter_by(email="dev@localhost.test").count() == 1


def test_dev_login_rejects_remote_clients(client, dev_login_on):
    resp = client.post("/auth/dev-login", data=FORM, environ_base={"REMOTE_ADDR": "192.168.1.50"})
    assert resp.status_code == 404
    with dev_login_on.app_context():
        assert User.query.count() == 0


@pytest.mark.parametrize("form", [
    {"email": "not-an-email", "display_name": "x"},
    {"email": "a@b.test", "display_name": ""},
    {"email": "a@b.test", "display_name": "x" * 101},
    {},
], ids=["bad-email", "empty-name", "long-name", "missing"])
def test_dev_login_validates_input(client, dev_login_on, form):
    resp = client.post("/auth/dev-login", data=form)

    assert resp.status_code == 302 and resp.headers["Location"].endswith("/auth/login")
    with dev_login_on.app_context():
        assert User.query.count() == 0


def test_app_refuses_to_start_with_dev_login_in_production(monkeypatch):
    monkeypatch.setattr(ProductionConfig, "DEV_LOGIN_ENABLED", True)
    with pytest.raises(RuntimeError, match="DEV_LOGIN_ENABLED"):
        create_app("production")


def test_production_config_never_enables_dev_login(monkeypatch):
    monkeypatch.setenv("DEV_LOGIN_ENABLED", "true")
    assert ProductionConfig.DEV_LOGIN_ENABLED is False


# --- TC-30: CSRF tokens present on every form ---------------------------------------

def test_forms_work_with_csrf_enabled(client, dev_login_on):
    dev_login_on.config["WTF_CSRF_ENABLED"] = True

    login_page = client.get("/auth/login").get_data(as_text=True)
    assert client.post("/auth/dev-login", data=FORM).status_code == 400  # no token
    assert client.post("/auth/dev-login", data={**FORM, "csrf_token": _csrf_token(login_page)}).status_code == 302

    create_page = client.get("/meetings/new").get_data(as_text=True)
    assert client.post("/meetings/new", data={"title": "x"}).status_code == 400  # no token
    resp = client.post("/meetings/new", data={"title": "週會", "platform": "teams",
                                              "csrf_token": _csrf_token(create_page)})
    assert resp.status_code == 302

    detail = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert 'name="csrf_token"' in detail  # participant-sync form
