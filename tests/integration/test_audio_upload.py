"""TC-47: upload a recording -> transcript; the uploaded file is never kept."""
import io
import os
import tempfile
import wave

import numpy as np
import pytest

from app.extensions import db as _db
from app.extensions import socketio
from app.minutes import generator
from app.models.audit import AuditLog
from app.models.meeting import Meeting, TranscriptSegment
from app.models.user import User
from app.transcription import streaming
from app.transcription.asr import TranscriptionResult
from app.transcription.diarization import SpeakerTurn
from tests.unit.test_transcription_pipeline import energy_vad

NS = "/transcription"
JSON = {"Accept": "application/json"}


class FakeTranscriber:
    def __init__(self, fail_on=None):
        self.calls = 0
        self.fail_on = fail_on

    def transcribe(self, audio):
        self.calls += 1
        if self.calls == self.fail_on:
            raise RuntimeError("model crashed")
        return TranscriptionResult(text=f"檔案第{self.calls}段", confidence=0.8)


@pytest.fixture(autouse=True)
def fakes(app, tmp_path, monkeypatch):
    streaming.reset_sessions()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))  # uploads land here; must be empty afterwards
    transcriber = FakeTranscriber()
    app.extensions["transcription_vad"] = energy_vad
    app.extensions["transcription_asr"] = transcriber
    yield transcriber
    streaming.reset_sessions()


def _wav(pattern, rate=16000, channels=1) -> bytes:
    """pattern: [(seconds, is_speech), ...] -> WAV bytes (speech = constant 0.5 amplitude)."""
    parts = [np.full(int(rate * sec), 0.5 if speech else 0.0, dtype=np.float32) for sec, speech in pattern]
    mono = (np.concatenate(parts) * 32767).astype("<i2")
    frames = np.repeat(mono, channels) if channels > 1 else mono
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames.tobytes())
    return buf.getvalue()


TWO_UTTERANCES = [(1.0, True), (1.0, False), (1.0, True), (1.0, False)]


def _login_with_meeting(client, app, email="org@example.com", existing_end_ms=None):
    with app.app_context():
        user = User(email=email, display_name="Org")
        _db.session.add(user)
        _db.session.flush()
        meeting = Meeting(organizer_id=user.id, title="上傳測試", status="scheduled")
        _db.session.add(meeting)
        _db.session.flush()
        if existing_end_ms:
            _db.session.add(TranscriptSegment(meeting_id=meeting.id, start_ms=0, end_ms=existing_end_ms, text="現場錄音"))
        _db.session.commit()
        user_id, meeting_id = user.id, meeting.id
    with client.session_transaction() as sess:
        sess["_user_id"] = user_id
        sess["_fresh"] = True
    return user_id, meeting_id


def _upload(client, meeting_id, data: bytes, filename="meeting.wav", headers=JSON):
    return client.post(f"/meetings/{meeting_id}/audio-upload", headers=headers,
                       data={"audio": (io.BytesIO(data), filename)}, content_type="multipart/form-data")


def _uploads_left(tmp_path):
    # Only the app's own copies: the test client spools large request bodies to tmp files itself.
    return [f for f in os.listdir(tmp_path) if f.startswith("ma-upload-")]


def _watcher(app, client, meeting_id):
    sio = socketio.test_client(app, namespace=NS, flask_test_client=client)
    assert sio.emit("watch_meeting", {"meeting_id": meeting_id}, namespace=NS, callback=True) == {"ok": True, "busy": False}
    sio.get_received(NS)
    return sio


def _events(sio, name):
    return [e["args"][0] for e in sio.get_received(NS) if e["name"] == name]


def _audits(app, action):
    with app.app_context():
        return [a.event_metadata for a in AuditLog.query.filter_by(action=action).all()]


def _status(app, meeting_id):
    with app.app_context():
        return _db.session.get(Meeting, meeting_id).status


def test_uploaded_recording_is_transcribed_after_existing_transcript_and_file_is_deleted(app, client, tmp_path):
    _, meeting_id = _login_with_meeting(client, app, existing_end_ms=5000)
    sio = _watcher(app, client, meeting_id)

    resp = _upload(client, meeting_id, _wav(TWO_UTTERANCES))

    assert resp.status_code == 202 and resp.get_json() == {"ok": True}
    received = sio.get_received(NS)
    names = [e["name"] for e in received]
    assert names.count("transcript_segment") == 2 and names[-1] == "upload_finished"
    assert "upload_progress" in names
    progress = [e["args"][0] for e in received if e["name"] == "upload_progress"][-1]
    assert progress == {"processed_ms": 4000, "total_ms": 4000}
    assert received[-1]["args"][0] == {"segments": 2}

    with app.app_context():
        rows = TranscriptSegment.query.filter_by(meeting_id=meeting_id).order_by(TranscriptSegment.start_ms).all()
        assert [(r.start_ms, r.end_ms, r.text) for r in rows] == [
            (0, 5000, "現場錄音"), (5000, 6000, "檔案第1段"), (7000, 8000, "檔案第2段")]
    assert _status(app, meeting_id) == "transcribed"
    assert _audits(app, "transcription.upload_started") == [{"bytes": len(_wav(TWO_UTTERANCES)), "type": "wav"}]
    assert _audits(app, "transcription.upload_finished") == [{"audio_seconds": 4.0, "segments": 2}]
    assert _uploads_left(tmp_path) == []  # the recording is not kept
    assert not streaming.meeting_busy(meeting_id)


