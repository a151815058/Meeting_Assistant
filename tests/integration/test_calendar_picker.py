from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.extensions import db as _db
from app.meetings import calendar_sync
from app.meetings.calendar_sync import CalendarEvent, parse_google_event, parse_microsoft_event
from app.models.meeting import Meeting, Participant
from app.models.user import OAuthAccount, User

TPE = ZoneInfo("Asia/Taipei")
GOOGLE_SCOPES = "openid https://www.googleapis.com/auth/calendar.readonly https://www.googleapis.com/auth/gmail.send"

EVENT = CalendarEvent("google", "evt_20260925", "Q3 預算會議",
                      datetime(2026, 9, 25, 14, 0, tzinfo=TPE), datetime(2026, 9, 25, 15, 0, tzinfo=TPE))


def _login(client, app, *, google=True, microsoft=False, email="org@example.com"):
    with app.app_context():
        user = User(email=email, display_name="王經理")
        _db.session.add(user)
        _db.session.flush()
        for provider, scopes, on in (("google", GOOGLE_SCOPES, google),
                                     ("microsoft", "User.Read Calendars.Read Mail.Send", microsoft)):
            if on:
                account = OAuthAccount(user_id=user.id, provider=provider, provider_account_id=f"{provider}-1",
                                       scopes=scopes)
                account.refresh_token = "rt"
                _db.session.add(account)
        _db.session.commit()
        user_id = user.id
    with client.session_transaction() as sess:
        sess["_user_id"] = user_id
        sess["_fresh"] = True
    return user_id


@pytest.fixture()
def calendar(mocker):
    """Stubs the calendar API layer; the route logic and templates run for real."""
    listing = mocker.patch("app.meetings.calendar_sync.list_upcoming_events", return_value=([EVENT], []))
    fetch = mocker.patch("app.meetings.calendar_sync.fetch_event", return_value=EVENT)

    def fake_sync(meeting):
        p = Participant(meeting_id=meeting.id, email="li@example.com", display_name="李小姐")
        _db.session.add(p)
        _db.session.commit()
        return [p]

    sync = mocker.patch("app.meetings.routes.sync_meeting_participants", side_effect=fake_sync)
    return SimpleNamespace(listing=listing, fetch=fetch, sync=sync)


# --- TC-40: pick the meeting from a calendar dropdown -------------------------------------------

def test_new_meeting_page_lists_calendar_events(client, app, calendar):
    _login(client, app)

    page = client.get("/meetings/new").get_data(as_text=True)

    assert 'name="calendar_event"' in page and "Google 行事曆" in page
    assert 'value="google_meet|evt_20260925"' in page and 'data-title="Q3 預算會議"' in page
    assert "09/25（五）14:00–15:00" in page
    assert "meeting_create.js" in page and "onchange" not in page  # no inline JS (REQ-34)


def test_create_from_calendar_event_fills_details_and_syncs_participants(client, app, calendar):
    user_id = _login(client, app)

    resp = client.post("/meetings/new", data={"calendar_event": "google_meet|evt_20260925", "title": ""},
                       follow_redirects=True)

    assert "從行事曆同步 1 位與會者" in resp.get_data(as_text=True)
    assert "2026-09-25（五）14:00–15:00" in resp.get_data(as_text=True)
    assert calendar.fetch.call_args.args[2:] == ("google_meet", "evt_20260925")
    with app.app_context():
        meeting = Meeting.query.filter_by(organizer_id=user_id).one()
        assert (meeting.title, meeting.platform, meeting.platform_event_id) == ("Q3 預算會議", "google_meet", "evt_20260925")
        assert meeting.scheduled_start == datetime(2026, 9, 25, 6, 0, tzinfo=timezone.utc)
        assert meeting.scheduled_end == datetime(2026, 9, 25, 7, 0, tzinfo=timezone.utc)
        assert [p.email for p in meeting.participants] == ["li@example.com"]


