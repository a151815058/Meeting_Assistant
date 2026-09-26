import email
from datetime import datetime, timedelta, timezone
from email import policy

import pytest

from app import create_app
from app.config import ProductionConfig
from app.extensions import db as _db
from app.minutes import generator
from app.models.audit import AuditLog
from app.models.meeting import Meeting, Participant
from app.models.template import Minutes
from app.models.user import OAuthAccount, User
from app.notifications import mailer
from app.notifications.senders import MailError, MailSender, SendResult


class FakeSender(MailSender):
    backend = "fake"

    def __init__(self):
        self.sent = []
        self.failures = []  # MailErrors raised by the next calls, in order

    def send(self, mail):
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append(mail)
        return SendResult(backend=self.backend, message_id=f"msg-{len(self.sent)}")


@pytest.fixture(autouse=True)
def sender(app):
    mailer.reset_state()
    generator.reset_jobs()
    fake = FakeSender()
    app.extensions["mail_sender"] = fake
    yield fake
    mailer.reset_state()
    generator.reset_jobs()


def _login(client, app, email="org@example.com"):
    with app.app_context():
        user = User(email=email, display_name="王經理")
        _db.session.add(user)
        _db.session.commit()
        user_id = user.id
    with client.session_transaction() as sess:
        sess["_user_id"] = user_id
        sess["_fresh"] = True
    return user_id


def _meeting(app, user_id, *, participants=(("li@example.com", "李小姐"), ("chen@example.com", "陳先生")),
             minutes=True, platform="manual"):
    with app.app_context():
        meeting = Meeting(organizer_id=user_id, title="Q3 預算會議", status="minutes_ready", platform=platform)
        _db.session.add(meeting)
        _db.session.flush()
        for addr, name in participants:
            _db.session.add(Participant(meeting_id=meeting.id, email=addr, display_name=name))
        if minutes:
            _db.session.add(Minutes(meeting_id=meeting.id, content_markdown="# 會議記錄\n- 決議：通過",
                                    llm_provider="fake", llm_model="claude-opus-5"))
        _db.session.commit()
        return meeting.id


def _audits(app, action):
    with app.app_context():
        return [a.event_metadata for a in AuditLog.query.filter_by(action=action).order_by(AuditLog.created_at)]


def _minutes(app, meeting_id):
    with app.app_context():
        m = _db.session.get(Meeting, meeting_id)
        return m.status, m.minutes.status, m.minutes.sent_at


# --- TC-15: send minutes to all participants ----------------------------------------------------

def test_send_minutes_to_all_participants(client, app, sender):
    user_id = _login(client, app)
    meeting_id = _meeting(app, user_id, participants=[("li@example.com", "李小姐"), ("chen@example.com", "陳先生"),
                                                      ("org@example.com", "王經理")])

    page = client.get(f"/meetings/{meeting_id}/minutes").get_data(as_text=True)
    assert "寄送給與會者" in page and "共 2 位" in page and "寄出會議記錄" in page

    resp = client.post(f"/meetings/{meeting_id}/minutes/send", follow_redirects=True)

    assert "會議記錄已寄給 2 位與會者" in resp.get_data(as_text=True)
    mail = sender.sent[0]
    assert [r.email for r in mail.recipients] == ["chen@example.com", "li@example.com"]  # organizer excluded
    assert mail.sender.email == "org@example.com"
    assert mail.subject == "【會議記錄】Q3 預算會議" and mail.body.startswith("# 會議記錄\n- 決議：通過")
    meeting_status, minutes_status, sent_at = _minutes(app, meeting_id)
    assert (meeting_status, minutes_status) == ("sent", "sent") and sent_at is not None
    # TC-17: audit carries result and recipient list
    audit, = _audits(app, "mail.sent")
    assert audit["recipients"] == ["chen@example.com", "li@example.com"] and audit["count"] == 2
    assert audit["backend"] == "fake" and audit["message_id"] == "msg-1" and audit["attempts"] == 1


def test_sends_saved_edits(client, app, sender):
    user_id = _login(client, app)
    meeting_id = _meeting(app, user_id)
    client.post(f"/meetings/{meeting_id}/minutes", data={"content_markdown": "# 修改後的會議記錄"})

    client.post(f"/meetings/{meeting_id}/minutes/send")

    assert sender.sent[0].body.startswith("# 修改後的會議記錄")


