"""Thin wrapper around msal so routes.py stays testable."""
import msal

# MSAL rejects the reserved scopes (openid, profile, offline_access) with a ValueError and
# always adds them itself, so a refresh token is still returned without listing offline_access.
MS_SCOPES = ["User.Read", "Calendars.Read", "Mail.Send"]


def build_msal_app(client_id: str, client_secret: str, tenant_id: str) -> msal.ConfidentialClientApplication:
    authority = f"https://login.microsoftonline.com/{tenant_id}"
    return msal.ConfidentialClientApplication(
        client_id=client_id, client_credential=client_secret, authority=authority
    )


def get_authorization_url(app: msal.ConfidentialClientApplication, redirect_uri: str, state: str) -> str:
    return app.get_authorization_request_url(MS_SCOPES, redirect_uri=redirect_uri, state=state)


def exchange_code(app: msal.ConfidentialClientApplication, code: str, redirect_uri: str) -> dict:
    result = app.acquire_token_by_authorization_code(code, scopes=MS_SCOPES, redirect_uri=redirect_uri)
    if "error" in result:
        raise RuntimeError(f"Microsoft OAuth error: {result.get('error_description', result['error'])}")
    return {
        "access_token": result["access_token"],
        "refresh_token": result.get("refresh_token"),
        "expires_in": result.get("expires_in"),
        "scopes": result.get("scope", "").split(),
    }


def refresh_access_token(app: msal.ConfidentialClientApplication, refresh_token: str) -> dict:
    result = app.acquire_token_by_refresh_token(refresh_token, scopes=MS_SCOPES)
    if "error" in result:
        raise RuntimeError(f"Microsoft token refresh error: {result.get('error_description', result['error'])}")
    return {
        "access_token": result["access_token"],
        "refresh_token": result.get("refresh_token", refresh_token),
        "expires_in": result.get("expires_in"),
    }


def get_userinfo(access_token: str) -> dict:
    import requests

    resp = requests.get(
        "https://graph.microsoft.com/v1.0/me",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    email = data.get("mail") or data.get("userPrincipalName")
    return {"email": email, "name": data.get("displayName", email), "id": data["id"]}
