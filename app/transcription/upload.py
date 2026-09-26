"""Transcribe an uploaded recording: file -> decode -> VAD -> Whisper -> DB + live push (REQ-47).

The upload is written to a private temp file, decoded block by block with PyAV (bundled with
faster-whisper) so a long recording never sits in memory as a whole, and run through the same
segmentation and transcription as a live recording. The file is deleted as soon as the job ends,
whether it succeeded or not: only the transcript is kept (REQ-28 data minimisation).

Progress is pushed on the meeting's SocketIO room:
  upload_progress {processed_ms, total_ms}
  transcript_segment {...}                (same payload as live recording)
  upload_finished {segments}
  upload_error {error}
"""
import logging
import os
import tempfile
import uuid

import numpy as np

from app.background import spawn
from app.extensions import db
from app.models.meeting import Meeting
from app.security.audit import record_audit_event
from app.transcription import streaming
from app.transcription.vad import SAMPLE_RATE, SpeechSegmenter

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = frozenset({"mp3", "wav", "m4a", "aac", "flac", "ogg", "oga", "opus", "webm", "wma", "mp4"})
BLOCK_SECONDS = 30  # decoded audio handed to the segmenter at a time
_COPY_BUFFER = 1024 * 1024


class UploadRejected(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class AudioDecoder:
    """Streams an audio file as 16 kHz mono float32 blocks (PyAV / FFmpeg)."""

    def __init__(self, path: str):
        import av

        try:
            self._container = av.open(path)
        except (av.error.FFmpegError, UnicodeDecodeError) as exc:
            raise UploadRejected("invalid_audio") from exc
        if not self._container.streams.audio:
            self._container.close()
            raise UploadRejected("invalid_audio")
        self._stream = self._container.streams.audio[0]
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        self._frames = self._container.decode(self._stream)
        self._pending: list[np.ndarray] = []
        self._pending_len = 0
        self._done = False

    @property
    def duration_ms(self) -> int | None:
        if self._container.duration:  # microseconds (av.time_base)
            return int(self._container.duration / 1000)
        if self._stream.duration and self._stream.time_base:
            return int(self._stream.duration * self._stream.time_base * 1000)
        return None

    def read(self, samples: int) -> np.ndarray:
        """Next block of up to ``samples`` samples; an empty array at the end of the file."""
        import av

        while self._pending_len < samples and not self._done:
            try:
                frame = next(self._frames)
            except StopIteration:
                self._done = True
                frames = self._resampler.resample(None)  # flush
            except av.error.FFmpegError as exc:
                raise UploadRejected("invalid_audio") from exc
            else:
                frames = self._resampler.resample(frame)
            for out in frames:
                pcm = out.to_ndarray().reshape(-1)
                self._pending.append(pcm)
                self._pending_len += len(pcm)

        if not self._pending_len:
            return np.zeros(0, dtype=np.float32)
        merged = np.concatenate(self._pending)
        block, rest = merged[:samples], merged[samples:]
        self._pending = [rest] if len(rest) else []
        self._pending_len = len(rest)
        return block.astype(np.float32) / 32768.0

    def close(self) -> None:
        self._container.close()


class UploadJob:
    """Quacks like RecordingSession where streaming's transcribe/diarize helpers need it."""

    def __init__(self, *, meeting_id: str, user_id: str, offset_ms: int, path: str,
                 size_bytes: int, extension: str, previous_status: str):
        self.id = f"upload:{uuid.uuid4()}"
        self.meeting_id = meeting_id
        self.user_id = user_id
        self.offset_ms = offset_ms
        self.path = path
        self.size_bytes = size_bytes
        self.extension = extension
        self.previous_status = previous_status
        self.segments: list[tuple[str, int, int]] = []
        self.audio_ms = 0

    def load_audio(self) -> np.ndarray | None:
        """Whole file for diarization (same memory profile as a live recording's diarization)."""
        decoder = AudioDecoder(self.path)
        try:
            blocks = []
            while len(block := decoder.read(BLOCK_SECONDS * SAMPLE_RATE)):
                blocks.append(block)
            return np.concatenate(blocks) if blocks else None
        finally:
            decoder.close()

    def cleanup(self) -> None:
        if self.path and os.path.exists(self.path):
            os.remove(self.path)
        self.path = None


def file_extension(filename: str | None) -> str:
    name = (filename or "").strip()
    return name.rsplit(".", 1)[1].lower() if "." in name else ""


def _save_capped(file_storage, path: str, max_bytes: int) -> int:
    """Copy the upload to ``path``, refusing more than ``max_bytes`` (the request may lie about its size)."""
    size = 0
    with open(path, "wb") as out:
        while chunk := file_storage.stream.read(_COPY_BUFFER):
            size += len(chunk)
            if size > max_bytes:
                raise UploadRejected("file_too_large")
            out.write(chunk)
    return size


def _probe(path: str, max_seconds: int) -> None:
    decoder = AudioDecoder(path)
    try:
        duration = decoder.duration_ms
        if duration is not None and duration > max_seconds * 1000:
            raise UploadRejected("too_long")
    finally:
        decoder.close()


def start_upload(app, meeting: Meeting, user_id: str, file_storage) -> UploadJob:
    """Validate and store the upload, then transcribe it in the background.

    Raises UploadRejected with a code the page maps to a message."""
    cfg = app.config
    if file_storage is None or not file_storage.filename:
        raise UploadRejected("no_file")
    extension = file_extension(file_storage.filename)
    if extension not in ALLOWED_EXTENSIONS:
        raise UploadRejected("unsupported_type")
    if meeting.status == "processing":
        raise UploadRejected("minutes_in_progress")
    if streaming.meeting_busy(meeting.id):
        raise UploadRejected("busy")

    fd, path = tempfile.mkstemp(prefix="ma-upload-", suffix="." + extension)
    os.close(fd)
    try:
        size = streaming._blocking(app, _save_capped, file_storage, path, cfg["TRANSCRIPTION_UPLOAD_MAX_BYTES"])
        if size == 0:
            raise UploadRejected("empty_file")
        streaming._blocking(app, _probe, path, cfg["TRANSCRIPTION_MAX_RECORDING_SECONDS"])

        job = UploadJob(meeting_id=meeting.id, user_id=user_id, offset_ms=streaming.transcript_end_ms(meeting.id),
                        path=path, size_bytes=size, extension=extension, previous_status=meeting.status)
        try:
            streaming.claim_meeting(meeting.id, job.id)
        except streaming.AlreadyRecording as exc:
            raise UploadRejected("busy") from exc
    except BaseException:
        if os.path.exists(path):
            os.remove(path)
        raise

    meeting.status = "transcribing"
    db.session.commit()
    record_audit_event(actor_user_id=user_id, action="transcription.upload_started", target_type="meeting",
                       target_id=meeting.id, metadata={"bytes": size, "type": extension})
    spawn(_run, app, job, inline=cfg["TRANSCRIPTION_INLINE"])
    return job


def _run(app, job: UploadJob) -> None:
    with app.app_context():
        try:
            _transcribe_file(app, job)
            if app.config["DIARIZATION_ENABLED"] and job.segments:
                streaming._diarize(app, job)  # reports its own failure; the transcript is kept
            meeting = db.session.get(Meeting, job.meeting_id)
            if meeting is not None:
                meeting.status = "transcribed"
                db.session.commit()
            record_audit_event(actor_user_id=job.user_id, action="transcription.upload_finished",
                               target_type="meeting", target_id=job.meeting_id,
                               metadata={"audio_seconds": round(job.audio_ms / 1000, 1), "segments": len(job.segments)})
            streaming._emit("upload_finished", {"segments": len(job.segments)}, job)
        except Exception as exc:  # the job must always leave a final state
            db.session.rollback()
            code = exc.code if isinstance(exc, UploadRejected) else "transcription_failed"
            if not isinstance(exc, UploadRejected):
                logger.exception("upload transcription failed for meeting %s", job.meeting_id)
            meeting = db.session.get(Meeting, job.meeting_id)
            if meeting is not None:
                # Keep what was transcribed before the failure; it is listed on the page.
                meeting.status = "transcribed" if job.segments else job.previous_status
                db.session.commit()
            record_audit_event(actor_user_id=job.user_id, action="transcription.upload_failed",
                               target_type="meeting", target_id=job.meeting_id,
                               metadata={"error": code, "segments": len(job.segments)})
            streaming._emit("upload_error", {"error": code, "segments": len(job.segments)}, job)
        finally:
            job.cleanup()
            streaming.release_meeting(job.meeting_id, job.id)


def _transcribe_file(app, job: UploadJob) -> None:
    max_samples = app.config["TRANSCRIPTION_MAX_RECORDING_SECONDS"] * SAMPLE_RATE
    decoder = streaming._blocking(app, AudioDecoder, job.path)
    try:
        total_ms = decoder.duration_ms
        segmenter = SpeechSegmenter(streaming.get_vad_fn(app))
        decoded = 0
        while True:
            block = streaming._blocking(app, decoder.read, BLOCK_SECONDS * SAMPLE_RATE)
            if not len(block):
                break
            if decoded + len(block) > max_samples:  # metadata may understate the length
                raise UploadRejected("too_long")
            for chunk in streaming._blocking(app, _feed, segmenter, block):
                streaming._transcribe_and_publish(app, job, chunk)
            decoded += len(block)
            job.audio_ms = decoded * 1000 // SAMPLE_RATE
            streaming._emit("upload_progress", {"processed_ms": job.audio_ms, "total_ms": total_ms}, job)
        for chunk in streaming._blocking(app, segmenter.flush):
            streaming._transcribe_and_publish(app, job, chunk)
    finally:
        decoder.close()


def _feed(segmenter: SpeechSegmenter, block: np.ndarray):
    """The segmenter expects small live chunks; feed a decoded block in 1 s steps."""
    chunks = []
    for i in range(0, len(block), SAMPLE_RATE):
        chunks.extend(segmenter.feed(block[i:i + SAMPLE_RATE]))
    return chunks
