from types import SimpleNamespace

import numpy as np
import pytest
from marshmallow import ValidationError

from app.transcription.asr import WhisperTranscriber
from app.transcription.diarization import SpeakerTurn, assign_speaker_labels
from app.transcription.schemas import StartRecordingSchema, validate_audio_chunk
from app.transcription.streaming import TokenBucket
from app.transcription.vad import SAMPLE_RATE, SpeechSegmenter


def energy_vad(audio):
    """Fake VAD: any sample with |x| > 0.1 is speech; contiguous runs form segments."""
    speech = np.abs(audio) > 0.1
    segments, start = [], None
    for i, is_speech in enumerate(speech):
        if is_speech and start is None:
            start = i
        elif not is_speech and start is not None:
            segments.append({"start": start, "end": i})
            start = None
    if start is not None:
        segments.append({"start": start, "end": len(audio)})
    return segments


def tone(seconds):
    return np.full(int(SAMPLE_RATE * seconds), 0.5, dtype=np.float32)


def silence(seconds):
    return np.zeros(int(SAMPLE_RATE * seconds), dtype=np.float32)


def feed_in_chunks(segmenter, audio, chunk_s=0.25):
    step = int(SAMPLE_RATE * chunk_s)
    out = []
    for i in range(0, len(audio), step):
        out.extend(segmenter.feed(audio[i:i + step]))
    return out


# --- TC-03: VAD segmentation -------------------------------------------------------

def test_segmenter_cuts_utterance_after_pause_and_drops_silence():
    seg = SpeechSegmenter(energy_vad)
    chunks = feed_in_chunks(seg, np.concatenate([silence(1), tone(2), silence(1)]))

    assert len(chunks) == 1
    assert chunks[0].start_ms == 1000
    assert chunks[0].end_ms == 3000
    assert np.all(np.abs(chunks[0].audio) > 0.1)  # leading/trailing silence not sent to ASR


def test_segmenter_emits_nothing_for_pure_silence():
    seg = SpeechSegmenter(energy_vad)
    assert feed_in_chunks(seg, silence(5)) == []
    assert seg.flush() == []


def test_segmenter_splits_two_utterances_with_absolute_offsets():
    seg = SpeechSegmenter(energy_vad)
    audio = np.concatenate([tone(1), silence(1), tone(1.5), silence(1)])
    chunks = feed_in_chunks(seg, audio)

    assert [(c.start_ms, c.end_ms) for c in chunks] == [(0, 1000), (2000, 3500)]


def test_segmenter_forces_cut_on_long_continuous_speech():
    seg = SpeechSegmenter(energy_vad, max_segment_s=5)
    chunks = feed_in_chunks(seg, tone(12))

    assert len(chunks) >= 2
    assert all(c.end_ms - c.start_ms <= 5500 for c in chunks)


def test_segmenter_flush_returns_speech_in_progress():
    seg = SpeechSegmenter(energy_vad)
    assert feed_in_chunks(seg, np.concatenate([silence(0.5), tone(1)])) == []

    chunks = seg.flush()
    assert [(c.start_ms, c.end_ms) for c in chunks] == [(500, 1500)]


# --- TC-04: Whisper wrapper --------------------------------------------------------

def _fake_segment(text, avg_logprob=-0.2, no_speech_prob=0.05):
    return SimpleNamespace(text=text, avg_logprob=avg_logprob, no_speech_prob=no_speech_prob)


def test_transcriber_joins_segments_and_filters_hallucinations(mocker):
    model = mocker.Mock()
    model.transcribe.return_value = (
        [_fake_segment("大家好，"), _fake_segment("今天討論預算。"),
         _fake_segment("請訂閱", avg_logprob=-1.5, no_speech_prob=0.9)],
        None,
    )
    transcriber = WhisperTranscriber("tiny", language="zh", initial_prompt="繁體中文")
    transcriber._model = model

    result = transcriber.transcribe(tone(1))

    assert result.text == "大家好，今天討論預算。"
    assert 0 < result.confidence <= 1
    kwargs = model.transcribe.call_args.kwargs
    assert kwargs["language"] == "zh"
    assert kwargs["vad_filter"] is False


def test_transcriber_returns_empty_when_nothing_recognised(mocker):
    transcriber = WhisperTranscriber("tiny")
    transcriber._model = mocker.Mock(transcribe=mocker.Mock(return_value=([], None)))

    result = transcriber.transcribe(tone(1))
    assert result.text == ""
    assert result.confidence is None


# --- TC-09: speaker label assignment ------------------------------------------------

def test_assign_speaker_labels_by_overlap_in_order_of_appearance():
    segments = [("s1", 0, 2000), ("s2", 2500, 4000), ("s3", 4200, 6000), ("s4", 9000, 9500)]
    turns = [
        SpeakerTurn(0, 2100, "SPEAKER_07"),
        SpeakerTurn(2400, 4100, "SPEAKER_02"),
        SpeakerTurn(4100, 6100, "SPEAKER_07"),
    ]

    labels = assign_speaker_labels(segments, turns)

    assert labels == {"s1": "Speaker A", "s2": "Speaker B", "s3": "Speaker A"}  # s4 overlaps no turn


# --- TC-20: payload validation ------------------------------------------------------

def test_start_recording_schema_accepts_uuid_and_rejects_bad_input():
    good = StartRecordingSchema().load({"meeting_id": "3f2b6c1e-8d4a-4c2b-9f1e-2a7d5b9c0e11"})
    assert good["meeting_id"].startswith("3f2b")

    for bad in ({}, {"meeting_id": "1 OR 1=1"}, {"meeting_id": 42},
                {"meeting_id": "3f2b6c1e-8d4a-4c2b-9f1e-2a7d5b9c0e11", "extra": "x"}):
        with pytest.raises(ValidationError):
            StartRecordingSchema().load(bad)


@pytest.mark.parametrize("data", ["text", b"", b"\x00" * 3, b"\x00" * 32002, {"a": 1}, None],
                         ids=["str", "empty", "odd-length", "too-large", "dict", "none"])
def test_validate_audio_chunk_rejects_invalid(data):
    with pytest.raises(ValidationError):
        validate_audio_chunk(data, max_bytes=32000)


def test_validate_audio_chunk_accepts_pcm16():
    assert validate_audio_chunk(bytearray(b"\x01\x00" * 100), max_bytes=32000) == b"\x01\x00" * 100


def test_token_bucket_limits_byte_rate():
    now = [0.0]
    bucket = TokenBucket(rate=100, capacity=200, clock=lambda: now[0])

    assert bucket.allow(150)
    assert not bucket.allow(100)  # only 50 left
    now[0] += 1.0                 # +100 tokens
    assert bucket.allow(100)
