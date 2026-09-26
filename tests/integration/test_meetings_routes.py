from app.extensions import db as _db
from app.models.meeting import Meeting, Participant
from app.models.user import User


def _login(client, app, email="dave@example.com"):
    with app.app_context():
        user = User(email=email, display_name="Dave")
        _db.session.add(user)
        _db.session.commit()
        user_id = user.id
    with client.session_transaction() as sess:
        sess["_user_id"] = user_id
        sess["_fresh"] = True
    return user_id


def test_dashboard_requires_login(client):
    resp = client.get("/meetings/")
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]


def test_create_meeting(client, app):
    _login(client, app)

    resp = client.post("/meetings/new", data={"title": "Sprint Planning", "platform": "manual"})

    assert resp.status_code == 302
    with app.app_context():
        meeting = Meeting.query.filter_by(title="Sprint Planning").first()
        assert meeting is not None
        assert meeting.platform == "manual"


def test_sync_participants_calls_calendar_sync(client, app, mocker):
    user_id = _login(client, app)
    with app.app_context():
        meeting = Meeting(
            organizer_id=user_id, title="Standup", platform="google_meet", platform_event_id="evt-1"
        )
        _db.session.add(meeting)
        _db.session.commit()
        meeting_id = meeting.id

    fake_participant = Participant(meeting_id=meeting_id, email="eve@example.com", display_name="Eve")
    mocker.patch(
        "app.meetings.routes.sync_meeting_participants",
        return_value=[fake_participant],
    )

    resp = client.post(f"/meetings/{meeting_id}/sync-participants")

    assert resp.status_code == 302


def test_sync_participants_handles_missing_oauth_account(client, app, mocker):
    user_id = _login(client, app)
    with app.app_context():
        meeting = Meeting(
            organizer_id=user_id, title="Standup", platform="google_meet", platform_event_id="evt-1"
        )
        _db.session.add(meeting)
        _db.session.commit()
        meeting_id = meeting.id

    mocker.patch(
        "app.meetings.routes.sync_meeting_participants",
        side_effect=RuntimeError("Organizer has no linked google account"),
    )

    resp = client.post(f"/meetings/{meeting_id}/sync-participants", follow_redirects=True)

    assert resp.status_code == 200
    assert "同步失敗".encode() in resp.data
