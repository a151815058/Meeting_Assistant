"""Write a meeting's saved minutes into the knowledge base (REQ-58 ~ REQ-60).

Triggered after minutes are generated and after the organizer saves an edit (the saved version is
the one that was sent, so it is the one to search). Runs as a background job: loading the model
and embedding take seconds, and a failure here must never affect the minutes themselves. The
durable state is ``MeetingKnowledge.status`` ("indexing" -> "indexed" / "failed").

Unchanged content (same minutes, metadata and embedding model) is skipped. If a new request
arrives while a meeting is being indexed, one more pass runs afterwards with the latest content.
"""
import hashlib
import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.background import run_blocking, spawn
from app.extensions import db
from app.knowledge import summary as summary_mod
from app.knowledge.chunking import chunk_markdown
from app.knowledge.embeddings import EmbeddingError, get_embedder
from app.minutes.providers import LLMError
from app.models.knowledge import EMBEDDING_DIMENSIONS, MeetingKnowledge, MeetingKnowledgeChunk
from app.models.meeting import Meeting
from app.security.audit import record_audit_event

logger = logging.getLogger(__name__)

# meeting_id -> "run again when done" flag. Green threads switch only at I/O, so plain dict
# operations are safe here (a threading.Lock would block the whole eventlet hub).
_running: dict[str, bool] = {}


class KnowledgeIndexError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def is_indexing(meeting_id: str) -> bool:
    return meeting_id in _running


def reset_jobs() -> None:
    """Test helper."""
    _running.clear()


def schedule_index(app, meeting_id: str, actor_user_id: str | None, *, force: bool = False) -> bool:
    """Queue indexing of one meeting. Returns False when the knowledge base is switched off."""
    if not app.config["KNOWLEDGE_ENABLED"]:
        return False
    if meeting_id in _running:
        _running[meeting_id] = True
        return True
    _running[meeting_id] = False
    spawn(_worker, app, meeting_id, actor_user_id, force, inline=app.config["KNOWLEDGE_INLINE"])
    return True


def _worker(app, meeting_id: str, actor_user_id: str | None, force: bool) -> None:
    try:
        while True:
            _running[meeting_id] = False
            with app.app_context():
                index_meeting(app, meeting_id, actor_user_id, force=force)
            if not _running.get(meeting_id):
                break
            force = False
    finally:
        _running.pop(meeting_id, None)


def meeting_metadata(app, meeting: Meeting) -> dict:
    """Plain-data metadata stored with the meeting and copied onto each chunk."""
    when = meeting.scheduled_start or meeting.created_at
    local = when.astimezone(ZoneInfo(app.config["DISPLAY_TIMEZONE"])) if when else None
    organizer = meeting.organizer
    people = sorted(meeting.participants, key=lambda p: (not p.is_organizer, (p.display_name or p.email).lower()))
    emails, names = [], []
    for email, name in ([(organizer.email, organizer.display_name)] if organizer else []) + \
            [(p.email, p.display_name) for p in people]:
        email = (email or "").strip().lower()
        if email and email not in emails:
            emails.append(email)
            names.append((name or email).strip())
    project = meeting.project
    return {
        "meeting_id": meeting.id,
        "title": meeting.title,
        "date": local.strftime("%Y-%m-%d") if local else "",
        "meeting_start": when.isoformat() if when else None,
        "platform": meeting.platform,
        "organizer": (organizer.display_name or organizer.email) if organizer else "",
        "participant_emails": emails,
        "participant_names": names,
        # Project the meeting is filed under (REQ-68); None / empty when it has none.
        "project_id": project.id if project else None,
        "project_name": project.name if project else None,
        "project_description": (project.description or "") if project else "",
        "project_start": project.start_date.isoformat() if project and project.start_date else None,
        "project_end": project.end_date.isoformat() if project and project.end_date else None,
        "project_stakeholders": [s.name for s in project.stakeholders] if project else [],
    }


