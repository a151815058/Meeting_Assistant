"""Resolves a usable access token for an OAuthAccount, refreshing when needed.

Access tokens are short-lived and are intentionally NOT persisted to the DB;
only the encrypted refresh_token is. Every call here may hit the provider's
token endpoint, so callers should not call it in a tight loop.
"""
from datetime import datetime, timedelta, timezone

from flask import current_app

from app.auth import google_oauth, microsoft_oauth
from app.extensions import db
from app.models.user import OAuthAccount

_EXPIRY_SAFETY_MARGIN = timedelta(minutes=2)


def get_valid_access_token(account: OAuthAccount) -> str:
    now = datetime.now(timezone.utc)
    still_valid = (
        account.access_token_expires_at is not None
        and account.access_token_expires_at - _EXPIRY_SAFETY_MARGIN > now
    )
    if still_valid and getattr(account, "_cached_access_token", None):
        return account._cached_access_token  # noqa: SLF001

    if not account.refresh_token:
        raise RuntimeError(f"No refresh token stored for OAuthAccount {account.id}; user must re-consent")

    if account.provider == "google":
        result = google_oauth.refresh_access_token(
            current_app.config["GOOGLE_CLIENT_ID"],
            current_app.config["GOOGLE_CLIENT_SECRET"],
            account.refresh_token,
        )
        access_token = result["access_token"]
        account.access_token_expires_at = result["expiry"]
    elif account.provider == "microsoft":
        msal_app = microsoft_oauth.build_msal_app(
            current_app.config["MS_CLIENT_ID"],
            current_app.config["MS_CLIENT_SECRET"],
            current_app.config["MS_TENANT_ID"],
        )
        result = microsoft_oauth.refresh_access_token(msal_app, account.refresh_token)
        access_token = result["access_token"]
        if result.get("refresh_token"):
            account.refresh_token = result["refresh_token"]
        if result.get("expires_in"):
            account.access_token_expires_at = now + timedelta(seconds=result["expires_in"])
    else:
        raise ValueError(f"Unknown OAuth provider: {account.provider}")

    db.session.commit()
    account._cached_access_token = access_token  # noqa: SLF001  (process-local cache only)
    return access_token
