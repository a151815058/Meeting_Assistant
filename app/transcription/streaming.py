"""Recording sessions: audio intake -> VAD -> Whisper -> DB + live push (REQ-02, REQ-05).

One ``RecordingSession`` per recording WebSocket. The SocketIO handler only
validates and queues audio (cheap); a per-session worker drains the queue in
order, running VAD and Whisper off the event loop (eventlet tpool) so other
clients are not blocked while the CPU-bound models run.

Raw audio is never persisted. It lives only in memory while being segmented,
plus a temp file for the duration of diarization when that is enabled.
"""
import logging
import os
import tempfile
import time
from collections import deque

import numpy as np

from app.background import run_blocking, spawn
from app.extensions import db, socketio
from app.models.meeting import Meeting, TranscriptSegment
from app.security.audit import record_audit_event
from app.transcription.asr import WhisperTranscriber
from app.transcription.diarization import PyannoteDiarizer, assign_speaker_labels
from app.transcription.vad import SAMPLE_RATE, SpeechChunk, SpeechSegmenter, silero_vad_fn

logger = logging.getLogger(__name__)

NAMESPACE = "/transcription"
BYTES_PER_SECOND = SAMPLE_RATE * 2  # PCM16 mono


def meeting_room(meeting_id: str) -> str:
    return f"meeting:{meeting_id}"


class AlreadyRecording(Exception):
    pass


class TokenBucket:
    """Byte-rate limiter: allows ``rate`` bytes/s with bursts up to ``capacity`` bytes."""

    def __init__(self, rate: float, capacity: float, clock=time.monotonic):
        self._rate = rate
        self._capacity = capacity
        self._tokens = capacity
        self._clock = clock
        self._last = clock()

    def allow(self, n: int) -> bool:
        now = self._clock()
        self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
        self._last = now
        if n > self._tokens:
            return False
        self._tokens -= n
        return True


class RecordingSession:
    def __init__(self, *, sid: str, meeting_id: str, user_id: str, offset_ms: int,
                 segmenter: SpeechSegmenter, limiter: TokenBucket, max_bytes: int, keep_audio: bool):
        self.sid = sid
        self.meeting_id = meeting_id
        self.user_id = user_id
        self.offset_ms = offset_ms  # continue after earlier recordings of the same meeting
        self.segmenter = segmenter
        self.limiter = limiter
        self.max_bytes = max_bytes
        self.started_at = time.monotonic()
        self.total_bytes = 0

        self.inbox: deque[bytes] = deque()
        self.queued_bytes = 0
        self.lag_warned = False
        self.worker_running = False
        self.stopping = False
        self.stop_reason: str | None = None
        self.finalized = False
        self.segments: list[tuple[str, int, int]] = []  # (segment_id, start_ms, end_ms)

        self._audio_path = None
        if keep_audio:
            fd, self._audio_path = tempfile.mkstemp(prefix="ma-rec-", suffix=".pcm")
            os.close(fd)

    def accept(self, pcm: bytes) -> str | None:
        """Admission control for one chunk. Returns an error code, or None if accepted."""
        if self.total_bytes + len(pcm) > self.max_bytes:
            return "max_duration_reached"
        if not self.limiter.allow(len(pcm)):
            return "rate_limited"
        self.total_bytes += len(pcm)
        return None

    def process_pcm(self, pcm: bytes) -> list[SpeechChunk]:
        if self._audio_path:
            with open(self._audio_path, "ab") as f:
                f.write(pcm)
        audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        return self.segmenter.feed(audio)

    def flush(self) -> list[SpeechChunk]:
        return self.segmenter.flush()

    def load_audio(self) -> np.ndarray | None:
        if not self._audio_path:
            return None
        return np.fromfile(self._audio_path, dtype="<i2").astype(np.float32) / 32768.0

    def cleanup(self) -> None:
        if self._audio_path and os.path.exists(self._audio_path):
            os.remove(self._audio_path)
        self._audio_path = None


# --- session registry (per process) -------------------------------------------------

_sessions: dict[str, RecordingSession] = {}
_active_meetings: dict[str, str] = {}  # meeting_id -> sid of the live recording, or an upload job id


def get_session(sid: str) -> RecordingSession | None:
    return _sessions.get(sid)


def meeting_busy(meeting_id: str) -> bool:
    """True while the meeting is being recorded live or an uploaded file is being transcribed."""
    return meeting_id in _active_meetings