def test_typed_title_overrides_calendar_title(client, app, calendar):
    user_id = _login(client, app)
    client.post("/meetings/new", data={"calendar_event": "google_meet|evt_20260925", "title": "  預算  會議 (內部) "})
    with app.app_context():
        assert Meeting.query.filter_by(organizer_id=user_id).one().title == "預算 會議 (內部)"


def test_same_calendar_event_is_not_created_twice(client, app, calendar):
    user_id = _login(client, app)
    client.post("/meetings/new", data={"calendar_event": "google_meet|evt_20260925"})

    assert "（已建立）" in client.get("/meetings/new").get_data(as_text=True)
    resp = client.post("/meetings/new", data={"calendar_event": "google_meet|evt_20260925"}, follow_redirects=True)

    assert "已經建立過" in resp.get_data(as_text=True)
    with app.app_context():
        assert Meeting.query.filter_by(organizer_id=user_id).count() == 1


def test_participant_sync_failure_still_creates_meeting(client, app, calendar):
    calendar.sync.side_effect = RuntimeError("403")
    user_id = _login(client, app)

    resp = client.post("/meetings/new", data={"calendar_event": "google_meet|evt_20260925"}, follow_redirects=True)

    assert "同步與會者失敗" in resp.get_data(as_text=True)
    with app.app_context():
        assert Meeting.query.filter_by(organizer_id=user_id).count() == 1


@pytest.mark.parametrize("choice", ["manual|x", "zoom|evt", "google_meet|", "google_meet|a b", "nonsense"])
def test_invalid_calendar_choice_is_rejected(client, app, calendar, choice):
    user_id = _login(client, app)

    resp = client.post("/meetings/new", data={"calendar_event": choice})

    assert resp.status_code == 400 and "選擇的行事曆會議無效" in resp.get_data(as_text=True)
    assert not calendar.fetch.called
    with app.app_context():
        assert Meeting.query.filter_by(organizer_id=user_id).count() == 0


def test_event_that_cannot_be_read_is_rejected(client, app, calendar):
    calendar.fetch.side_effect = RuntimeError("404 not found")  # e.g. someone else's event id
    user_id = _login(client, app)

    resp = client.post("/meetings/new", data={"calendar_event": "google_meet|not-mine"})

    assert resp.status_code == 400 and "無法讀取這場行事曆會議" in resp.get_data(as_text=True)
    with app.app_context():
        assert Meeting.query.filter_by(organizer_id=user_id).count() == 0


def test_calendar_errors_are_shown_without_breaking_the_page(client, app, calendar):
    calendar.listing.return_value = ([], ["無法讀取Google 行事曆，請重新登入該帳號後再試"])
    _login(client, app)

    page = client.get("/meetings/new").get_data(as_text=True)
    assert "無法讀取Google 行事曆" in page and 'name="title"' in page


def test_without_calendar_account_the_form_stays_manual(client, app, calendar):
    user_id = _login(client, app, google=False)

    page = client.get("/meetings/new").get_data(as_text=True)
    assert 'name="calendar_event"' not in page and "登入後，可直接從行事曆選擇會議" in page
    assert not calendar.listing.called

    client.post("/meetings/new", data={"title": "手動會議", "platform": "manual"})
    with app.app_context():
        assert Meeting.query.filter_by(organizer_id=user_id).one().platform == "manual"


@pytest.mark.parametrize("form,field", [
    ({"title": "", "platform": "manual"}, "標題"),
    ({"title": "x" * 501, "platform": "manual"}, "標題"),
    ({"title": "ok", "platform": "zoom"}, "平台"),
    ({"title": "ok", "platform": "teams", "platform_event_id": "a\nb"}, "事件 ID"),
])
def test_manual_meeting_input_is_validated(client, app, calendar, form, field):
    user_id = _login(client, app, google=False)

    resp = client.post("/meetings/new", data=form)

    assert resp.status_code == 400 and f"{field}格式不正確" in resp.get_data(as_text=True)
    with app.app_context():
        assert Meeting.query.filter_by(organizer_id=user_id).count() == 0