def _content_hash(markdown: str, metadata: dict, model: str) -> str:
    payload = json.dumps({"minutes": markdown, "meta": metadata, "model": model}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _minutes_hash(markdown: str, title: str) -> str:
    """Of everything the AI summary is made from."""
    return hashlib.sha256(json.dumps([title, markdown], ensure_ascii=False).encode("utf-8")).hexdigest()


def _stored_summary(knowledge: MeetingKnowledge) -> summary_mod.KnowledgeSummary:
    return summary_mod.KnowledgeSummary(
        summary=knowledge.summary or "", key_points=list(knowledge.key_points or []),
        decisions=list(knowledge.decisions or []), action_items=list(knowledge.action_items or []),
        keywords=list(knowledge.keywords or []))


def passage_text(metadata: dict, section: str | None, text: str) -> str:
    """What is embedded: the passage plus where it came from, so a query naming the project,
    meeting, date or section lands on the right passages. The stored ``content`` is the bare passage."""
    header = f"會議：{metadata['title']}｜日期：{metadata['date'] or '未知'}"
    if metadata.get("project_name"):
        header = f"專案：{metadata['project_name']}｜{header}"
    if section:
        header += f"｜章節：{section}"
    return f"{header}\n{text}"


def index_meeting(app, meeting_id: str, actor_user_id: str | None = None, *, force: bool = False) -> str:
    """Index synchronously. Returns "indexed", "skipped", "no_minutes" or "failed"."""
    meeting = db.session.get(Meeting, meeting_id)
    if meeting is None or meeting.minutes is None:
        return "no_minutes"
    minutes = meeting.minutes
    inline = app.config["KNOWLEDGE_INLINE"]
    knowledge = meeting.knowledge
    try:
        metadata = meeting_metadata(app, meeting)
        if knowledge is None:
            knowledge = MeetingKnowledge(meeting_id=meeting.id, title=meeting.title, platform=meeting.platform,
                                         organizer_id=meeting.organizer_id, status="pending")
            db.session.add(knowledge)
            db.session.commit()
        embedder = get_embedder(app)
        content_hash = _content_hash(minutes.content_markdown, metadata, embedder.name)
        if not force and knowledge.status == "indexed" and knowledge.content_hash == content_hash:
            return "skipped"

        knowledge.status, knowledge.error = "indexing", None
        db.session.commit()

        chunks = chunk_markdown(minutes.content_markdown, max_chars=app.config["KNOWLEDGE_CHUNK_CHARS"],
                                overlap=app.config["KNOWLEDGE_CHUNK_OVERLAP"])
        if not chunks:
            raise KnowledgeIndexError("empty_minutes")
        if embedder.dimensions != EMBEDDING_DIMENSIONS:
            raise EmbeddingError("dimension_mismatch",
                                 f"{embedder.name} has {embedder.dimensions} dims, column has {EMBEDDING_DIMENSIONS}")
        vectors = run_blocking(embedder.embed_passages,
                               [passage_text(metadata, c.section, c.text) for c in chunks], inline=inline)
        if len(vectors) != len(chunks) or any(len(v) != EMBEDDING_DIMENSIONS for v in vectors):
            raise EmbeddingError("dimension_mismatch", "embedder returned unexpected vectors")

        # The summary is optional metadata: its failure is recorded but does not block indexing.
        # When only metadata changed (attendees, project), the stored summary is kept, so the
        # minutes are not sent to the LLM again (RISK-13).
        summary_error = None
        minutes_hash = _minutes_hash(minutes.content_markdown, meeting.title)
        if (not force and knowledge.minutes_hash == minutes_hash and knowledge.summary_error is None
                and knowledge.indexed_at is not None):
            summary = _stored_summary(knowledge)
        else:
            try:
                provider = summary_mod.get_summary_provider(app)
                summary = run_blocking(summary_mod.summarize, provider, meeting.title, minutes.content_markdown,
                                       inline=inline)
            except (LLMError, summary_mod.SummaryError) as exc:
                logger.warning("knowledge summary failed for meeting %s: %s", meeting.id, exc)
                summary, summary_error = summary_mod.KnowledgeSummary(), exc.code

        knowledge.minutes_id = minutes.id
        knowledge.minutes_hash = minutes_hash
        knowledge.project_id = metadata["project_id"]
        knowledge.project_name = metadata["project_name"]
        knowledge.title = metadata["title"]
        knowledge.meeting_start = meeting.scheduled_start or meeting.created_at
        knowledge.platform = meeting.platform
        knowledge.organizer_id = meeting.organizer_id
        knowledge.organizer_name = metadata["organizer"]
        knowledge.participant_emails = metadata["participant_emails"]
        knowledge.participant_names = metadata["participant_names"]
        knowledge.summary = summary.summary or None
        knowledge.key_points = summary.key_points
        knowledge.decisions = summary.decisions
        knowledge.action_items = summary.action_items
        knowledge.keywords = summary.keywords
        knowledge.summary_error = summary_error

        MeetingKnowledgeChunk.query.filter_by(knowledge_id=knowledge.id).delete(synchronize_session=False)
        for chunk, vector in zip(chunks, vectors):
            db.session.add(MeetingKnowledgeChunk(
                knowledge_id=knowledge.id, meeting_id=meeting.id, chunk_index=chunk.index,
                section=(chunk.section or "")[:500] or None, content=chunk.text, embedding=vector,
                chunk_metadata={**metadata, "section": chunk.section, "keywords": summary.keywords},
            ))
        knowledge.chunk_count = len(chunks)
        knowledge.content_hash = content_hash
        knowledge.embedding_model = embedder.name
        knowledge.status = "indexed"
        knowledge.indexed_at = datetime.now(timezone.utc)
        db.session.commit()
    except Exception as exc:  # the job must always leave a final state
        db.session.rollback()
        code = exc.code if isinstance(exc, (EmbeddingError, KnowledgeIndexError)) else "internal_error"
        if code == "internal_error":
            logger.exception("knowledge indexing failed for meeting %s", meeting_id)
        else:
            logger.warning("knowledge indexing failed for meeting %s: %s", meeting_id, exc)
        knowledge = MeetingKnowledge.query.filter_by(meeting_id=meeting_id).first()
        if knowledge is not None:
            knowledge.status, knowledge.error = "failed", code
            db.session.commit()
        record_audit_event(actor_user_id=actor_user_id, action="knowledge.index_failed", target_type="meeting",
                           target_id=meeting_id, metadata={"error": code})
        return "failed"

    record_audit_event(
        actor_user_id=actor_user_id, action="knowledge.indexed", target_type="meeting", target_id=meeting_id,
        metadata={"chunks": knowledge.chunk_count, "model": knowledge.embedding_model,
                  "summary": "failed" if knowledge.summary_error else "ok"},
    )
    return "indexed"