def test_stereo_44k_file_is_resampled(app, client, tmp_path):
    _, meeting_id = _login_with_meeting(client, app)
    resp = _upload(client, meeting_id, _wav(TWO_UTTERANCES, rate=44100, channels=2), filename="錄音 1.WAV")
    assert resp.status_code == 202
    with app.app_context():
        rows = TranscriptSegment.query.filter_by(meeting_id=meeting_id).order_by(TranscriptSegment.start_ms).all()
        assert len(rows) == 2
        assert abs(rows[1].start_ms - 2000) <= 50
    assert _uploads_left(tmp_path) == []


@pytest.mark.parametrize("data,filename,code", [
    (None, None, "no_file"),
    (b"hello", "notes.txt", "unsupported_type"),
    (b"RIFF", "noext", "unsupported_type"),
    (b"", "empty.wav", "empty_file"),
    (b"this is not audio at all" * 100, "fake.mp3", "invalid_audio"),
])
def test_invalid_uploads_are_rejected_and_nothing_is_kept(app, client, tmp_path, data, filename, code):
    _, meeting_id = _login_with_meeting(client, app)
    if data is None:
        resp = client.post(f"/meetings/{meeting_id}/audio-upload", headers=JSON, data={})
    else:
        resp = _upload(client, meeting_id, data, filename=filename)
    assert resp.status_code == 400
    assert resp.get_json()["error"] == code and resp.get_json()["message"]
    assert _status(app, meeting_id) == "scheduled"
    assert _uploads_left(tmp_path) == []
    assert not streaming.meeting_busy(meeting_id)


def test_size_and_length_limits(app, client, tmp_path):
    _, meeting_id = _login_with_meeting(client, app)
    data = _wav(TWO_UTTERANCES)

    app.config["TRANSCRIPTION_UPLOAD_MAX_BYTES"] = len(data) - 1
    assert _upload(client, meeting_id, data).get_json()["error"] == "file_too_large"

    app.config["TRANSCRIPTION_UPLOAD_MAX_BYTES"] = len(data)
    app.config["TRANSCRIPTION_MAX_RECORDING_SECONDS"] = 3
    assert _upload(client, meeting_id, data).get_json()["error"] == "too_long"

    app.config["MAX_CONTENT_LENGTH"] = 100  # oversized bodies are refused before parsing
    assert _upload(client, meeting_id, data).status_code == 413
    assert _uploads_left(tmp_path) == []


def test_file_without_duration_metadata_is_capped_while_decoding(app, client, tmp_path, monkeypatch):
    from app.transcription import upload

    monkeypatch.setattr(upload.AudioDecoder, "duration_ms", property(lambda self: None))
    _, meeting_id = _login_with_meeting(client, app)
    app.config["TRANSCRIPTION_MAX_RECORDING_SECONDS"] = 3
    sio = _watcher(app, client, meeting_id)

    assert _upload(client, meeting_id, _wav(TWO_UTTERANCES)).status_code == 202
    assert _events(sio, "upload_error") == [{"error": "too_long", "segments": 0}]
    assert _status(app, meeting_id) == "scheduled"
    assert _uploads_left(tmp_path) == []


def test_failure_mid_file_keeps_transcribed_part_and_deletes_file(app, client, tmp_path, fakes):
    fakes.fail_on = 2
    _, meeting_id = _login_with_meeting(client, app)
    sio = _watcher(app, client, meeting_id)

    assert _upload(client, meeting_id, _wav(TWO_UTTERANCES)).status_code == 202

    assert _events(sio, "upload_error") == [{"error": "transcription_failed", "segments": 1}]
    with app.app_context():
        assert [r.text for r in TranscriptSegment.query.filter_by(meeting_id=meeting_id)] == ["檔案第1段"]
    assert _status(app, meeting_id) == "transcribed"
    assert _audits(app, "transcription.upload_failed") == [{"error": "transcription_failed", "segments": 1}]
    assert _uploads_left(tmp_path) == []
    assert not streaming.meeting_busy(meeting_id)