# --- TC-16: recipients only from the Participant table ---------------------------------------------

def test_request_cannot_add_recipients(client, app, sender):
    user_id = _login(client, app)
    meeting_id = _meeting(app, user_id, participants=[("li@example.com", "李小姐")])

    client.post(f"/meetings/{meeting_id}/minutes/send",
                data={"to": "spam@evil.test", "recipients": "spam@evil.test", "email": "spam@evil.test"})

    assert [r.email for r in sender.sent[0].recipients] == ["li@example.com"]


@pytest.mark.parametrize("participants,minutes,expected", [
    ([], True, "沒有其他與會者"),
    ([("org@example.com", "王經理")], True, "沒有其他與會者"),
])
def test_send_rejected_without_recipients(client, app, sender, participants, minutes, expected):
    user_id = _login(client, app)
    meeting_id = _meeting(app, user_id, participants=participants, minutes=minutes)

    resp = client.post(f"/meetings/{meeting_id}/minutes/send", follow_redirects=True)

    assert expected in resp.get_data(as_text=True)
    assert sender.sent == [] and _audits(app, "mail.sent") == [] and _audits(app, "mail.send_failed") == []


def test_send_rejected_over_recipient_limit(client, app, sender):
    app.config["MAIL_MAX_RECIPIENTS"] = 1
    user_id = _login(client, app)
    meeting_id = _meeting(app, user_id)

    resp = client.post(f"/meetings/{meeting_id}/minutes/send", follow_redirects=True)
    assert "收件人超過上限" in resp.get_data(as_text=True) and sender.sent == []


def test_cannot_send_someone_elses_minutes_or_missing_minutes(client, app, sender):
    owner = _login(client, app, email="owner@example.com")
    theirs = _meeting(app, owner)
    me = _login(client, app, email="me@example.com")
    no_minutes = _meeting(app, me, minutes=False)

    assert client.post(f"/meetings/{theirs}/minutes/send").status_code == 404
    assert client.post(f"/meetings/{no_minutes}/minutes/send").status_code == 404
    assert sender.sent == []


def test_organizer_without_mail_account_cannot_send(client, app):
    app.extensions.pop("mail_sender")
    app.config["MAIL_OUTBOX_ENABLED"] = False
    user_id = _login(client, app)
    meeting_id = _meeting(app, user_id)

    assert "尚未授權寄信" in client.get(f"/meetings/{meeting_id}/minutes").get_data(as_text=True)
    resp = client.post(f"/meetings/{meeting_id}/minutes/send", follow_redirects=True)
    assert "尚未授權寄信" in resp.get_data(as_text=True)
    assert _audits(app, "mail.send_failed")[0]["error"] == "not_configured"


def test_sends_as_organizer_via_their_gmail_account(client, app, mocker):
    app.extensions.pop("mail_sender")
    user_id = _login(client, app)
    with app.app_context():
        account = OAuthAccount(user_id=user_id, provider="google", provider_account_id="g1",
                               scopes="openid https://www.googleapis.com/auth/gmail.send")
        account.refresh_token = "refresh"
        _db.session.add(account)
        _db.session.commit()
    meeting_id = _meeting(app, user_id)
    token = mocker.patch("app.notifications.mailer.get_valid_access_token", return_value="access-123")
    gmail = mocker.patch("app.notifications.mailer.GmailSender.send",
                         return_value=SendResult(backend="gmail", message_id="gm-9"))

    assert "Google 帳號（Gmail）" in client.get(f"/meetings/{meeting_id}/minutes").get_data(as_text=True)
    client.post(f"/meetings/{meeting_id}/minutes/send")

    assert token.call_args.args[0].provider_account_id == "g1"
    assert gmail.called
    assert _audits(app, "mail.sent")[0]["backend"] == "gmail"


# --- TC-17: failures are audited and can be retried ----------------------------------------------

