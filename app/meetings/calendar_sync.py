"""Pulls the attendee list for a scheduled meeting from Google Calendar or
Microsoft Graph and upserts it into the Participant table (REQ-04).

Known limitation (see docs/requirements/requirements_matrix.md RISK-01):
this only gives us the *invited* roster, not a guarantee of who actually
joined or who is speaking at any instant. Speaker attribution for the MVP
comes from diarization in app/transcription, not from this module.
"""
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
import requests

from app.auth.token_service import get_valid_access_token
from app.background import run_blocking
from app.extensions import db
from app.models.meeting import Meeting, Participant
from app.models.user import OAuthAccount, User

logger = logging.getLogger(__name__)

GOOGLE_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
PLATFORM_BY_PROVIDER = {"google": "google_meet", "microsoft": "teams"}
PROVIDER_LABELS = {"google": "Google 行事曆", "microsoft": "Outlook 行事曆"}


def _fetch_google_event(access_token: str, event_id: str) -> dict:
    from google.oauth2.credentials import Credentials

    creds = Credentials(token=access_token)
    service = build("calendar", "v3", credentials=creds)
    return service.events().get(calendarId="primary", eventId=event_id).execute()


def _extract_google_attendees(event: dict) -> list[dict]:
    organizer_email = (event.get("organizer") or {}).get("email")
    attendees = []
    for a in event.get("attendees", []):
        attendees.append(
            {
                "email": a["email"],
                "display_name": a.get("displayName") or a["email"],
                "response_status": a.get("responseStatus", "needsAction"),
                "is_organizer": a["email"] == organizer_email,
            }
        )
    return attendees


