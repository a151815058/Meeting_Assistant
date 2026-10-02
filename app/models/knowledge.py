"""Knowledge base for Q&A over past meetings (REQ-58 ~ REQ-60).

One ``MeetingKnowledge`` row per meeting holds the searchable metadata (title, date, attendees,
AI summary); its ``MeetingKnowledgeChunk`` rows hold the saved minutes split into passages with
their embedding vectors (pgvector). ``participant_emails`` (organizer included) is what later
queries filter on, so people only ever see meetings they took part in.
"""
import uuid
from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from app.extensions import db

EMBEDDING_DIMENSIONS = 384  # intfloat/multilingual-e5-small; changing it needs a migration


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MeetingKnowledge(db.Model):
    __tablename__ = "meeting_knowledge"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    meeting_id = db.Column(db.String(36), db.ForeignKey("meetings.id", ondelete="CASCADE"),
                           nullable=False, unique=True)
    minutes_id = db.Column(db.String(36), db.ForeignKey("minutes.id", ondelete="SET NULL"), nullable=True)

    # Meeting metadata, copied at index time
    title = db.Column(db.String(500), nullable=False)
    meeting_start = db.Column(db.DateTime(timezone=True), nullable=True)
    platform = db.Column(db.String(20), nullable=False)
    organizer_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    organizer_name = db.Column(db.String(255), nullable=True)
    participant_emails = db.Column(ARRAY(db.String(255)), nullable=False, default=list)  # lower-case
    participant_names = db.Column(ARRAY(db.String(255)), nullable=False, default=list)
    # Project the meeting was filed under at index time (REQ-68); the rest of the project
    # metadata (description, period, stakeholders) is in each chunk's metadata.
    project_id = db.Column(db.String(36), db.ForeignKey("projects.id", ondelete="SET NULL"),
                           nullable=True, index=True)
    project_name = db.Column(db.String(255), nullable=True)

    # AI summary of the saved minutes; empty lists when the summary step failed (see summary_error)
    summary = db.Column(db.Text, nullable=True)
    key_points = db.Column(JSONB, nullable=False, default=list)
    decisions = db.Column(JSONB, nullable=False, default=list)
    action_items = db.Column(JSONB, nullable=False, default=list)  # [{"task", "owner", "due"}]
    keywords = db.Column(JSONB, nullable=False, default=list)
    summary_error = db.Column(db.String(50), nullable=True)

    # "pending" | "indexing" | "indexed" | "failed"
    status = db.Column(db.String(20), nullable=False, default="pending")
    error = db.Column(db.String(50), nullable=True)
    content_hash = db.Column(db.String(64), nullable=True)  # of the indexed minutes version + metadata
    minutes_hash = db.Column(db.String(64), nullable=True)  # of the minutes text the summary was made from
    embedding_model = db.Column(db.String(255), nullable=True)
    chunk_count = db.Column(db.Integer, nullable=False, default=0)
    indexed_at = db.Column(db.DateTime(timezone=True), nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    meeting = db.relationship("Meeting", back_populates="knowledge")
    chunks = db.relationship("MeetingKnowledgeChunk", back_populates="knowledge", cascade="all, delete-orphan",
                             order_by="MeetingKnowledgeChunk.chunk_index", passive_deletes=True)

    __table_args__ = (
        db.Index("ix_meeting_knowledge_participant_emails", "participant_emails", postgresql_using="gin"),
    )

    def __repr__(self) -> str:
        return f"<MeetingKnowledge {self.title!r} ({self.status})>"


class MeetingKnowledgeChunk(db.Model):
    __tablename__ = "meeting_knowledge_chunks"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    knowledge_id = db.Column(db.String(36), db.ForeignKey("meeting_knowledge.id", ondelete="CASCADE"),
                             nullable=False, index=True)
    meeting_id = db.Column(db.String(36), db.ForeignKey("meetings.id", ondelete="CASCADE"),
                           nullable=False, index=True)

    chunk_index = db.Column(db.Integer, nullable=False)
    section = db.Column(db.String(500), nullable=True)  # Markdown heading the passage sits under
    content = db.Column(db.Text, nullable=False)
    embedding = db.Column(Vector(EMBEDDING_DIMENSIONS), nullable=False)
    # Denormalised copy of the meeting metadata (title, date, attendee e-mails, section), so a
    # similarity query can filter and cite without a join.
    chunk_metadata = db.Column("metadata", JSONB, nullable=False, default=dict)

    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False)

    knowledge = db.relationship("MeetingKnowledge", back_populates="chunks")

    __table_args__ = (
        db.UniqueConstraint("knowledge_id", "chunk_index", name="uq_knowledge_chunk_index"),
        db.Index("ix_meeting_knowledge_chunks_embedding", "embedding", postgresql_using="hnsw",
                 postgresql_ops={"embedding": "vector_cosine_ops"}),
    )

    def __repr__(self) -> str:
        return f"<MeetingKnowledgeChunk {self.chunk_index} of {self.meeting_id}>"
