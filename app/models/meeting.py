import uuid
from datetime import datetime, timezone

from app.extensions import db


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Meeting(db.Model):
    __tablename__ = "meetings"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    organizer_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False)
    # Optional project the meeting is filed under (REQ-65); one of the organizer's own projects.
    project_id = db.Column(db.String(36), db.ForeignKey("projects.id", ondelete="SET NULL"),
                           nullable=True, index=True)

    # "google_meet" | "teams" | "manual"
    platform = db.Column(db.String(20), nullable=False, default="manual")
    platform_event_id = db.Column(db.String(255), nullable=True, index=True)

    title = db.Column(db.String(500), nullable=False)
    scheduled_start = db.Column(db.DateTime(timezone=True), nullable=True)
    scheduled_end = db.Column(db.DateTime(timezone=True), nullable=True)

    # "scheduled" | "recording" | "transcribed" (recording ended, awaiting minutes)
    # | "processing" | "minutes_ready" | "sent"
    status = db.Column(db.String(20), nullable=False, default="scheduled")

    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    organizer = db.relationship("User", back_populates="meetings")
    project = db.relationship("Project", back_populates="meetings")
    participants = db.relationship("Participant", back_populates="meeting", cascade="all, delete-orphan")
    transcript_segments = db.relationship(
        "TranscriptSegment", back_populates="meeting", cascade="all, delete-orphan",
        order_by="TranscriptSegment.start_ms",
    )
    minutes = db.relationship("Minutes", back_populates="meeting", uselist=False, cascade="all, delete-orphan")
    knowledge = db.relationship("MeetingKnowledge", back_populates="meeting", uselist=False,
                                cascade="all, delete-orphan", passive_deletes=True)

    def __repr__(self) -> str:
        return f"<Meeting {self.title!r} ({self.platform})>"


class Participant(db.Model):
    __tablename__ = "participants"
    __table_args__ = (
        db.UniqueConstraint("meeting_id", "email", name="uq_participant_meeting_email"),
    )

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    meeting_id = db.Column(db.String(36), db.ForeignKey("meetings.id"), nullable=False)

    email = db.Column(db.String(255), nullable=False)
    display_name = db.Column(db.String(255), nullable=True)
    # True once the organizer typed the name by hand: calendar sync then leaves it alone (REQ-66).
    display_name_edited = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    is_organizer = db.Column(db.Boolean, nullable=False, default=False)

    # From Calendar/Graph invite response: "accepted" | "declined" | "tentative" | "needsAction"
    response_status = db.Column(db.String(20), nullable=False, default="needsAction")

    # Reserved for Phase 6 (Bot/native speaking events): platform-native participant/speaker id.
    platform_participant_id = db.Column(db.String(255), nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False)

    meeting = db.relationship("Meeting", back_populates="participants")

    def __repr__(self) -> str:
        return f"<Participant {self.email} of {self.meeting_id}>"


class TranscriptSegment(db.Model):
    __tablename__ = "transcript_segments"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    meeting_id = db.Column(db.String(36), db.ForeignKey("meetings.id"), nullable=False)

    start_ms = db.Column(db.Integer, nullable=False)
    end_ms = db.Column(db.Integer, nullable=False)
    text = db.Column(db.Text, nullable=False)

    # MVP: clustered label from diarization, e.g. "Speaker A". Not a verified identity.
    speaker_label = db.Column(db.String(50), nullable=True)

    # Reserved for Phase 6: filled once Bot/native platform speaking events are wired up.
    # When set, this is a verified identity and should be preferred over speaker_label.
    platform_speaker_id = db.Column(db.String(255), nullable=True)

    asr_confidence = db.Column(db.Float, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False)

    meeting = db.relationship("Meeting", back_populates="transcript_segments")

    def __repr__(self) -> str:
        return f"<TranscriptSegment {self.start_ms}-{self.end_ms}ms>"
