"""Projects that meetings can be filed under (REQ-64).

A project belongs to the user who created it; only they see and manage it. Its name, period and
stakeholders travel with the meetings linked to it: into the minutes prompt (REQ-67) and into the
knowledge-base metadata (REQ-68).
"""
import uuid
from datetime import datetime, timezone

from app.extensions import db

PROJECT_STATUSES = ("active", "closed")


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Project(db.Model):
    __tablename__ = "projects"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    owner_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)

    name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=True)
    start_date = db.Column(db.Date, nullable=True)
    end_date = db.Column(db.Date, nullable=True)
    # "active" | "closed" (closed projects are no longer offered when creating a meeting)
    status = db.Column(db.String(20), nullable=False, default="active")

    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    stakeholders = db.relationship("ProjectStakeholder", back_populates="project", cascade="all, delete-orphan",
                                   order_by="ProjectStakeholder.position", passive_deletes=True)
    # Deleting a project keeps its meetings; they just lose the link (ON DELETE SET NULL).
    meetings = db.relationship("Meeting", back_populates="project", passive_deletes=True)

    @property
    def period(self) -> str:
        """"2026-01-01 ~ 2026-06-30"; an open end is left blank; "" when neither date is set."""
        start = self.start_date.isoformat() if self.start_date else ""
        end = self.end_date.isoformat() if self.end_date else ""
        return f"{start} ~ {end}".strip() if start or end else ""

    def __repr__(self) -> str:
        return f"<Project {self.name!r} ({self.status})>"


class ProjectStakeholder(db.Model):
    __tablename__ = "project_stakeholders"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    project_id = db.Column(db.String(36), db.ForeignKey("projects.id", ondelete="CASCADE"),
                           nullable=False, index=True)

    name = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(255), nullable=True)  # lower-case
    role = db.Column(db.String(255), nullable=True)  # role in the project or organisation
    position = db.Column(db.Integer, nullable=False, default=0)  # order entered on the form

    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False)

    project = db.relationship("Project", back_populates="stakeholders")

    def __repr__(self) -> str:
        return f"<ProjectStakeholder {self.name!r} of {self.project_id}>"
