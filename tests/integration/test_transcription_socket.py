import os

import numpy as np
import pytest

from app.extensions import db as _db
from app.extensions import socketio
from app.models.audit import AuditLog
from app.models.meeting import Meeting, TranscriptSegment
from app.models.user import User
from app.transcription import streaming
from app.transcription.asr import TranscriptionResult
from app.transcription.diarization import SpeakerTurn
from app.transcription.vad import SAMPLE_RATE
from tests.unit.test_transcription_pipeline import energy_vad

NS = "/transcription"


class FakeTranscriber:
    def __init__(self):
        self.calls = []

    def transcribe(self, audio):
        self.calls.append(len(audio))
        return TranscriptionResult(text=f"第{len(self.calls)}段", confidence=0.9)


class FakeDiarizer:
    def __init__(self):
        self.audio_len = None

    def diarize(self, audio):
        self.audio_len = len(audio)
        return [SpeakerTurn(0, 1500, "spk1"), SpeakerTurn(1500, 99999, "spk2")]


@pytest.fixture(autouse=True)
def fakes(app):
    streaming.reset_sessions()
    transcriber = FakeTranscriber()
    app.extensions["transcription_vad"] = energy_vad
    app.extensions["transcription_asr"] = transcriber
    yield transcriber
    streaming.reset_sessions()


def _make_user_and_meeting(app, email="org@example.com"):
    with app.app_context():
        user = User(email=email, display_name="Org")
        _db.session.add(user)
        _db.session.flush()
        meeting = Meeting(organizer_id=user.id, title="週會")
        _db.session.add(meeting)
        _db.session.commit()
        return user.id, meeting.id


def _socket_client(app, client, user_id):
    with client.session_transaction() as sess:
        sess["_user_id"] = user_id
        sess["_fresh"] = True
    return socketio.test_client(app, namespace=NS, flask_test_client=client)


def _pcm(audio: np.ndarray) -> bytes:
    return (audio * 32767).astype("<i2").tobytes()


def _speech_then_pause(seconds_speech=1.0, seconds_pause=1.0) -> bytes:
    audio = np.concatenate([
        np.full(int(SAMPLE_RATE * seconds_speech), 0.5, dtype=np.float32),
        np.zeros(int(SAMPLE_RATE * seconds_pause), dtype=np.float32),
    ])
    return _pcm(audio)


def _send(sio, pcm: bytes, chunk_bytes=8000):
    for i in range(0, len(pcm), chunk_bytes):
        sio.emit("audio_chunk", pcm[i:i + chunk_bytes], namespace=NS)


def _events(sio, name):
    return [e["args"][0] for e in sio.get_received(NS) if e["name"] == name]


# --- TC-01 / TC-02 / TC-05: full recording flow --------------------------------------

def test_recording_flow_streams_segments_and_persists_them(app, client, fakes):
    user_id, meeting_id = _make_user_and_meeting(app)
    sio = _socket_client(app, client, user_id)
    assert sio.is_connected(NS)

    ack = sio.emit("start_recording", {"meeting_id": meeting_id}, namespace=NS, callback=True)
    assert ack == {"ok": True, "offset_ms": 0}
    with app.app_context():
        assert _db.session.get(Meeting, meeting_id).status == "recording"

    _send(sio, _speech_then_pause() + _speech_then_pause())
    live = _events(sio, "transcript_segment")
    assert [s["text"] for s in live] == ["第1段", "第2段"]
    assert live[0]["start_ms"] == 0 and live[1]["start_ms"] == 2000

    assert sio.emit("stop_recording", namespace=NS, callback=True) == {"ok": True}
    stopped = _events(sio, "recording_stopped")
    assert stopped == [{"reason": "user_stopped", "segments": 2}]

    with app.app_context():
        rows = TranscriptSegment.query.filter_by(meeting_id=meeting_id).order_by(TranscriptSegment.start_ms).all()
        assert [(r.start_ms, r.end_ms, r.text) for r in rows] == [(0, 1000, "第1段"), (2000, 3000, "第2段")]
        assert _db.session.get(Meeting, meeting_id).status == "transcribed"
        actions = [a.action for a in AuditLog.query.filter_by(target_id=meeting_id).all()]
        assert "transcription.recording_started" in actions
        assert "transcription.recording_stopped" in actions


