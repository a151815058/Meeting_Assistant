import time
from datetime import datetime, timezone

import pytest

from app.models.audit import AuditLog

from app.models.user import OAuthAccount, User


def test_login_page_renders(client):
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    assert "Google".encode() in resp.data


def test_google_login_redirects_and_sets_state(client, app, mocker):
    app.config.update(GOOGLE_CONFIG)
    mocker.patch("app.auth.google_oauth.build_flow", return_value=object())
    mocker.patch(
        "app.auth.google_oauth.get_authorization_url",
        return_value=("https://accounts.google.com/authorize?...", "state-123", "verifier-xyz"),
    )

    resp = client.get("/auth/google/login")

    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("https://accounts.google.com/authorize")
    with client.session_transaction() as sess:
        assert sess["oauth_state"] == "state-123"
        assert sess["oauth_code_verifier"] == "verifier-xyz"


def test_google_callback_creates_user_and_logs_in(client, mocker, db, app):
    with client.session_transaction() as sess:
        sess["oauth_state"] = "state-123"

    mocker.patch("app.auth.google_oauth.build_flow", return_value=object())
    mocker.patch(
        "app.auth.google_oauth.exchange_code",
        return_value={
            "access_token": "fake-access-token",
            "refresh_token": "fake-refresh-token",
            "expiry": datetime.now(timezone.utc),
            "scopes": ["openid", "email"],
        },
    )
    mocker.patch(
        "app.auth.google_oauth.get_userinfo",
        return_value={"email": "carol@example.com", "name": "Carol", "id": "google-999"},
    )

    resp = client.get("/auth/google/callback?state=state-123&code=abc")

    assert resp.status_code == 302
    with app.app_context():
        user = User.query.filter_by(email="carol@example.com").first()
        assert user is not None
        account = OAuthAccount.query.filter_by(user_id=user.id, provider="google").first()
        assert account is not None
        assert account.refresh_token == "fake-refresh-token"


def test_google_callback_rejects_mismatched_state(client):
    with client.session_transaction() as sess:
        sess["oauth_state"] = "expected-state"

    resp = client.get("/auth/google/callback?state=wrong-state&code=abc")

    assert resp.status_code == 400


# --- TC-38: OAuth flows work against the real libraries --------------------------------------

GOOGLE_CONFIG = {"GOOGLE_CLIENT_ID": "cid.apps.googleusercontent.com", "GOOGLE_CLIENT_SECRET": "secret",
                 "GOOGLE_REDIRECT_URI": "http://localhost:5000/auth/google/callback"}


def test_google_pkce_verifier_survives_to_the_callback(client, app, mocker):
    """The real Flow generates a PKCE verifier at login; the callback's new Flow must send it."""
    from requests_oauthlib import OAuth2Session

    app.config.update(GOOGLE_CONFIG)
    resp = client.get("/auth/google/login")  # real google_auth_oauthlib Flow, no mocks

    assert "code_challenge=" in resp.headers["Location"] and "code_challenge_method=S256" in resp.headers["Location"]
    with client.session_transaction() as sess:
        state, verifier = sess["oauth_state"], sess["oauth_code_verifier"]
    assert verifier

    sent = {}

    def fake_fetch_token(self, token_url, **kwargs):  # stands in for the HTTP call to Google
        sent.update(kwargs)
        self.token = {"access_token": "at", "refresh_token": "rt", "token_type": "Bearer",
                      "expires_in": 3600, "expires_at": time.time() + 3600}
        return self.token

    mocker.patch.object(OAuth2Session, "fetch_token", autospec=True, side_effect=fake_fetch_token)
    mocker.patch("app.auth.google_oauth.get_userinfo",
                 return_value={"email": "pkce@example.com", "name": "PKCE", "id": "g-1"})

    resp = client.get(f"/auth/google/callback?state={state}&code=auth-code")

    assert resp.status_code == 302 and resp.headers["Location"].endswith("/meetings/")
    assert sent["code_verifier"] == verifier
    with client.session_transaction() as sess:
        assert "oauth_code_verifier" not in sess and "oauth_state" not in sess
    with app.app_context():
        user = User.query.filter_by(email="pkce@example.com").one()
        assert OAuthAccount.query.filter_by(user_id=user.id, provider="google").one().refresh_token == "rt"


def test_google_partial_consent_is_accepted():
    """Unticking a scope on Google's consent screen must not make oauthlib raise."""
    import json

    from oauthlib.oauth2.rfc6749.parameters import parse_token_response

    from app.auth.google_oauth import GOOGLE_SCOPES

    token = parse_token_response(json.dumps({"access_token": "a", "token_type": "Bearer", "scope": "openid"}),
                                 scope=GOOGLE_SCOPES)
    assert token["scope"] == ["openid"]


def test_microsoft_scopes_are_accepted_by_msal():
    """MSAL raises ValueError for reserved scopes; it adds offline_access itself."""
    from types import SimpleNamespace

    import msal

    from app.auth.microsoft_oauth import MS_SCOPES

    decorated = msal.ClientApplication._decorate_scope(SimpleNamespace(_exclude_scopes=frozenset()), MS_SCOPES)
    assert {"Mail.Send", "Calendars.Read", "User.Read", "offline_access"} <= set(decorated)


