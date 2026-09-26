import uuid
from datetime import datetime, timezone

from app.extensions import db


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MinutesTemplate(db.Model):
    """User-authored template that the LLM is instructed to follow when
    producing meeting minutes. Body is a Jinja2-safe Markdown template with
    placeholders like {{ meeting.title }}, {{ participants }}, {{ transcript_summary }}.
    """

    __tablename__ = "minutes_templates"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    owner_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False)

    name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.String(1000), nullable=True)
    body = db.Column(db.Text, nullable=False)
    is_default = db.Column(db.Boolean, nullable=False, default=False)

    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    minutes = db.relationship("Minutes", back_populates="template")

    def __repr__(self) -> str:
        return f"<MinutesTemplate {self.name!r}>"


class Minutes(db.Model):
    """The AI-generated meeting minutes for a single meeting."""

    __tablename__ = "minutes"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    meeting_id = db.Column(db.String(36), db.ForeignKey("meetings.id"), nullable=False, unique=True)
    template_id = db.Column(db.String(36), db.ForeignKey("minutes_templates.id"), nullable=True)

    content_markdown = db.Column(db.Text, nullable=False)
    llm_provider = db.Column(db.String(50), nullable=False)
    llm_model = db.Column(db.String(100), nullable=False)

    # "draft" | "sent" | "send_failed"
    status = db.Column(db.String(20), nullable=False, default="draft")

    generated_at = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False)
    sent_at = db.Column(db.DateTime(timezone=True), nullable=True)

    meeting = db.relationship("Meeting", back_populates="minutes")
    template = db.relationship("MinutesTemplate", back_populates="minutes")

    def __repr__(self) -> str:
        return f"<Minutes for meeting {self.meeting_id}>"