def test_failure_is_audited_and_retry_succeeds(client, app, sender):
    sender.failures = [MailError("auth_failed", "401")]
    user_id = _login(client, app)
    meeting_id = _meeting(app, user_id)

    resp = client.post(f"/meetings/{meeting_id}/minutes/send", follow_redirects=True)

    page = resp.get_data(as_text=True)
    assert "寄信授權失效" in page and "上次寄送失敗" in page and "重新寄送" in page
    assert _minutes(app, meeting_id)[:2] == ("minutes_ready", "send_failed")
    failed, = _audits(app, "mail.send_failed")
    assert failed["error"] == "auth_failed" and failed["recipients"] == ["chen@example.com", "li@example.com"]

    client.post(f"/meetings/{meeting_id}/minutes/send")
    assert _minutes(app, meeting_id)[:2] == ("sent", "sent")
    assert len(sender.sent) == 1


def test_rate_limited_send_is_retried_automatically(client, app, sender):
    sender.failures = [MailError("rate_limited", retryable=True), MailError("provider_unavailable", retryable=True)]
    user_id = _login(client, app)
    meeting_id = _meeting(app, user_id)

    client.post(f"/meetings/{meeting_id}/minutes/send")

    assert len(sender.sent) == 1
    assert _audits(app, "mail.sent")[0]["attempts"] == 3


def test_retries_stop_at_limit_and_connection_errors_are_not_retried(client, app, sender):
    sender.failures = [MailError("rate_limited", retryable=True)] * 3
    user_id = _login(client, app)
    first = _meeting(app, user_id)
    client.post(f"/meetings/{first}/minutes/send")
    assert _audits(app, "mail.send_failed")[0]["attempts"] == 3

    sender.failures = [MailError("connection_failed")]
    second = _meeting(app, user_id)
    resp = client.post(f"/meetings/{second}/minutes/send", follow_redirects=True)
    assert _audits(app, "mail.send_failed")[1]["attempts"] == 1
    assert "信件可能未寄出" in resp.get_data(as_text=True)
    assert sender.sent == []


def test_resend_after_success_has_cooldown_and_failed_resend_keeps_sent_status(client, app, sender):
    user_id = _login(client, app)
    meeting_id = _meeting(app, user_id)
    client.post(f"/meetings/{meeting_id}/minutes/send")

    resp = client.post(f"/meetings/{meeting_id}/minutes/send", follow_redirects=True)
    assert "剛剛才寄出過" in resp.get_data(as_text=True) and len(sender.sent) == 1

    with app.app_context():
        _db.session.get(Meeting, meeting_id).minutes.sent_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        _db.session.commit()
    assert "再寄一次" in client.get(f"/meetings/{meeting_id}/minutes").get_data(as_text=True)
    sender.failures = [MailError("bad_request")]
    client.post(f"/meetings/{meeting_id}/minutes/send")
    assert _minutes(app, meeting_id)[:2] == ("sent", "sent")


# --- concurrency with generation / editing --------------------------------------------------------

def test_send_blocked_while_generating_or_already_sending(client, app, sender):
    user_id = _login(client, app)
    meeting_id = _meeting(app, user_id)

    generator._jobs[meeting_id] = generator.Job()
    resp = client.post(f"/meetings/{meeting_id}/minutes/send", follow_redirects=True)
    assert "完成後才能寄出" in resp.get_data(as_text=True)
    generator.reset_jobs()

    mailer._sending.add(meeting_id)
    resp = client.post(f"/meetings/{meeting_id}/minutes/send", follow_redirects=True)
    assert "寄送中" in resp.get_data(as_text=True)
    assert sender.sent == []


def test_edit_and_regenerate_blocked_while_sending(client, app, sender):
    user_id = _login(client, app)
    meeting_id = _meeting(app, user_id)
    mailer._sending.add(meeting_id)

    resp = client.post(f"/meetings/{meeting_id}/minutes", data={"content_markdown": "改"}, follow_redirects=True)
    assert "寄送中，暫時無法儲存" in resp.get_data(as_text=True)
    resp = client.post(f"/meetings/{meeting_id}/minutes/generate", follow_redirects=True)
    assert "寄送中" in resp.get_data(as_text=True)
    with app.app_context():
        assert _db.session.get(Meeting, meeting_id).minutes.content_markdown.startswith("# 會議記錄")


# --- development outbox --------------------------------------------------------------------------