def test_stop_flushes_speech_still_in_progress(app, client, fakes):
    user_id, meeting_id = _make_user_and_meeting(app)
    sio = _socket_client(app, client, user_id)
    sio.emit("start_recording", {"meeting_id": meeting_id}, namespace=NS, callback=True)

    _send(sio, _pcm(np.full(SAMPLE_RATE, 0.5, dtype=np.float32)))  # no pause yet
    assert _events(sio, "transcript_segment") == []

    sio.emit("stop_recording", namespace=NS, callback=True)
    assert [s["text"] for s in _events(sio, "transcript_segment")] == ["第1段"]


def test_second_recording_continues_timeline(app, client, fakes):
    user_id, meeting_id = _make_user_and_meeting(app)
    sio = _socket_client(app, client, user_id)
    sio.emit("start_recording", {"meeting_id": meeting_id}, namespace=NS, callback=True)
    _send(sio, _speech_then_pause())
    sio.emit("stop_recording", namespace=NS, callback=True)

    ack = sio.emit("start_recording", {"meeting_id": meeting_id}, namespace=NS, callback=True)
    assert ack == {"ok": True, "offset_ms": 1000}
    sio.get_received(NS)
    _send(sio, _speech_then_pause())
    assert _events(sio, "transcript_segment")[0]["start_ms"] == 1000


def test_disconnect_finalizes_recording(app, client, fakes):
    user_id, meeting_id = _make_user_and_meeting(app)
    sio = _socket_client(app, client, user_id)
    sio.emit("start_recording", {"meeting_id": meeting_id}, namespace=NS, callback=True)
    _send(sio, _pcm(np.full(SAMPLE_RATE, 0.5, dtype=np.float32)))

    sio.disconnect(NS)

    with app.app_context():
        assert TranscriptSegment.query.filter_by(meeting_id=meeting_id).count() == 1
        assert _db.session.get(Meeting, meeting_id).status == "transcribed"
    assert streaming.get_session("anything") is None and not streaming._active_meetings


# --- access control -----------------------------------------------------------------

def test_unauthenticated_connection_is_rejected(app):
    sio = socketio.test_client(app, namespace=NS)
    assert not sio.is_connected(NS)


def test_cannot_record_someone_elses_meeting(app, client):
    _owner_id, meeting_id = _make_user_and_meeting(app, email="owner@example.com")
    other_id, _ = _make_user_and_meeting(app, email="other@example.com")
    sio = _socket_client(app, client, other_id)

    ack = sio.emit("start_recording", {"meeting_id": meeting_id}, namespace=NS, callback=True)
    assert ack == {"ok": False, "error": "not_found"}


def test_same_meeting_cannot_be_recorded_twice(app, client):
    user_id, meeting_id = _make_user_and_meeting(app)
    first = _socket_client(app, client, user_id)
    second = _socket_client(app, client, user_id)

    assert first.emit("start_recording", {"meeting_id": meeting_id}, namespace=NS, callback=True)["ok"]
    ack = second.emit("start_recording", {"meeting_id": meeting_id}, namespace=NS, callback=True)
    assert ack == {"ok": False, "error": "already_recording"}


# --- TC-20: validation and DoS controls ---------------------------------------------

def test_invalid_start_payload_is_rejected(app, client):
    user_id, _ = _make_user_and_meeting(app)
    sio = _socket_client(app, client, user_id)

    for payload in ({"meeting_id": "not-a-uuid"}, "string", {"meeting_id": None}):
        ack = sio.emit("start_recording", payload, namespace=NS, callback=True)
        assert ack == {"ok": False, "error": "invalid_payload"}


