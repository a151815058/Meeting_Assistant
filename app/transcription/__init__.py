"""Real-time transcription over SocketIO namespace ``/transcription``.

Client protocol:
  emit  watch_meeting {meeting_id}        -> ack {ok, busy}   (receive this meeting's events)
  emit  start_recording {meeting_id}      -> ack {ok, error?}
  emit  audio_chunk <binary PCM16 16kHz>  (no ack; errors arrive as ``recording_error``)
  emit  stop_recording                    -> ack {ok}
  recv  transcript_segment {id, start_ms, end_ms, text, speaker_label, confidence}
  recv  speaker_labels {labels: [{id, speaker_label}]}
  recv  recording_stopped {reason, segments}
  recv  recording_error {error}
  recv  upload_progress / upload_finished / upload_error   (uploaded file, see upload.py)

Security: only authenticated users may connect; only the meeting organizer may
record it; every payload is schema-validated; audio is rate- and size-limited.
"""
from flask import current_app, request
from flask_login import current_user
from flask_socketio import SocketIO, emit, join_room, leave_room
from marshmallow import ValidationError

from app.extensions import db
from app.models.meeting import Meeting
from app.security.audit import record_audit_event
from app.transcription import streaming
from app.transcription.schemas import StartRecordingSchema, validate_audio_chunk

NAMESPACE = streaming.NAMESPACE


def register_socketio_handlers(socketio: SocketIO) -> None:
    @socketio.on("connect", namespace=NAMESPACE)
    def handle_connect(_auth=None):
        if not current_user.is_authenticated:
            return False  # reject the connection
        streaming.warm_up(current_app._get_current_object(), delay=1.0)
        return True

    @socketio.on("watch_meeting", namespace=NAMESPACE)
    def handle_watch_meeting(data=None):
        try:
            payload = StartRecordingSchema().load(data if isinstance(data, dict) else {})
        except ValidationError:
            return {"ok": False, "error": "invalid_payload"}
        meeting = db.session.get(Meeting, payload["meeting_id"])
        if meeting is None or meeting.organizer_id != current_user.id:
            return {"ok": False, "error": "not_found"}
        join_room(streaming.meeting_room(meeting.id))
        return {"ok": True, "busy": streaming.meeting_busy(meeting.id)}

    @socketio.on("start_recording", namespace=NAMESPACE)
    def handle_start_recording(data=None):
        try:
            payload = StartRecordingSchema().load(data if isinstance(data, dict) else {})
        except ValidationError:
            return {"ok": False, "error": "invalid_payload"}

        meeting = db.session.get(Meeting, payload["meeting_id"])
        if meeting is None or meeting.organizer_id != current_user.id:
            return {"ok": False, "error": "not_found"}

        try:
            session = streaming.start_session(current_app._get_current_object(), sid=request.sid,
                                              meeting=meeting, user_id=current_user.id)
        except streaming.AlreadyRecording:
            return {"ok": False, "error": "already_recording"}

        meeting.status = "recording"
        db.session.commit()
        record_audit_event(actor_user_id=current_user.id, action="transcription.recording_started",
                           target_type="meeting", target_id=meeting.id)
        join_room(streaming.meeting_room(meeting.id))
        return {"ok": True, "offset_ms": session.offset_ms}

    @socketio.on("audio_chunk", namespace=NAMESPACE)
    def handle_audio_chunk(data=None):
        session = streaming.get_session(request.sid)
        if session is None or session.stopping:
            emit("recording_error", {"error": "not_recording"})
            return

        app = current_app._get_current_object()
        try:
            pcm = validate_audio_chunk(data, app.config["TRANSCRIPTION_MAX_CHUNK_BYTES"])
        except ValidationError:
            emit("recording_error", {"error": "invalid_audio_chunk"})
            return

        error = session.accept(pcm)
        if error == "max_duration_reached":
            emit("recording_error", {"error": error})
            streaming.request_stop(app, session, reason=error)
            return
        if error:
            emit("recording_error", {"error": error})  # chunk dropped
            return

        streaming.enqueue_audio(app, session, pcm)

    @socketio.on("stop_recording", namespace=NAMESPACE)
    def handle_stop_recording(_data=None):
        session = streaming.get_session(request.sid)
        if session is None:
            return {"ok": False, "error": "not_recording"}
        streaming.request_stop(current_app._get_current_object(), session, reason="user_stopped")
        return {"ok": True}

    @socketio.on("disconnect", namespace=NAMESPACE)
    def handle_disconnect(*_args):
        session = streaming.get_session(request.sid)
        if session is not None:
            leave_room(streaming.meeting_room(session.meeting_id))
            streaming.request_stop(current_app._get_current_object(), session, reason="disconnected")
