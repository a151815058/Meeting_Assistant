"""Minutes generation (REQ-12): transcript + template -> LLM -> editable draft.

Runs as a background job (LLM calls take tens of seconds to minutes). Job progress is
kept in process memory for the status endpoint; the durable result is the ``Minutes``
row plus ``Meeting.status`` ("processing" -> "minutes_ready", or back on failure).

Long meetings: if the prompt would exceed MINUTES_MAX_INPUT_TOKENS, the transcript is
split into chunks, each chunk is condensed into notes (map), and the minutes are written
from the notes (reduce). Nothing is silently truncated.
"""
import logging
import math
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.background import run_blocking, spawn
from app.extensions import db
from app.knowledge import indexer
from app.minutes import prompting
from app.minutes.providers import LLMError, get_provider
from app.models.meeting import Meeting
from app.models.template import Minutes, MinutesTemplate
from app.security.audit import record_audit_event
from app.transcription import streaming

logger = logging.getLogger(__name__)


class GenerationRejected(Exception):
    """Request-level problem shown to the user (no job started)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class Job:
    state: str = "running"  # running | done | failed
    error: str | None = None
    progress: str = "準備中"
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


_jobs: dict[str, Job] = {}  # meeting_id -> latest job
_jobs_lock = threading.Lock()


def get_job(meeting_id: str) -> Job | None:
    return _jobs.get(meeting_id)


def reset_jobs() -> None:
    """Test helper."""
    _jobs.clear()


def resolve_template(template_id: str | None, user_id: str) -> MinutesTemplate | None:
    """None -> the user's default template, else the built-in one (returned as None)."""
    if template_id:
        template = db.session.get(MinutesTemplate, template_id)
        if template is None or template.owner_id != user_id:
            raise GenerationRejected("template_not_found")
        return template
    return MinutesTemplate.query.filter_by(owner_id=user_id, is_default=True).first()


def start_generation(app, meeting: Meeting, template: MinutesTemplate | None, user_id: str) -> None:
    if meeting.status == "recording":
        raise GenerationRejected("recording_in_progress")
    # Status alone could be stale after a restart (jobs live in memory), so ask the registry.
    if meeting.status == "transcribing" and streaming.meeting_busy(meeting.id):
        raise GenerationRejected("transcription_in_progress")
    if not meeting.transcript_segments:
        raise GenerationRejected("no_transcript")

    with _jobs_lock:
        current = _jobs.get(meeting.id)
        if current is not None and current.state == "running":
            raise GenerationRejected("already_generating")
        _jobs[meeting.id] = Job()

    previous_status = meeting.status
    meeting.status = "processing"
    db.session.commit()
    spawn(_run_job, app, meeting.id, template.id if template else None, user_id, previous_status,
          inline=app.config["MINUTES_INLINE"])


def _run_job(app, meeting_id: str, template_id: str | None, user_id: str, previous_status: str) -> None:
    job = _jobs[meeting_id]
    with app.app_context():
        try:
            meeting = db.session.get(Meeting, meeting_id)
            template = db.session.get(MinutesTemplate, template_id) if template_id else None
            minutes = generate_minutes(app, meeting, template, job)
            meeting.status = "minutes_ready"
            db.session.commit()
            job.state, job.progress = "done", "完成"
            logger.info("minutes %s generated for meeting %s", minutes.id, meeting_id)
        except Exception as exc:  # the job must always leave a final state
            db.session.rollback()
            code = exc.code if isinstance(exc, (LLMError, GenerationRejected)) else "internal_error"
            if not isinstance(exc, LLMError):
                logger.exception("minutes generation failed for meeting %s", meeting_id)
            meeting = db.session.get(Meeting, meeting_id)
            if meeting is not None:
                meeting.status = previous_status
                db.session.commit()
            record_audit_event(actor_user_id=user_id, action="minutes.generation_failed",
                               target_type="meeting", target_id=meeting_id, metadata={"error": code})
            job.state, job.error = "failed", code
            return
    # Knowledge base (REQ-58): a separate job, so its failure never touches the minutes.
    indexer.schedule_index(app, meeting_id, user_id)