def test_microsoft_callback_creates_user_and_logs_in(client, app, mocker):
    with client.session_transaction() as sess:
        sess["oauth_state"] = "ms-state"
    mocker.patch("app.auth.microsoft_oauth.build_msal_app", return_value=object())
    exchange = mocker.patch("app.auth.microsoft_oauth.exchange_code", return_value={
        "access_token": "at", "refresh_token": "ms-rt", "expires_in": 3600,
        "scopes": ["User.Read", "Calendars.Read", "Mail.Send"],
    })
    mocker.patch("app.auth.microsoft_oauth.get_userinfo",
                 return_value={"email": "dave@example.com", "name": "Dave", "id": "ms-1"})

    resp = client.get("/auth/microsoft/callback?state=ms-state&code=abc")

    assert resp.status_code == 302 and resp.headers["Location"].endswith("/meetings/")
    assert exchange.call_args.args[1] == "abc"
    with app.app_context():
        account = OAuthAccount.query.filter_by(provider="microsoft").one()
        assert account.refresh_token == "ms-rt" and account.has_scope("Mail.Send")
        assert account.access_token_expires_at is not None


def test_microsoft_callback_rejects_mismatched_state(client):
    with client.session_transaction() as sess:
        sess["oauth_state"] = "expected-state"
    assert client.get("/auth/microsoft/callback?state=wrong&code=abc").status_code == 400


def test_http_callback_allowed_only_in_development(monkeypatch):
    from oauthlib.oauth2.rfc6749.utils import is_secure_transport

    from app import create_app

    monkeypatch.delenv("OAUTHLIB_INSECURE_TRANSPORT", raising=False)
    create_app("production")  # starts fine and does not relax transport security
    assert not is_secure_transport("http://localhost:5000/auth/google/callback?code=x")

    create_app("development")
    assert is_secure_transport("http://localhost:5000/auth/google/callback?code=x")


def test_production_refuses_insecure_oauth_transport(monkeypatch):
    from app import create_app

    monkeypatch.setenv("OAUTHLIB_INSECURE_TRANSPORT", "1")
    with pytest.raises(RuntimeError, match="OAUTHLIB_INSECURE_TRANSPORT"):
        create_app("production")


# --- TC-39: cancelled or failed authorization ------------------------------------------------

PROVIDERS = [("google", "Google", "app.auth.google_oauth.exchange_code"),
             ("microsoft", "Microsoft", "app.auth.microsoft_oauth.exchange_code")]


def _start(client, state="s-1"):
    with client.session_transaction() as sess:
        sess["oauth_state"] = state
        sess["oauth_code_verifier"] = "v"


@pytest.mark.parametrize("provider,label,exchange_path", PROVIDERS)
def test_user_cancels_consent(client, app, mocker, provider, label, exchange_path):
    _start(client)
    exchange = mocker.patch(exchange_path)

    resp = client.get(f"/auth/{provider}/callback?state=s-1&error=access_denied&error_description=user+cancelled")

    assert resp.status_code == 302 and resp.headers["Location"].endswith("/auth/login")
    assert f"已取消 {label} 授權" in client.get("/auth/login").get_data(as_text=True)
    assert not exchange.called
    with client.session_transaction() as sess:
        assert "oauth_state" not in sess and "oauth_code_verifier" not in sess and "_user_id" not in sess
    with app.app_context():
        assert User.query.count() == 0
        audit = AuditLog.query.filter_by(action="oauth.denied").one()
        assert audit.event_metadata == {"provider": provider, "error": "access_denied"}


@pytest.mark.parametrize("provider,label,exchange_path", PROVIDERS)
def test_unknown_provider_error_is_not_reflected(client, app, mocker, provider, label, exchange_path):
    _start(client)
    mocker.patch(exchange_path)

    client.get(f"/auth/{provider}/callback?state=s-1&error=%3Cscript%3Ealert(1)%3C/script%3E")

    page = client.get("/auth/login").get_data(as_text=True)
    assert f"{label} 登入失敗（other）" in page and "<script>alert(1)" not in page
    with app.app_context():
        assert AuditLog.query.filter_by(action="oauth.denied").one().event_metadata["error"] == "other"


@pytest.mark.parametrize("provider,label,exchange_path", PROVIDERS)
def test_callback_without_code_or_with_failed_exchange(client, app, mocker, provider, label, exchange_path):
    _start(client)
    resp = client.get(f"/auth/{provider}/callback?state=s-1")
    assert resp.status_code == 302
    assert "缺少授權碼" in client.get("/auth/login").get_data(as_text=True)

    _start(client)
    mocker.patch(f"app.auth.{provider}_oauth.build_{'flow' if provider == 'google' else 'msal_app'}",
                 return_value=object())
    mocker.patch(exchange_path, side_effect=RuntimeError("invalid_grant"))
    resp = client.get(f"/auth/{provider}/callback?state=s-1&code=expired")

    assert resp.status_code == 302 and resp.headers["Location"].endswith("/auth/login")
    assert f"無法完成 {label} 登入" in client.get("/auth/login").get_data(as_text=True)
    with app.app_context():
        assert User.query.count() == 0


@pytest.mark.parametrize("provider,label", [("google", "Google"), ("microsoft", "Microsoft")])
def test_login_without_configured_client_shows_message(client, app, provider, label):
    app.config.update(GOOGLE_CLIENT_ID="", GOOGLE_CLIENT_SECRET="", MS_CLIENT_ID="", MS_CLIENT_SECRET="")

    resp = client.get(f"/auth/{provider}/login")

    assert resp.status_code == 302 and resp.headers["Location"].endswith("/auth/login")
    assert f"尚未設定 {label} OAuth 用戶端" in client.get("/auth/login").get_data(as_text=True)
