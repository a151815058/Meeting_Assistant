"""Thin wrapper around google-auth-oauthlib so routes.py stays testable.

Every function here is a seam that unit tests mock instead of hitting the
real Google endpoints.
"""
import os

from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

GOOGLE_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

# Google's consent screen lets users untick individual scopes (e.g. Gmail send). oauthlib
# would then raise ``Warning: Scope has changed`` and the callback would crash; accept the
# granted subset instead. Features check the scopes actually granted (OAuthAccount.has_scope).
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")


def build_flow(client_id: str, client_secret: str, redirect_uri: str) -> Flow:
    client_config = {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",  # nosec B105 - endpoint URL, not a credential
            "redirect_uris": [redirect_uri],
        }
    }
    flow = Flow.from_client_config(client_config, scopes=GOOGLE_SCOPES)
    flow.redirect_uri = redirect_uri
    return flow


def get_authorization_url(flow: Flow) -> tuple[str, str, str]:
    """Returns (url, state, code_verifier). The flow generates a PKCE code_verifier here; the
    callback runs on a new Flow, so the caller must keep the verifier and pass it to exchange_code.
    """
    url, state = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")
    return url, state, flow.code_verifier


def exchange_code(flow: Flow, authorization_response_url: str, code_verifier: str | None) -> dict:
    """Exchanges the OAuth code for tokens. Returns a dict with keys:
    access_token, refresh_token, expiry, scopes.
    """
    flow.code_verifier = code_verifier
    flow.fetch_token(authorization_response=authorization_response_url)
    creds = flow.credentials
    return {
        "access_token": creds.token,
        "refresh_token": creds.refresh_token,
        "expiry": creds.expiry,
        "scopes": creds.scopes or [],
    }


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> dict:
    """Exchanges a stored refresh_token for a fresh access_token.

    Returns {"access_token": ..., "expiry": datetime}.
    """
    from google.auth.transport.requests import Request as GoogleAuthRequest
    from google.oauth2.credentials import Credentials

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",  # nosec B106 - endpoint URL, not a credential
        client_id=client_id,
        client_secret=client_secret,
    )
    creds.refresh(GoogleAuthRequest())
    return {"access_token": creds.token, "expiry": creds.expiry}


def get_userinfo(access_token: str) -> dict:
    """Returns {"email": ..., "name": ..., "id": ...} via the OAuth2 userinfo endpoint."""
    from google.oauth2.credentials import Credentials

    creds = Credentials(token=access_token)
    service = build("oauth2", "v2", credentials=creds)
    info = service.userinfo().get().execute()
    return {"email": info["email"], "name": info.get("name", info["email"]), "id": info["id"]}