def test_dev_outbox_writes_eml_when_no_mail_account(client, app, tmp_path):
    app.extensions.pop("mail_sender")
    app.config.update(MAIL_OUTBOX_ENABLED=True, MAIL_OUTBOX_DIR=str(tmp_path))
    user_id = _login(client, app)
    meeting_id = _meeting(app, user_id)

    assert "開發模式本機信箱" in client.get(f"/meetings/{meeting_id}/minutes").get_data(as_text=True)
    resp = client.post(f"/meetings/{meeting_id}/minutes/send", follow_redirects=True)

    assert "開發模式：信件未真的寄出" in resp.get_data(as_text=True)
    eml, = tmp_path.glob("*.eml")
    msg = email.message_from_bytes(eml.read_bytes(), policy=policy.default)
    assert [a.addr_spec for a in msg["To"].addresses] == ["chen@example.com", "li@example.com"]
    assert _audits(app, "mail.sent")[0]["backend"] == "outbox"


def test_outbox_cannot_be_enabled_in_production(monkeypatch):
    monkeypatch.setattr(ProductionConfig, "MAIL_OUTBOX_ENABLED", True)
    with pytest.raises(RuntimeError, match="MAIL_OUTBOX_ENABLED"):
        create_app("production")


# --- manual participants (feeds REQ-16) ----------------------------------------------------------

def test_organizer_adds_and_removes_participants(client, app):
    user_id = _login(client, app)
    meeting_id = _meeting(app, user_id, participants=[], minutes=False)

    client.post(f"/meetings/{meeting_id}/participants", data={"email": " New@Example.com ", "display_name": "新 同事"})
    page = client.get(f"/meetings/{meeting_id}").get_data(as_text=True)
    assert "new@example.com" in page and "新 同事" in page
    with app.app_context():
        participant = Participant.query.filter_by(meeting_id=meeting_id).one()
        pid = participant.id

    client.post(f"/meetings/{meeting_id}/participants/{pid}/delete")
    with app.app_context():
        assert Participant.query.filter_by(meeting_id=meeting_id).count() == 0
    assert _audits(app, "participant.added") == [{"email": "new@example.com"}]
    assert _audits(app, "participant.removed") == [{"email": "new@example.com"}]


@pytest.mark.parametrize("form,message", [
    ({"email": "not-an-email"}, "請輸入有效的 Email"),
    ({"email": "a@example.com\r\nBcc: x@evil.test"}, "請輸入有效的 Email"),
    ({"email": "li@example.com"}, "已在與會者名單中"),
    ({"email": "LI@example.com"}, "已在與會者名單中"),
    ({"email": "ok@example.com", "display_name": "x" * 256}, "請輸入有效的 Email"),
])
def test_participant_validation(client, app, form, message):
    user_id = _login(client, app)
    meeting_id = _meeting(app, user_id, participants=[("li@example.com", "李小姐")], minutes=False)

    resp = client.post(f"/meetings/{meeting_id}/participants", data=form, follow_redirects=True)

    assert message in resp.get_data(as_text=True)
    with app.app_context():
        assert Participant.query.filter_by(meeting_id=meeting_id).count() == 1


def test_participant_limit(client, app):
    app.config["MAIL_MAX_RECIPIENTS"] = 1
    user_id = _login(client, app)
    meeting_id = _meeting(app, user_id, participants=[("li@example.com", "李小姐")], minutes=False)

    resp = client.post(f"/meetings/{meeting_id}/participants", data={"email": "b@example.com"}, follow_redirects=True)
    assert "已達上限" in resp.get_data(as_text=True)


def test_participants_of_other_meetings_are_protected(client, app):
    owner = _login(client, app, email="owner@example.com")
    theirs = _meeting(app, owner, minutes=False)
    with app.app_context():
        their_pid = Participant.query.filter_by(meeting_id=theirs).first().id
    me = _login(client, app, email="me@example.com")
    mine = _meeting(app, me, participants=[], minutes=False)

    assert client.post(f"/meetings/{theirs}/participants", data={"email": "x@example.com"}).status_code == 404
    assert client.post(f"/meetings/{theirs}/participants/{their_pid}/delete").status_code == 404
    assert client.post(f"/meetings/{mine}/participants/{their_pid}/delete").status_code == 404  # wrong meeting
    with app.app_context():
        assert Participant.query.filter_by(meeting_id=theirs).count() == 2
