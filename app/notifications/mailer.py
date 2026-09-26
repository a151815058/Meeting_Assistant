"""Send reviewed minutes to the meeting's participants (REQ-15 ~ REQ-17).

- Recipients come only from the ``Participant`` table; a send request cannot name anyone.
- Mail is sent as the organizer through their own OAuth account (Gmail / Graph), never a
  shared system account. Development may fall back to a local .eml outbox instead.
- Every attempt ends in an audit entry (``mail.sent`` / ``mail.send_failed``) with the
  recipient list; failures can be retried from the minutes page.

Sending is synchronous within the request (one API call); the HTTP call itself runs in a
real OS thread so the eventlet loop keeps serving other clients.
"""
import logging
import os
import re
import threading
from datetime import datetime, timezone

from marshmallow import ValidationError, validate

from app.auth.token_service import get_valid_access_token
from app.background import run_blocking
from app.extensions import db, socketio
from app.models.meeting import Meeting
from app.models.user import OAuthAccount, User
from app.notifications.senders import (
    Attachment, GmailSender, GraphSender, MailError, MailSender, OutboxSender, OutgoingMail, Recipient, SendResult,
)
from app.security.audit import record_audit_event

logger = logging.getLogger(__name__)

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")
_is_email = validate.Email()

_sending: set[str] = set()  # meeting ids with a send in progress (single web process, RISK-06)
_sending_lock = threading.Lock()