def claim_meeting(meeting_id: str, owner: str) -> None:
    """Reserve the meeting for one audio source (live recording or uploaded file) at a time."""
    if meeting_id in _active_meetings:
        raise AlreadyRecording("meeting is already being recorded or transcribed")
    _active_meetings[meeting_id] = owner


def release_meeting(meeting_id: str, owner: str) -> None:
    if _active_meetings.get(meeting_id) == owner:
        _active_meetings.pop(meeting_id, None)


def transcript_end_ms(meeting_id: str) -> int:
    """New audio continues after the meeting's existing transcript."""
    return (
        db.session.query(db.func.max(TranscriptSegment.end_ms))
        .filter(TranscriptSegment.meeting_id == meeting_id)
        .scalar()
    ) or 0


def start_session(app, *, sid: str, meeting: Meeting, user_id: str) -> RecordingSession:
    if sid in _sessions:
        raise AlreadyRecording("this connection is already recording")
    if meeting_busy(meeting.id):
        raise AlreadyRecording("meeting is already being recorded or transcribed")

    last_end = transcript_end_ms(meeting.id)
    cfg = app.config
    rate = cfg["TRANSCRIPTION_MAX_BYTES_PER_SECOND"]
    session = RecordingSession(
        sid=sid,
        meeting_id=meeting.id,
        user_id=user_id,
        offset_ms=last_end,
        segmenter=SpeechSegmenter(get_vad_fn(app)),
        limiter=TokenBucket(rate=rate, capacity=rate * 2),
        max_bytes=cfg["TRANSCRIPTION_MAX_RECORDING_SECONDS"] * BYTES_PER_SECOND,
        keep_audio=cfg["DIARIZATION_ENABLED"],
    )
    _sessions[sid] = session
    _active_meetings[meeting.id] = sid
    return session


def _end_session(session: RecordingSession) -> None:
    _sessions.pop(session.sid, None)
    release_meeting(session.meeting_id, session.sid)


def reset_sessions() -> None:
    """Test helper."""
    for session in list(_sessions.values()):
        session.cleanup()
    _sessions.clear()
    _active_meetings.clear()


# --- model accessors (cached per app; tests inject fakes via app.extensions) -------

def get_vad_fn(app):
    if "transcription_vad" not in app.extensions:
        app.extensions["transcription_vad"] = silero_vad_fn()
    return app.extensions["transcription_vad"]


def get_transcriber(app):
    if "transcription_asr" not in app.extensions:
        cfg = app.config
        app.extensions["transcription_asr"] = WhisperTranscriber(
            cfg["WHISPER_MODEL_SIZE"], cfg["WHISPER_DEVICE"], cfg["WHISPER_COMPUTE_TYPE"],
            language=cfg["WHISPER_LANGUAGE"], initial_prompt=cfg["WHISPER_INITIAL_PROMPT"],
            cpu_threads=cfg["WHISPER_CPU_THREADS"],
        )
    return app.extensions["transcription_asr"]


def get_diarizer(app):
    if "transcription_diarizer" not in app.extensions:
        app.extensions["transcription_diarizer"] = PyannoteDiarizer(
            app.config["DIARIZATION_MODEL"], app.config["HF_TOKEN"]
        )
    return app.extensions["transcription_diarizer"]


def warm_up(app, delay: float = 0.0) -> None:
    """Load VAD + Whisper in the background so the first utterance isn't delayed by model loading.

    Called at server start (run.py) and, as a fallback, when a client connects. Loading holds the
    GIL for ~2 s even in a worker thread, so the connect-time call waits ``delay`` seconds to let
    the connection handshake finish first."""
    if app.config["TRANSCRIPTION_INLINE"] or app.extensions.get("transcription_warm"):
        return
    app.extensions["transcription_warm"] = True

    def _load_models():
        get_vad_fn(app)  # importing faster_whisper/onnxruntime alone takes seconds
        get_transcriber(app).load()

    def _load():
        if delay:
            socketio.sleep(delay)
        try:
            # Everything runs in a real thread: even the imports would otherwise block the event loop.
            _blocking(app, _load_models)
        except Exception:
            logger.exception("failed to preload transcription models")
            app.extensions["transcription_warm"] = False

    spawn(_load)


# --- processing -----------------------------------------------------------------