def _fetch_microsoft_event(access_token: str, event_id: str) -> dict:
    resp = requests.get(
        f"https://graph.microsoft.com/v1.0/me/events/{requests.utils.quote(event_id, safe='')}",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def _extract_microsoft_attendees(event: dict) -> list[dict]:
    organizer_email = ((event.get("organizer") or {}).get("emailAddress") or {}).get("address")
    attendees = []
    for a in event.get("attendees", []):
        email_address = a.get("emailAddress", {})
        email = email_address.get("address")
        if not email:
            continue
        status = (a.get("status") or {}).get("response", "none")
        attendees.append(
            {
                "email": email,
                "display_name": email_address.get("name") or email,
                "response_status": status,
                "is_organizer": email == organizer_email,
            }
        )
    return attendees


def sync_meeting_participants(meeting: Meeting) -> list[Participant]:
    if meeting.platform not in ("google_meet", "teams"):
        raise ValueError(f"Cannot sync participants for platform={meeting.platform!r}")
    if not meeting.platform_event_id:
        raise ValueError("Meeting has no platform_event_id to sync from")

    provider = "google" if meeting.platform == "google_meet" else "microsoft"
    account = OAuthAccount.query.filter_by(user_id=meeting.organizer_id, provider=provider).first()
    if account is None:
        raise RuntimeError(f"Organizer has no linked {provider} account")

    access_token = get_valid_access_token(account)

    if meeting.platform == "google_meet":
        event = _fetch_google_event(access_token, meeting.platform_event_id)
        attendees = _extract_google_attendees(event)
    else:
        event = _fetch_microsoft_event(access_token, meeting.platform_event_id)
        attendees = _extract_microsoft_attendees(event)

    existing_by_email = {p.email: p for p in meeting.participants}
    result: list[Participant] = []
    for a in attendees:
        participant = existing_by_email.get(a["email"])
        if participant is None:
            participant = Participant(meeting_id=meeting.id, email=a["email"])
            db.session.add(participant)
        participant.display_name = a["display_name"]
        participant.response_status = a["response_status"]
        participant.is_organizer = a["is_organizer"]
        result.append(participant)

    db.session.commit()
    return result


# --- listing upcoming events for the "new meeting" dropdown -----------------------------------

@dataclass(frozen=True)
class CalendarEvent:
    provider: str  # "google" | "microsoft"
    event_id: str
    title: str
    start: datetime  # timezone-aware
    end: datetime | None
    all_day: bool = False

    @property
    def platform(self) -> str:
        return PLATFORM_BY_PROVIDER[self.provider]


def calendar_accounts(user: User) -> list[OAuthAccount]:
    """The user's linked accounts that were granted calendar read access."""
    def can_read(account):
        if account.provider == "google":
            return account.has_scope(GOOGLE_CALENDAR_SCOPE)
        return any(s.lower().endswith("calendars.read") for s in (account.scopes or "").split())

    return sorted((a for a in user.oauth_accounts if a.refresh_token and can_read(a)), key=lambda a: a.provider)


def _parse_google_time(value: dict, tz: ZoneInfo) -> tuple[datetime, bool]:
    if value.get("dateTime"):
        return datetime.fromisoformat(value["dateTime"]), False
    return datetime.combine(date.fromisoformat(value["date"]), time(0), tzinfo=tz), True  # all-day


def _parse_microsoft_time(value: dict, tz: ZoneInfo) -> datetime:
    # Requested with Prefer: outlook.timezone="UTC"; Graph returns 7 fractional digits and no offset.
    naive = datetime.fromisoformat(value["dateTime"][:26])
    return naive.replace(tzinfo=timezone.utc) if value.get("timeZone", "UTC") == "UTC" else naive.replace(tzinfo=tz)


def parse_google_event(item: dict, tz: ZoneInfo) -> CalendarEvent:
    start, all_day = _parse_google_time(item["start"], tz)
    end = _parse_google_time(item["end"], tz)[0] if item.get("end") else None
    return CalendarEvent("google", item["id"], (item.get("summary") or "").strip() or "（無標題）", start, end, all_day)


def parse_microsoft_event(item: dict, tz: ZoneInfo) -> CalendarEvent:
    start = _parse_microsoft_time(item["start"], tz)
    end = _parse_microsoft_time(item["end"], tz) if item.get("end") else None
    return CalendarEvent("microsoft", item["id"], (item.get("subject") or "").strip() or "（無標題）", start, end,
                         bool(item.get("isAllDay")))


def _list_google(access_token: str, time_min: datetime, time_max: datetime, limit: int) -> list[dict]:
    from google.oauth2.credentials import Credentials

    service = build("calendar", "v3", credentials=Credentials(token=access_token))
    result = service.events().list(
        calendarId="primary", timeMin=time_min.isoformat(), timeMax=time_max.isoformat(),
        singleEvents=True, orderBy="startTime", maxResults=limit,
        fields="items(id,summary,start,end,status)",
    ).execute()
    return [e for e in result.get("items", []) if e.get("status") != "cancelled"]


def _list_microsoft(access_token: str, time_min: datetime, time_max: datetime, limit: int) -> list[dict]:
    resp = requests.get(
        "https://graph.microsoft.com/v1.0/me/calendarView",
        params={
            "startDateTime": time_min.isoformat(), "endDateTime": time_max.isoformat(),
            "$orderby": "start/dateTime", "$top": limit,
            "$select": "id,subject,start,end,isAllDay,isCancelled",
        },
        headers={"Authorization": f"Bearer {access_token}", "Prefer": 'outlook.timezone="UTC"'},
        timeout=10,
    )
    resp.raise_for_status()
    return [e for e in resp.json().get("value", []) if not e.get("isCancelled")]


def list_upcoming_events(app, user: User) -> tuple[list[CalendarEvent], list[str]]:
    """Events from all of the user's calendars, soonest first, plus a message per calendar
    that could not be read (so one broken account does not hide the others)."""
    cfg = app.config
    tz = ZoneInfo(cfg["DISPLAY_TIMEZONE"])
    now = datetime.now(timezone.utc)
    time_min = now - timedelta(hours=cfg["CALENDAR_LOOKBACK_HOURS"])
    time_max = now + timedelta(days=cfg["CALENDAR_LOOKAHEAD_DAYS"])
    limit = cfg["CALENDAR_MAX_EVENTS"]

    events, errors = [], []
    for account in calendar_accounts(user):
        try:
            token = get_valid_access_token(account)
            if account.provider == "google":
                items = run_blocking(_list_google, token, time_min, time_max, limit)
                events += [parse_google_event(i, tz) for i in items]
            else:
                items = run_blocking(_list_microsoft, token, time_min, time_max, limit)
                events += [parse_microsoft_event(i, tz) for i in items]
        except Exception:  # expired consent, API error, unexpected payload
            logger.exception("listing %s calendar events failed for user %s", account.provider, user.id)
            errors.append(f"無法讀取{PROVIDER_LABELS[account.provider]}，請重新登入該帳號後再試")
    events.sort(key=lambda e: e.start)
    return events[:limit], errors


def fetch_event(app, user: User, platform: str, event_id: str) -> CalendarEvent:
    """Re-read one event from the provider so title/times come from the calendar, not the form."""
    provider = {v: k for k, v in PLATFORM_BY_PROVIDER.items()}[platform]
    account = next((a for a in calendar_accounts(user) if a.provider == provider), None)
    if account is None:
        raise RuntimeError(f"尚未連結可讀取行事曆的 {PROVIDER_LABELS[provider]} 帳號")
    tz = ZoneInfo(app.config["DISPLAY_TIMEZONE"])
    token = get_valid_access_token(account)
    if provider == "google":
        return parse_google_event(run_blocking(_fetch_google_event, token, event_id), tz)
    return parse_microsoft_event(run_blocking(_fetch_microsoft_event_utc, token, event_id), tz)


def _fetch_microsoft_event_utc(access_token: str, event_id: str) -> dict:
    resp = requests.get(
        f"https://graph.microsoft.com/v1.0/me/events/{requests.utils.quote(event_id, safe='')}",
        params={"$select": "id,subject,start,end,isAllDay"},
        headers={"Authorization": f"Bearer {access_token}", "Prefer": 'outlook.timezone="UTC"'},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()
