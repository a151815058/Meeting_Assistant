"""Jinja filters: show stored UTC times in DISPLAY_TIMEZONE; meeting status labels."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import current_app

_WEEKDAYS = "一二三四五六日"

MEETING_STATUS_LABELS = {
    "scheduled": "已安排",
    "recording": "錄音中",
    "transcribing": "轉錄中",
    "transcribed": "轉錄",
    "processing": "會議記錄產生中",
    "minutes_ready": "會議記錄已產生",
    "sent": "已寄出",
}


def localtime(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """``{{ dt|localtime }}``; ``%W`` in fmt becomes the Chinese weekday (一 … 日)."""
    if value is None:
        return ""
    if value.tzinfo is None:  # naive values in the DB are UTC
        value = value.replace(tzinfo=timezone.utc)
    local = value.astimezone(ZoneInfo(current_app.config["DISPLAY_TIMEZONE"]))
    return local.strftime(fmt.replace("%W", _WEEKDAYS[local.weekday()]))


def meeting_status(value: str | None) -> str:
    """``{{ meeting.status|meeting_status }}``; unknown values are shown as stored."""
    return MEETING_STATUS_LABELS.get(value, value or "")


def register(app) -> None:
    app.add_template_filter(localtime, "localtime")
    app.add_template_filter(meeting_status, "meeting_status")