def test_audio_before_start_and_malformed_audio_are_rejected(app, client, fakes):
    user_id, meeting_id = _make_user_and_meeting(app)
    sio = _socket_client(app, client, user_id)

    sio.emit("audio_chunk", b"\x00\x00" * 100, namespace=NS)
    assert _events(sio, "recording_error") == [{"error": "not_recording"}]

    sio.emit("start_recording", {"meeting_id": meeting_id}, namespace=NS, callback=True)
    sio.emit("audio_chunk", "not binary", namespace=NS)
    sio.emit("audio_chunk", b"\x00" * 3, namespace=NS)
    sio.emit("audio_chunk", b"\x00" * (app.config["TRANSCRIPTION_MAX_CHUNK_BYTES"] + 2), namespace=NS)
    assert _events(sio, "recording_error") == [{"error": "invalid_audio_chunk"}] * 3
    assert fakes.calls == []


def test_audio_faster_than_limit_is_dropped(app, client):
    app.config["TRANSCRIPTION_MAX_BYTES_PER_SECOND"] = 16000  # burst capacity = 32000 bytes
    user_id, meeting_id = _make_user_and_meeting(app)
    sio = _socket_client(app, client, user_id)
    sio.emit("start_recording", {"meeting_id": meeting_id}, namespace=NS, callback=True)

    _send(sio, b"\x00\x00" * 40000, chunk_bytes=16000)  # 80000 bytes sent instantly

    errors = _events(sio, "recording_error")
    assert {"error": "rate_limited"} in errors
    assert streaming.get_session(next(iter(streaming._sessions))).total_bytes <= 32000


def test_recording_stops_at_max_duration(app, client):
    app.config["TRANSCRIPTION_MAX_RECORDING_SECONDS"] = 1  # 32000 bytes
    user_id, meeting_id = _make_user_and_meeting(app)
    sio = _socket_client(app, client, user_id)
    sio.emit("start_recording", {"meeting_id": meeting_id}, namespace=NS, callback=True)

    _send(sio, b"\x00\x00" * 24000, chunk_bytes=16000)

    received = sio.get_received(NS)
    assert {"error": "max_duration_reached"} in [e["args"][0] for e in received if e["name"] == "recording_error"]
    assert [e["args"][0]["reason"] for e in received if e["name"] == "recording_stopped"] == ["max_duration_reached"]


def test_client_is_warned_once_when_transcription_lags(app, client):
    app.config["TRANSCRIPTION_LAG_WARNING_SECONDS"] = 1
    user_id, meeting_id = _make_user_and_meeting(app)
    sio = _socket_client(app, client, user_id)
    sio.emit("start_recording", {"meeting_id": meeting_id}, namespace=NS, callback=True)
    session = streaming.get_session(next(iter(streaming._sessions)))
    session.worker_running = True  # simulate a busy worker so audio piles up in the queue

    _send(sio, b"\x00\x00" * 32000, chunk_bytes=16000)  # 2s queued > 1s threshold

    assert _events(sio, "recording_error") == [{"error": "transcription_lagging"}]


# --- TC-09: diarization after stop --------------------------------------------------

def test_diarization_labels_segments_and_deletes_temp_audio(app, client, mocker):
    app.config["DIARIZATION_ENABLED"] = True
    diarizer = FakeDiarizer()
    app.extensions["transcription_diarizer"] = diarizer
    removed = mocker.spy(os, "remove")

    user_id, meeting_id = _make_user_and_meeting(app)
    sio = _socket_client(app, client, user_id)
    sio.emit("start_recording", {"meeting_id": meeting_id}, namespace=NS, callback=True)
    _send(sio, _speech_then_pause() + _speech_then_pause())
    sio.emit("stop_recording", namespace=NS, callback=True)

    assert diarizer.audio_len == 4 * SAMPLE_RATE
    labels = _events(sio, "speaker_labels")[0]["labels"]
    assert sorted(item["speaker_label"] for item in labels) == ["Speaker A", "Speaker B"]
    with app.app_context():
        rows = TranscriptSegment.query.filter_by(meeting_id=meeting_id).order_by(TranscriptSegment.start_ms).all()
        assert [r.speaker_label for r in rows] == ["Speaker A", "Speaker B"]

    temp_files = [c.args[0] for c in removed.call_args_list if "ma-rec-" in str(c.args[0])]
    assert len(temp_files) == 1 and not os.path.exists(temp_files[0])