def enqueue_audio(app, session: RecordingSession, pcm: bytes) -> None:
    session.inbox.append(pcm)
    session.queued_bytes += len(pcm)
    lag_limit = app.config["TRANSCRIPTION_LAG_WARNING_SECONDS"] * BYTES_PER_SECOND
    if session.queued_bytes > lag_limit and not session.lag_warned:
        session.lag_warned = True  # warn once per recording
        _emit("recording_error", {"error": "transcription_lagging"}, session)
    _kick(app, session)


def request_stop(app, session: RecordingSession, reason: str) -> None:
    if session.stopping:
        return
    session.stopping = True
    session.stop_reason = reason
    _kick(app, session)


def _kick(app, session: RecordingSession) -> None:
    if session.worker_running:
        return
    session.worker_running = True
    spawn(_drain, app, session, inline=app.config["TRANSCRIPTION_INLINE"])


def _blocking(app, fn, *args):
    """Run CPU-bound model code in a real OS thread so the eventlet hub keeps serving."""
    return run_blocking(fn, *args, inline=app.config["TRANSCRIPTION_INLINE"])


def _drain(app, session: RecordingSession) -> None:
    with app.app_context():
        try:
            while session.inbox:
                pcm = session.inbox.popleft()
                session.queued_bytes -= len(pcm)
                for chunk in _blocking(app, session.process_pcm, pcm):
                    _transcribe_and_publish(app, session, chunk)
            if session.stopping and not session.finalized:
                _finalize(app, session)
        except Exception:
            logger.exception("transcription worker failed for meeting %s", session.meeting_id)
            _emit("recording_error", {"error": "transcription_failed"}, session)
        finally:
            session.worker_running = False


def _transcribe_and_publish(app, session: RecordingSession, chunk: SpeechChunk) -> None:
    result = _blocking(app, get_transcriber(app).transcribe, chunk.audio)
    if not result.text:
        return

    segment = TranscriptSegment(
        meeting_id=session.meeting_id,
        start_ms=session.offset_ms + chunk.start_ms,
        end_ms=session.offset_ms + chunk.end_ms,
        text=result.text,
        asr_confidence=result.confidence,
    )
    db.session.add(segment)
    db.session.commit()
    session.segments.append((segment.id, segment.start_ms, segment.end_ms))

    _emit("transcript_segment", {
        "id": segment.id,
        "start_ms": segment.start_ms,
        "end_ms": segment.end_ms,
        "text": segment.text,
        "speaker_label": None,
        "confidence": segment.asr_confidence,
    }, session)


def _finalize(app, session: RecordingSession) -> None:
    session.finalized = True
    try:
        for chunk in _blocking(app, session.flush):
            _transcribe_and_publish(app, session, chunk)

        meeting = db.session.get(Meeting, session.meeting_id)
        if meeting is not None:
            meeting.status = "transcribed"
            db.session.commit()
        record_audit_event(
            actor_user_id=session.user_id,
            action="transcription.recording_stopped",
            target_type="meeting",
            target_id=session.meeting_id,
            metadata={
                "reason": session.stop_reason,
                "audio_seconds": round(session.total_bytes / BYTES_PER_SECOND, 1),
                "segments": len(session.segments),
            },
        )
        _emit("recording_stopped", {"reason": session.stop_reason, "segments": len(session.segments)}, session)
    finally:
        _end_session(session)

    try:
        if app.config["DIARIZATION_ENABLED"] and session.segments:
            _diarize(app, session)
    finally:
        session.cleanup()


def _diarize(app, session: RecordingSession) -> None:
    try:
        audio = _blocking(app, session.load_audio)
        turns = _blocking(app, get_diarizer(app).diarize, audio)
    except Exception:
        logger.exception("diarization failed for meeting %s", session.meeting_id)
        _emit("recording_error", {"error": "diarization_failed"}, session)
        return

    # Diarization timestamps are relative to this recording; segments carry the meeting offset.
    relative = [(sid, start - session.offset_ms, end - session.offset_ms) for sid, start, end in session.segments]
    labels = assign_speaker_labels(relative, turns)
    for segment_id, label in labels.items():
        segment = db.session.get(TranscriptSegment, segment_id)
        if segment is not None:
            segment.speaker_label = label
    db.session.commit()
    _emit("speaker_labels", {"labels": [{"id": k, "speaker_label": v} for k, v in labels.items()]}, session)


def _emit(event: str, payload: dict, session: RecordingSession) -> None:
    socketio.emit(event, payload, to=meeting_room(session.meeting_id), namespace=NAMESPACE)