def generate_minutes(app, meeting: Meeting, template: MinutesTemplate | None, job: Job | None = None) -> Minutes:
    provider = get_provider(app)
    cfg = app.config
    inline = cfg["MINUTES_INLINE"]

    def progress(text):
        if job is not None:
            job.progress = text

    context = prompting.build_context(meeting)
    try:
        rendered = prompting.render_template_body(
            template.body if template else prompting.BUILTIN_TEMPLATE_BODY, context)
    except prompting.TemplateError as exc:
        raise GenerationRejected("template_render_failed") from exc

    segments = list(meeting.transcript_segments)
    transcript = prompting.format_transcript(segments)
    prompt = prompting.build_minutes_prompt(context, rendered, transcript)

    progress("計算逐字稿長度")
    tokens = run_blocking(provider.count_tokens, prompting.SYSTEM_PROMPT, prompt, inline=inline)
    if tokens is None:
        tokens = _estimate_tokens(prompt)
    limit = cfg["MINUTES_MAX_INPUT_TOKENS"]

    chunks_used = 1
    input_tokens = output_tokens = 0
    if tokens > limit:
        chunks = _split_segments(segments, transcript_tokens=tokens, chunk_tokens=cfg["MINUTES_CHUNK_TOKENS"])
        chunks_used = len(chunks)
        notes = []
        for i, chunk in enumerate(chunks, start=1):
            progress(f"逐字稿很長，分段整理中（{i}/{len(chunks)}）")
            chunk_prompt = prompting.build_chunk_prompt(context, prompting.format_transcript(chunk), i, len(chunks))
            result = run_blocking(provider.generate, prompting.CHUNK_SYSTEM_PROMPT, chunk_prompt, inline=inline)
            input_tokens += result.input_tokens
            output_tokens += result.output_tokens
            notes.append(f"### 第 {i}/{len(chunks)} 段筆記\n{result.text}")
        prompt = prompting.build_minutes_prompt(context, rendered, "\n\n".join(notes), from_notes=True)

    progress("AI 撰寫會議記錄中")
    result = run_blocking(provider.generate, prompting.SYSTEM_PROMPT, prompt, inline=inline)
    input_tokens += result.input_tokens
    output_tokens += result.output_tokens
    if not result.text:
        raise LLMError("bad_request", "empty response")

    minutes = meeting.minutes or Minutes(meeting_id=meeting.id)
    minutes.content_markdown = result.text
    minutes.template_id = template.id if template else None
    minutes.llm_provider = provider.name
    minutes.llm_model = result.model
    minutes.status = "draft"
    minutes.generated_at = datetime.now(timezone.utc)
    db.session.add(minutes)
    db.session.commit()

    record_audit_event(
        actor_user_id=meeting.organizer_id, action="minutes.generated", target_type="meeting",
        target_id=meeting.id,
        metadata={
            "minutes_id": minutes.id, "provider": provider.name, "model": result.model,
            "template": template.name if template else prompting.BUILTIN_TEMPLATE_NAME,
            "segments": len(segments), "chunks": chunks_used,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
        },
    )
    return minutes


def _estimate_tokens(text: str) -> int:
    # Conservative for CJK text (roughly one token per character).
    return len(text)


def _split_segments(segments, *, transcript_tokens: int, chunk_tokens: int) -> list[list]:
    """Split on segment boundaries into chunks of roughly chunk_tokens each."""
    n_chunks = max(2, math.ceil(transcript_tokens / chunk_tokens))
    total_chars = sum(len(s.text) for s in segments) or 1
    target = total_chars / n_chunks
    chunks, current, size = [], [], 0
    for seg in segments:
        if current and size + len(seg.text) > target and len(chunks) < n_chunks - 1:
            chunks.append(current)
            current, size = [], 0
        current.append(seg)
        size += len(seg.text)
    if current:
        chunks.append(current)
    return chunks