class SendRejected(Exception):
    """Request-level problem shown to the user; nothing was sent or audited."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def is_sending(meeting_id: str) -> bool:
    return meeting_id in _sending


def reset_state() -> None:
    """Test helper."""
    _sending.clear()


# --- recipients & message ------------------------------------------------------------------

def _clean(text: str | None, limit: int = 200) -> str:
    """Single-line header text: no CR/LF or other control characters (header injection)."""
    return " ".join(_CONTROL_CHARS.sub(" ", text or "").split())[:limit]


def _valid_email(email: str) -> bool:
    try:
        _is_email(email)
    except ValidationError:
        return False
    return not _CONTROL_CHARS.search(email)


def recipients_for(meeting: Meeting) -> tuple[list[Recipient], list[str]]:
    """(recipients, skipped emails). The organizer is the sender, so they are not a recipient."""
    organizer_email = meeting.organizer.email.lower()
    seen, recipients, skipped = {organizer_email}, [], []
    for p in sorted(meeting.participants, key=lambda p: p.email.lower()):
        email = p.email.strip()
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        if not _valid_email(email):
            skipped.append(email)
            continue
        recipients.append(Recipient(email=email, name=_clean(p.display_name) or email))
    return recipients, skipped


def compose(meeting: Meeting, recipients: list[Recipient], attachments: tuple[Attachment, ...] = ()) -> OutgoingMail:
    organizer = meeting.organizer
    title = _clean(meeting.title, 150) or "會議"
    subject = f"【會議記錄】{title}"
    if meeting.scheduled_start:
        subject += f"（{meeting.scheduled_start.strftime('%Y-%m-%d')}）"
    sender_name = _clean(organizer.display_name) or organizer.email
    body = (
        f"{meeting.minutes.content_markdown.rstrip()}\n\n"
        "---\n"
        f"此信件由「會議小助手」以 {sender_name} 的帳號寄出。\n"
        "會議記錄由 AI 依會議逐字稿產生，並經主辦人確認；如有錯誤請直接回覆此信告知主辦人。\n"
    )
    return OutgoingMail(sender=Recipient(email=organizer.email, name=sender_name),
                        recipients=tuple(recipients), subject=subject, body=body, attachments=attachments)


# --- sender selection (REQ-16) --------------------------------------------------------------

def _can_send(account: OAuthAccount) -> bool:
    if not account.refresh_token:
        return False
    if account.provider == "google":
        return account.has_scope(GMAIL_SEND_SCOPE)
    if account.provider == "microsoft":
        # MSAL may return "Mail.Send" or the fully qualified "https://graph.microsoft.com/Mail.Send"
        return any(s.lower().endswith("mail.send") for s in (account.scopes or "").split())
    return False


def send_account(organizer: User, platform: str) -> OAuthAccount | None:
    """The organizer's account that can send mail, preferring the meeting's own platform."""
    preferred = {"google_meet": "google", "teams": "microsoft"}.get(platform)
    accounts = sorted((a for a in organizer.oauth_accounts if _can_send(a)),
                      key=lambda a: (a.provider != preferred, a.provider))
    return accounts[0] if accounts else None


def describe_backend(app, meeting: Meeting) -> str | None:
    """Which backend a send would use right now: gmail | graph | outbox | None (cannot send)."""
    if "mail_sender" in app.extensions:
        return app.extensions["mail_sender"].backend
    account = send_account(meeting.organizer, meeting.platform)
    if account is not None:
        return "gmail" if account.provider == "google" else "graph"
    return "outbox" if app.config["MAIL_OUTBOX_ENABLED"] else None


def _get_sender(app, meeting: Meeting) -> MailSender:
    if "mail_sender" in app.extensions:  # tests inject a fake transport here
        return app.extensions["mail_sender"]
    account = send_account(meeting.organizer, meeting.platform)
    if account is not None:
        try:
            token = get_valid_access_token(account)
        except Exception as exc:  # refresh rejected / revoked consent
            raise MailError("auth_failed", str(exc)) from exc
        return GmailSender(token) if account.provider == "google" else GraphSender(token)
    if app.config["MAIL_OUTBOX_ENABLED"]:
        return OutboxSender(app.config["MAIL_OUTBOX_DIR"] or os.path.join(app.instance_path, "outbox"))
    raise MailError("not_configured")


# --- send ---------------------------------------------------------------------------------

def build_attachments(app, meeting: Meeting, formats) -> tuple[Attachment, ...]:
    """Render the saved minutes in each requested format (REQ-41). Raises SendRejected."""
    from app.minutes import export

    attachments = []
    for fmt in formats:
        try:
            data = export.render(meeting, fmt, app.config)
        except export.ExportError as exc:
            raise SendRejected(exc.code) from exc
        except Exception as exc:
            logger.exception("rendering %s attachment failed for meeting %s", fmt, meeting.id)
            raise SendRejected("export_failed") from exc
        attachments.append(Attachment(export.filename(meeting, fmt), export.FORMATS[fmt], data))
    if sum(len(a.data) for a in attachments) > app.config["MAIL_MAX_ATTACHMENT_BYTES"]:
        raise SendRejected("attachments_too_large")
    return tuple(attachments)


def send_minutes(app, meeting: Meeting, user_id: str, *, generation_running: bool = False,
                 attach=()) -> tuple[SendResult, int]:
    """Send the saved minutes to every participant, optionally with Word/PDF attachments
    (``attach`` = formats, e.g. ("docx", "pdf")). Returns (result, recipient count).

    Raises SendRejected before anything is attempted, or MailError after a failed (audited) attempt.
    """
    cfg = app.config
    minutes = meeting.minutes
    if generation_running:
        raise SendRejected("generating")
    recipients, skipped = recipients_for(meeting)
    if not recipients:
        raise SendRejected("no_recipients")
    if len(recipients) > cfg["MAIL_MAX_RECIPIENTS"]:
        raise SendRejected("too_many_recipients")
    if minutes.sent_at is not None:
        since = (datetime.now(timezone.utc) - minutes.sent_at).total_seconds()
        if since < cfg["MAIL_RESEND_COOLDOWN_SECONDS"]:
            raise SendRejected("cooldown")
    attachments = build_attachments(app, meeting, attach)

    with _sending_lock:
        if meeting.id in _sending:
            raise SendRejected("already_sending")
        _sending.add(meeting.id)

    emails = [r.email for r in recipients]
    backend, attempts = None, 0
    try:
        mail = compose(meeting, recipients, attachments)
        sender = _get_sender(app, meeting)
        backend = sender.backend
        while True:
            attempts += 1
            try:
                result = run_blocking(sender.send, mail, inline=cfg["MAIL_INLINE"])
                break
            except MailError as exc:
                if not exc.retryable or attempts >= cfg["MAIL_SEND_ATTEMPTS"]:
                    raise
                logger.warning("mail send attempt %d for meeting %s failed (%s); retrying",
                               attempts, meeting.id, exc.code)
                socketio.sleep(cfg["MAIL_RETRY_BACKOFF_SECONDS"] * attempts)
    except Exception as exc:
        db.session.rollback()
        if not isinstance(exc, MailError):
            logger.exception("sending minutes failed for meeting %s", meeting.id)
            exc = MailError("internal_error", str(exc))
        else:
            logger.warning("sending minutes failed for meeting %s: %s", meeting.id, exc)
        if minutes.sent_at is None:  # a failed re-send does not undo an earlier successful send
            minutes.status = "send_failed"
            db.session.commit()
        record_audit_event(actor_user_id=user_id, action="mail.send_failed", target_type="meeting",
                           target_id=meeting.id,
                           metadata={"error": exc.code, "backend": backend, "attempts": attempts,
                                     "recipients": emails, "skipped": skipped, "attachments": list(attach)})
        raise exc
    finally:
        with _sending_lock:
            _sending.discard(meeting.id)

    minutes.status = "sent"
    minutes.sent_at = datetime.now(timezone.utc)
    meeting.status = "sent"
    db.session.commit()
    record_audit_event(actor_user_id=user_id, action="mail.sent", target_type="meeting", target_id=meeting.id,
                       metadata={"backend": result.backend, "message_id": result.message_id,
                                 "attempts": attempts, "count": len(emails), "recipients": emails,
                                 "skipped": skipped, "minutes_id": minutes.id, "attachments": list(attach)})
    return result, len(emails)
