"""Mail transports (REQ-15, REQ-16): Gmail API, Microsoft Graph, and a dev-only local outbox.

Senders only talk HTTP (or write a file) and need no Flask app context, so ``send`` can run
in a real OS thread (``app.background.run_blocking``). Access tokens are resolved by the
caller beforehand.
"""
import base64
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import make_msgid

import requests

GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
GRAPH_SEND_URL = "https://graph.microsoft.com/v1.0/me/sendMail"
_TIMEOUT_SECONDS = 30


class MailError(Exception):
    """``retryable`` = the provider definitely rejected the request (429/503), so sending it
    again cannot deliver a duplicate. Connection errors are not retried automatically: the
    message may already have been sent."""

    def __init__(self, code: str, detail: str = "", *, retryable: bool = False):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail
        self.retryable = retryable


@dataclass(frozen=True)
class Recipient:
    email: str
    name: str


@dataclass(frozen=True)
class Attachment:
    filename: str
    mimetype: str
    data: bytes


@dataclass(frozen=True)
class OutgoingMail:
    sender: Recipient
    recipients: tuple[Recipient, ...]
    subject: str
    body: str
    attachments: tuple[Attachment, ...] = ()

    def to_mime(self) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = _address(self.sender)
        msg["To"] = [_address(r) for r in self.recipients]
        msg["Subject"] = self.subject
        msg["Message-ID"] = make_msgid(domain=self.sender.email.rsplit("@", 1)[-1])
        msg.set_content(self.body, charset="utf-8")
        for a in self.attachments:  # non-ASCII filenames are RFC 2231-encoded by the email package
            maintype, _, subtype = a.mimetype.partition("/")
            msg.add_attachment(a.data, maintype=maintype, subtype=subtype, filename=a.filename)
        return msg


@dataclass(frozen=True)
class SendResult:
    backend: str
    message_id: str | None = None
    outbox_path: str | None = None


def _address(r: Recipient) -> Address:
    local, _, domain = r.email.rpartition("@")
    return Address(display_name=r.name if r.name != r.email else "", username=local, domain=domain)


def _raise_for_status(resp: requests.Response) -> None:
    if resp.status_code < 400:
        return
    detail = f"{resp.status_code} {resp.text[:300]}"
    if resp.status_code in (401, 403):
        raise MailError("auth_failed", detail)
    if resp.status_code == 429:
        raise MailError("rate_limited", detail, retryable=True)
    if resp.status_code >= 500:
        raise MailError("provider_unavailable", detail, retryable=resp.status_code == 503)
    raise MailError("bad_request", detail)


def _post(url: str, access_token: str, payload: dict) -> requests.Response:
    try:
        resp = requests.post(url, json=payload, headers={"Authorization": f"Bearer {access_token}"},
                             timeout=_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise MailError("connection_failed", str(exc)) from exc
    _raise_for_status(resp)
    return resp


class MailSender:
    backend = "base"

    def send(self, mail: OutgoingMail) -> SendResult:
        raise NotImplementedError


class GmailSender(MailSender):
    backend = "gmail"

    def __init__(self, access_token: str):
        self._token = access_token

    def send(self, mail: OutgoingMail) -> SendResult:
        raw = base64.urlsafe_b64encode(mail.to_mime().as_bytes()).decode("ascii")
        resp = _post(GMAIL_SEND_URL, self._token, {"raw": raw})
        return SendResult(backend=self.backend, message_id=resp.json().get("id"))


class GraphSender(MailSender):
    backend = "graph"

    def __init__(self, access_token: str):
        self._token = access_token

    def send(self, mail: OutgoingMail) -> SendResult:
        payload = {
            "message": {
                "subject": mail.subject,
                "body": {"contentType": "Text", "content": mail.body},
                "toRecipients": [{"emailAddress": {"address": r.email, "name": r.name}} for r in mail.recipients],
                "attachments": [
                    {"@odata.type": "#microsoft.graph.fileAttachment", "name": a.filename,
                     "contentType": a.mimetype, "contentBytes": base64.b64encode(a.data).decode("ascii")}
                    for a in mail.attachments
                ],
            },
            "saveToSentItems": True,
        }
        _post(GRAPH_SEND_URL, self._token, payload)  # 202 Accepted, no body
        return SendResult(backend=self.backend)


class OutboxSender(MailSender):
    """Development only: writes the message as an .eml file instead of sending it."""

    backend = "outbox"

    def __init__(self, directory: str):
        self._dir = directory

    def send(self, mail: OutgoingMail) -> SendResult:
        os.makedirs(self._dir, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        path = os.path.join(self._dir, f"{stamp}.eml")
        with open(path, "wb") as fh:
            fh.write(mail.to_mime().as_bytes())
        return SendResult(backend=self.backend, message_id=os.path.basename(path), outbox_path=path)
