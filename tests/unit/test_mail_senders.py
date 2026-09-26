import base64
import email
from email import policy
from types import SimpleNamespace

import pytest
import requests

from app.notifications import mailer
from app.notifications.senders import (
    GMAIL_SEND_URL, GRAPH_SEND_URL, GmailSender, GraphSender, MailError, OutboxSender, OutgoingMail, Recipient,
)

MAIL = OutgoingMail(
    sender=Recipient("org@example.com", "王經理"),
    recipients=(Recipient("li@example.com", "李小姐"), Recipient("chen@example.com", "chen@example.com")),
    subject="【會議記錄】Q3 預算會議",
    body="# 會議記錄\n- 決議：通過",
)


def _resp(status=200, payload=None, text=""):
    return SimpleNamespace(status_code=status, json=lambda: payload or {}, text=text)


def _parse(raw: bytes):
    return email.message_from_bytes(raw, policy=policy.default)


# --- TC-15: transports ----------------------------------------------------------------------

def test_gmail_sender_posts_base64url_mime(mocker):
    post = mocker.patch("app.notifications.senders.requests.post", return_value=_resp(200, {"id": "gm-1"}))

    result = GmailSender("tok").send(MAIL)

    url, kwargs = post.call_args.args[0], post.call_args.kwargs
    assert url == GMAIL_SEND_URL and kwargs["headers"] == {"Authorization": "Bearer tok"} and kwargs["timeout"]
    msg = _parse(base64.urlsafe_b64decode(kwargs["json"]["raw"]))
    assert msg["Subject"] == "【會議記錄】Q3 預算會議"
    assert msg["From"] == "王經理 <org@example.com>"
    assert msg["To"] == "李小姐 <li@example.com>, chen@example.com"
    assert msg.get_content().strip() == "# 會議記錄\n- 決議：通過"
    assert (result.backend, result.message_id) == ("gmail", "gm-1")


def test_graph_sender_posts_send_mail_json(mocker):
    post = mocker.patch("app.notifications.senders.requests.post", return_value=_resp(202))

    result = GraphSender("tok").send(MAIL)

    assert post.call_args.args[0] == GRAPH_SEND_URL
    message = post.call_args.kwargs["json"]["message"]
    assert message["subject"] == MAIL.subject
    assert message["body"] == {"contentType": "Text", "content": MAIL.body}
    assert [r["emailAddress"]["address"] for r in message["toRecipients"]] == ["li@example.com", "chen@example.com"]
    assert post.call_args.kwargs["json"]["saveToSentItems"] is True
    assert result.backend == "graph"


@pytest.mark.parametrize("status,code,retryable", [
    (401, "auth_failed", False), (403, "auth_failed", False), (429, "rate_limited", True),
    (503, "provider_unavailable", True), (500, "provider_unavailable", False), (400, "bad_request", False),
])
def test_http_errors_are_mapped(mocker, status, code, retryable):
    mocker.patch("app.notifications.senders.requests.post", return_value=_resp(status, text="nope"))

    with pytest.raises(MailError) as info:
        GmailSender("tok").send(MAIL)
    assert (info.value.code, info.value.retryable) == (code, retryable)


def test_connection_error_is_not_retryable(mocker):
    # The request may have reached the provider, so an automatic retry could send twice.
    mocker.patch("app.notifications.senders.requests.post", side_effect=requests.ConnectionError("reset"))

    with pytest.raises(MailError) as info:
        GraphSender("tok").send(MAIL)
    assert (info.value.code, info.value.retryable) == ("connection_failed", False)


def test_outbox_sender_writes_eml(tmp_path):
    result = OutboxSender(str(tmp_path / "outbox")).send(MAIL)

    assert result.backend == "outbox" and result.outbox_path.endswith(".eml")
    msg = _parse(open(result.outbox_path, "rb").read())
    assert msg["Subject"] == MAIL.subject and "決議：通過" in msg.get_content()


# --- TC-16 / REQ-20: recipients and header safety --------------------------------------------

def _meeting(participants, title="Q3 預算會議"):
    organizer = SimpleNamespace(email="Org@Example.com", display_name="王經理", oauth_accounts=[])
    return SimpleNamespace(
        title=title, scheduled_start=None, organizer=organizer, platform="manual",
        participants=[SimpleNamespace(email=e, display_name=n) for e, n in participants],
        minutes=SimpleNamespace(content_markdown="# 會議記錄\n"),
    )


def test_recipients_exclude_organizer_dedupe_and_skip_invalid():
    meeting = _meeting([("org@example.com", "我"), ("li@example.com", "李小姐"), ("LI@example.com", "重複"),
                        ("not-an-email", "x"), ("chen@example.com", None)])

    recipients, skipped = mailer.recipients_for(meeting)

    assert [r.email for r in recipients] == ["chen@example.com", "li@example.com"]
    assert recipients[0].name == "chen@example.com"
    assert skipped == ["not-an-email"]


def test_header_injection_is_neutralised():
    meeting = _meeting([("li@example.com", "李小姐\r\nBcc: evil@example.com")],
                       title="預算\r\nBcc: evil@example.com")
    recipients, _ = mailer.recipients_for(meeting)

    mail = mailer.compose(meeting, recipients)
    raw = mail.to_mime().as_bytes()
    msg = _parse(raw)

    assert "\n" not in mail.subject and "\n" not in recipients[0].name
    assert msg["Bcc"] is None
    assert [a.addr_spec for a in msg["To"].addresses] == ["li@example.com"]
    assert "由 AI 依會議逐字稿產生" in msg.get_content()


def test_send_account_needs_send_scope_and_prefers_meeting_platform():
    google = SimpleNamespace(provider="google", refresh_token="r",
                             scopes="openid https://www.googleapis.com/auth/gmail.send",
                             has_scope=lambda s: s == mailer.GMAIL_SEND_SCOPE)
    ms = SimpleNamespace(provider="microsoft", refresh_token="r", scopes="User.Read https://graph.microsoft.com/Mail.Send",
                         has_scope=lambda s: False)
    ms_no_send = SimpleNamespace(provider="microsoft", refresh_token="r", scopes="User.Read", has_scope=lambda s: False)
    google_no_token = SimpleNamespace(provider="google", refresh_token=None, scopes="", has_scope=lambda s: True)

    user = SimpleNamespace(oauth_accounts=[google, ms])
    assert mailer.send_account(user, "teams") is ms
    assert mailer.send_account(user, "google_meet") is google
    assert mailer.send_account(user, "manual") is google
    assert mailer.send_account(SimpleNamespace(oauth_accounts=[ms_no_send, google_no_token]), "manual") is None