def test_upload_and_live_recording_exclude_each_other(app, client, tmp_path):
    user_id, meeting_id = _login_with_meeting(client, app)

    streaming.claim_meeting(meeting_id, "upload:running")  # an upload is being transcribed
    sio = socketio.test_client(app, namespace=NS, flask_test_client=client)
    assert sio.emit("watch_meeting", {"meeting_id": meeting_id}, namespace=NS, callback=True) == {"ok": True, "busy": True}
    ack = sio.emit("start_recording", {"meeting_id": meeting_id}, namespace=NS, callback=True)
    assert ack == {"ok": False, "error": "already_recording"}
    assert _upload(client, meeting_id, _wav(TWO_UTTERANCES)).get_json()["error"] == "busy"
    streaming.release_meeting(meeting_id, "upload:running")

    assert sio.emit("start_recording", {"meeting_id": meeting_id}, namespace=NS, callback=True)["ok"] is True
    assert _upload(client, meeting_id, _wav(TWO_UTTERANCES)).get_json()["error"] == "busy"
    assert _uploads_left(tmp_path) == []


def test_no_upload_while_minutes_are_generated_and_minutes_wait_for_upload(app, client):
    user_id, meeting_id = _login_with_meeting(client, app, existing_end_ms=1000)
    with app.app_context():
        meeting = _db.session.get(Meeting, meeting_id)
        meeting.status = "processing"
        _db.session.commit()
    assert _upload(client, meeting_id, _wav(TWO_UTTERANCES)).get_json()["error"] == "minutes_in_progress"

    with app.app_context():
        meeting = _db.session.get(Meeting, meeting_id)
        meeting.status = "transcribing"
        _db.session.commit()
        streaming.claim_meeting(meeting_id, "upload:running")
        with pytest.raises(generator.GenerationRejected) as exc:
            generator.start_generation(app, meeting, None, user_id)
        assert exc.value.code == "transcription_in_progress"
        streaming.release_meeting(meeting_id, "upload:running")


def test_upload_requires_login(app, client, tmp_path):
    _, meeting_id = _login_with_meeting(app.test_client(), app)
    assert _upload(client, meeting_id, _wav(TWO_UTTERANCES)).status_code == 302
    assert _uploads_left(tmp_path) == []


def test_only_the_organizer_can_upload_or_watch(app, client, tmp_path):
    with app.app_context():
        owner = User(email="owner@example.com", display_name="Owner")
        _db.session.add(owner)
        _db.session.flush()
        meeting = Meeting(organizer_id=owner.id, title="別人的會議")
        _db.session.add(meeting)
        _db.session.commit()
        meeting_id = meeting.id

    other = client
    _login_with_meeting(other, app, email="other@example.com")

    assert _upload(other, meeting_id, _wav(TWO_UTTERANCES)).status_code == 404
    sio = socketio.test_client(app, namespace=NS, flask_test_client=other)
    assert sio.emit("watch_meeting", {"meeting_id": meeting_id}, namespace=NS, callback=True) == {"ok": False, "error": "not_found"}
    assert sio.emit("watch_meeting", "bad", namespace=NS, callback=True) == {"ok": False, "error": "invalid_payload"}
    assert _uploads_left(tmp_path) == []


def test_plain_form_post_redirects_back_to_live_tab(app, client):
    _, meeting_id = _login_with_meeting(client, app)
    resp = _upload(client, meeting_id, b"x", filename="a.txt", headers={})
    assert resp.status_code == 302 and resp.headers["Location"].endswith(f"/meetings/{meeting_id}#live")
    assert "不支援的檔案格式" in client.get(f"/meetings/{meeting_id}").get_data(as_text=True)

    resp = _upload(client, meeting_id, _wav(TWO_UTTERANCES), headers={})
    assert resp.headers["Location"].endswith("#live")
    assert "錄音檔已上傳，正在轉錄" in client.get(f"/meetings/{meeting_id}").get_data(as_text=True)


def test_uploaded_recording_gets_speaker_labels_when_diarization_enabled(app, client, tmp_path):
    class FakeDiarizer:
        def diarize(self, audio):
            return [SpeakerTurn(0, 1500, "spk1"), SpeakerTurn(1500, 99999, "spk2")]

    app.config["DIARIZATION_ENABLED"] = True
    app.extensions["transcription_diarizer"] = FakeDiarizer()
    _, meeting_id = _login_with_meeting(client, app)

    assert _upload(client, meeting_id, _wav(TWO_UTTERANCES)).status_code == 202
    with app.app_context():
        rows = TranscriptSegment.query.filter_by(meeting_id=meeting_id).order_by(TranscriptSegment.start_ms).all()
        assert [r.speaker_label for r in rows] == ["Speaker A", "Speaker B"]
    assert _uploads_left(tmp_path) == []


def test_live_tab_has_upload_form(app, client):
    _, meeting_id = _login_with_meeting(client, app)
    html = client.get(f"/meetings/{meeting_id}").get_data(as_text=True)
    live = html[html.index('id="panel-live"'):html.index('id="panel-minutes"')]
    assert 'id="upload-form"' in live and 'enctype="multipart/form-data"' in live
    assert f'action="/meetings/{meeting_id}/audio-upload"' in live
    assert 'name="audio"' in live and "錄音檔轉完後立即刪除，不會保存" in live
    assert 'data-label-transcribing="轉錄中"' in html
