import secrets
from datetime import datetime, timedelta, timezone

from flask import abort, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import login_required, login_user, logout_user
from marshmallow import Schema, ValidationError, fields, validate

from app.auth import auth_bp, google_oauth, microsoft_oauth
from app.extensions import db, login_manager
from app.models.user import OAuthAccount, User
from app.security.audit import record_audit_event


@login_manager.user_loader
def load_user(user_id: str):
    return db.session.get(User, user_id)


def _upsert_user_and_oauth_account(*, provider: str, provider_account_id: str, email: str,
                                    display_name: str, access_token: str, refresh_token: str | None,
                                    scopes: list[str], expires_at) -> User:
    user = User.query.filter_by(email=email).first()
    if user is None:
        user = User(email=email, display_name=display_name)
        db.session.add(user)
        db.session.flush()

    account = OAuthAccount.query.filter_by(user_id=user.id, provider=provider).first()
    if account is None:
        account = OAuthAccount(user_id=user.id, provider=provider, provider_account_id=provider_account_id)
        db.session.add(account)

    account.provider_account_id = provider_account_id
    account.scopes = " ".join(scopes)
    account.access_token_expires_at = expires_at
    if refresh_token:
        # Providers only return a refresh_token on the first consent; keep the
        # existing one if this exchange didn't return a new one.
        account.refresh_token = refresh_token

    db.session.commit()
    return user


@auth_bp.route("/login")
def login():
    return render_template("auth/login.html")


@auth_bp.route("/google/login")
def google_login():
    if not (current_app.config["GOOGLE_CLIENT_ID"] and current_app.config["GOOGLE_CLIENT_SECRET"]):
        flash("尚未設定 Google OAuth 用戶端（GOOGLE_CLIENT_ID／GOOGLE_CLIENT_SECRET），請洽系統管理員", "error")
        return redirect(url_for("auth.login"))
    flow = google_oauth.build_flow(
        current_app.config["GOOGLE_CLIENT_ID"],
        current_app.config["GOOGLE_CLIENT_SECRET"],
        current_app.config["GOOGLE_REDIRECT_URI"],
    )
    auth_url, state, code_verifier = google_oauth.get_authorization_url(flow)
    session["oauth_state"] = state
    session["oauth_code_verifier"] = code_verifier  # PKCE: needed again by the callback's new Flow
    return redirect(auth_url)


# Error codes a provider may put on the callback URL (RFC 6749 §4.1.2.1). Only these are shown
# or audited verbatim; anything else is recorded as "other" so the URL cannot inject text.
_OAUTH_ERRORS = {"access_denied", "consent_required", "interaction_required", "login_required",
                 "invalid_request", "invalid_scope", "server_error", "temporarily_unavailable",
                 "unauthorized_client", "unsupported_response_type"}


def _callback_problem(provider: str, label: str):
    """None if the callback carries a code to exchange, otherwise the redirect to show instead.
    Must be called after the state check."""
    error = request.args.get("error")
    if error:
        code = error if error in _OAUTH_ERRORS else "other"
        record_audit_event(actor_user_id=None, action="oauth.denied", target_type="oauth_account",
                           metadata={"provider": provider, "error": code})
        if code == "access_denied":
            return _login_failed(f"已取消 {label} 授權，尚未登入")
        return _login_failed(f"{label} 登入失敗（{code}），請再試一次")
    if not request.args.get("code"):
        return _login_failed(f"{label} 登入失敗：缺少授權碼，請再試一次")
    return None


def _login_failed(message: str):
    session.pop("oauth_state", None)
    session.pop("oauth_code_verifier", None)
    flash(message, "error")
    return redirect(url_for("auth.login"))


@auth_bp.route("/google/callback")
def google_callback():
    if request.args.get("state") != session.get("oauth_state"):
        return "Invalid OAuth state", 400
    problem = _callback_problem("google", "Google")
    if problem is not None:
        return problem

    flow = google_oauth.build_flow(
        current_app.config["GOOGLE_CLIENT_ID"],
        current_app.config["GOOGLE_CLIENT_SECRET"],
        current_app.config["GOOGLE_REDIRECT_URI"],
    )
    try:
        tokens = google_oauth.exchange_code(flow, request.url, session.get("oauth_code_verifier"))
        userinfo = google_oauth.get_userinfo(tokens["access_token"])
    except Exception:  # oauthlib / HTTP / provider errors: show a message instead of a 500
        current_app.logger.exception("Google OAuth callback failed")
        return _login_failed("無法完成 Google 登入，請再試一次")

    user = _upsert_user_and_oauth_account(
        provider="google",
        provider_account_id=userinfo["id"],
        email=userinfo["email"],
        display_name=userinfo["name"],
        access_token=tokens["access_token"],
        refresh_token=tokens.get("refresh_token"),
        scopes=tokens.get("scopes", []),
        expires_at=tokens.get("expiry"),
    )
    login_user(user)
    record_audit_event(actor_user_id=user.id, action="oauth_grant", target_type="oauth_account",
                        target_id=user.id, metadata={"provider": "google"})
    session.pop("oauth_state", None)
    session.pop("oauth_code_verifier", None)
    return redirect(url_for("meetings.dashboard"))