# --- calendar API parsing and account selection -------------------------------------------------

def test_parse_google_timed_and_all_day_events():
    timed = parse_google_event({"id": "a", "summary": " 週會 ", "start": {"dateTime": "2026-09-25T14:00:00+08:00"},
                                "end": {"dateTime": "2026-09-25T15:00:00+08:00"}}, TPE)
    all_day = parse_google_event({"id": "b", "start": {"date": "2026-09-26"}, "end": {"date": "2026-09-27"}}, TPE)

    assert (timed.title, timed.start.astimezone(timezone.utc).hour, timed.all_day) == ("週會", 6, False)
    assert (all_day.title, all_day.all_day, all_day.start) == ("（無標題）", True, datetime(2026, 9, 26, tzinfo=TPE))
    assert timed.platform == "google_meet"


def test_parse_microsoft_event_with_seven_digit_fraction():
    e = parse_microsoft_event({"id": "AAMk/x=", "subject": "Sprint", "isAllDay": False,
                               "start": {"dateTime": "2026-09-25T06:00:00.0000000", "timeZone": "UTC"},
                               "end": {"dateTime": "2026-09-25T07:30:00.0000000", "timeZone": "UTC"}}, TPE)

    assert e.start == datetime(2026, 9, 25, 6, 0, tzinfo=timezone.utc) and e.end.minute == 30
    assert e.platform == "teams"


def test_list_merges_calendars_sorted_and_reports_failed_account(app, mocker):
    user = SimpleNamespace(id="u1", oauth_accounts=[
        SimpleNamespace(provider="google", refresh_token="rt", scopes=GOOGLE_SCOPES,
                        has_scope=lambda s: s in GOOGLE_SCOPES.split()),
        SimpleNamespace(provider="microsoft", refresh_token="rt", scopes="Calendars.Read", has_scope=lambda s: False),
    ])
    mocker.patch("app.meetings.calendar_sync.get_valid_access_token", return_value="tok")
    mocker.patch("app.meetings.calendar_sync._list_google", return_value=[
        {"id": "g2", "summary": "晚", "start": {"dateTime": "2026-09-25T18:00:00+08:00"}, "end": {}},
        {"id": "g1", "summary": "早", "start": {"dateTime": "2026-09-25T09:00:00+08:00"}},
    ])
    mocker.patch("app.meetings.calendar_sync._list_microsoft", return_value=[
        {"id": "m1", "subject": "中", "start": {"dateTime": "2026-09-25T04:00:00.0000000", "timeZone": "UTC"}},
    ])

    events, errors = calendar_sync.list_upcoming_events(app, user)
    assert [e.event_id for e in events] == ["g1", "m1", "g2"] and errors == []

    mocker.patch("app.meetings.calendar_sync._list_microsoft", side_effect=RuntimeError("401"))
    events, errors = calendar_sync.list_upcoming_events(app, user)
    assert [e.event_id for e in events] == ["g1", "g2"] and errors == ["無法讀取Outlook 行事曆，請重新登入該帳號後再試"]


def test_accounts_without_calendar_scope_are_skipped():
    user = SimpleNamespace(oauth_accounts=[
        SimpleNamespace(provider="google", refresh_token="rt", scopes="openid", has_scope=lambda s: False),
        SimpleNamespace(provider="microsoft", refresh_token=None, scopes="Calendars.Read", has_scope=lambda s: False),
    ])
    assert calendar_sync.calendar_accounts(user) == []


def test_microsoft_event_id_is_url_encoded(mocker):
    get = mocker.patch("app.meetings.calendar_sync.requests.get")
    calendar_sync._fetch_microsoft_event("tok", "../../users/ceo@example.com/messages")
    assert get.call_args.args[0] == \
        "https://graph.microsoft.com/v1.0/me/events/..%2F..%2Fusers%2Fceo%40example.com%2Fmessages"