@auth_bp.route("/microsoft/login")
def microsoft_login():
    if not (current_app.config["MS_CLIENT_ID"] and current_app.config["MS_CLIENT_SECRET"]):
        flash("尚未設定 Microsoft OAuth 用戶端（MS_CLIENT_ID／MS_CLIENT_SECRET），請洽系統管理員", "error")
        return redirect(url_for("auth.login"))
    msal_app = microsoft_oauth.build_msal_app(
        current_app.config["MS_CLIENT_ID"],
        current_app.config["MS_CLIENT_SECRET"],
        current_app.config["MS_TENANT_ID"],
    )
    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state
    auth_url = microsoft_oauth.get_authorization_url(
        msal_app, current_app.config["MS_REDIRECT_URI"], state
    )
    return redirect(auth_url)


@auth_bp.route("/microsoft/callback")
def microsoft_callback():
    if request.args.get("state") != session.get("oauth_state"):
        return "Invalid OAuth state", 400
    problem = _callback_problem("microsoft", "Microsoft")
    if problem is not None:
        return problem

    try:
        msal_app = microsoft_oauth.build_msal_app(
            current_app.config["MS_CLIENT_ID"],
            current_app.config["MS_CLIENT_SECRET"],
            current_app.config["MS_TENANT_ID"],
        )
        tokens = microsoft_oauth.exchange_code(
            msal_app, request.args["code"], current_app.config["MS_REDIRECT_URI"]
        )
        userinfo = microsoft_oauth.get_userinfo(tokens["access_token"])
    except Exception:  # MSAL / HTTP / provider errors: show a message instead of a 500
        current_app.logger.exception("Microsoft OAuth callback failed")
        return _login_failed("無法完成 Microsoft 登入，請再試一次")

    expires_at = None
    if tokens.get("expires_in"):
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=tokens["expires_in"])

    user = _upsert_user_and_oauth_account(
        provider="microsoft",
        provider_account_id=userinfo["id"],
        email=userinfo["email"],
        display_name=userinfo["name"],
        access_token=tokens["access_token"],
        refresh_token=tokens.get("refresh_token"),
        scopes=tokens.get("scopes", []),
        expires_at=expires_at,
    )
    login_user(user)
    record_audit_event(actor_user_id=user.id, action="oauth_grant", target_type="oauth_account",
                        target_id=user.id, metadata={"provider": "microsoft"})
    session.pop("oauth_state", None)
    return redirect(url_for("meetings.dashboard"))


class DevLoginSchema(Schema):
    email = fields.Email(required=True, validate=validate.Length(max=255))
    display_name = fields.String(required=True, validate=validate.Length(min=1, max=100))


_LOOPBACK = {"127.0.0.1", "::1"}


@auth_bp.route("/dev-login", methods=["POST"])
def dev_login():
    """Passwordless login for local development only (REQ-29).

    Disabled (404) unless DEV_LOGIN_ENABLED, which only DevelopmentConfig sets, and only
    reachable from the local machine even though the dev server listens on 0.0.0.0.
    """
    if not current_app.config["DEV_LOGIN_ENABLED"] or request.remote_addr not in _LOOPBACK:
        abort(404)

    try:
        data = DevLoginSchema().load({
            "email": request.form.get("email", "").strip().lower(),
            "display_name": request.form.get("display_name", "").strip(),
        })
    except ValidationError:
        flash("開發模式登入：請輸入有效的 Email 與名稱", "error")
        return redirect(url_for("auth.login"))

    user = User.query.filter_by(email=data["email"]).first()
    if user is None:
        user = User(email=data["email"], display_name=data["display_name"])
        db.session.add(user)
        db.session.commit()

    login_user(user)
    record_audit_event(actor_user_id=user.id, action="auth.dev_login", target_type="user",
                       target_id=user.id, metadata={"remote_addr": request.remote_addr})
    return redirect(url_for("meetings.dashboard"))


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))
