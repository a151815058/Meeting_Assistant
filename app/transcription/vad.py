"""Streaming speech segmentation with Silero VAD (REQ-03).

Audio arrives as a continuous stream of small chunks. ``SpeechSegmenter`` keeps
a bounded buffer of not-yet-transcribed audio, periodically runs VAD over it,
and cuts out an utterance once the speaker pauses (or once the utterance gets
too long). Silence is discarded, so only speech is sent to Whisper.

The VAD function is injectable so the segmentation logic can be unit-tested
without loading the ONNX model.
"""
from dataclasses import dataclass
from typing import Callable

import numpy as np

SAMPLE_RATE = 16000

# (audio float32 [-1, 1]) -> [{"start": sample_idx, "end": sample_idx}, ...]
VadFn = Callable[[np.ndarray], list[dict]]


@dataclass
class SpeechChunk:
    """An utterance cut from the stream. Sample offsets are relative to the session start."""

    start_sample: int
    end_sample: int
    audio: np.ndarray

    @property
    def start_ms(self) -> int:
        return self.start_sample * 1000 // SAMPLE_RATE

    @property
    def end_ms(self) -> int:
        return self.end_sample * 1000 // SAMPLE_RATE


def silero_vad_fn(min_silence_ms: int = 600, speech_pad_ms: int = 200,
                  min_speech_ms: int = 250) -> VadFn:
    """Silero VAD bundled with faster-whisper (ONNX, no torch dependency)."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    options = VadOptions(
        min_silence_duration_ms=min_silence_ms,
        speech_pad_ms=speech_pad_ms,
        min_speech_duration_ms=min_speech_ms,
    )

    def _vad(audio: np.ndarray) -> list[dict]:
        return get_speech_timestamps(audio, options, sampling_rate=SAMPLE_RATE)

    return _vad


class SpeechSegmenter:
    def __init__(self, vad_fn: VadFn, *, end_silence_ms: int = 600, max_segment_s: float = 15.0,
                 vad_interval_ms: int = 500, keep_tail_ms: int = 500):
        self._vad = vad_fn
        self._end_silence = SAMPLE_RATE * end_silence_ms // 1000
        self._max_segment = int(SAMPLE_RATE * max_segment_s)
        self._vad_interval = SAMPLE_RATE * vad_interval_ms // 1000
        self._keep_tail = SAMPLE_RATE * keep_tail_ms // 1000

        self._buffer = np.zeros(0, dtype=np.float32)
        self._buffer_start = 0  # absolute sample index of self._buffer[0]
        self._since_vad = 0

    def feed(self, audio: np.ndarray) -> list[SpeechChunk]:
        self._buffer = np.concatenate([self._buffer, audio.astype(np.float32, copy=False)])
        self._since_vad += len(audio)
        if self._since_vad < self._vad_interval:
            return []
        self._since_vad = 0
        return self._segment(final=False)

    def flush(self) -> list[SpeechChunk]:
        """Emit whatever speech remains (called when recording stops)."""
        chunks = self._segment(final=True) if len(self._buffer) else []
        self._drop(len(self._buffer))
        return chunks

    def _segment(self, *, final: bool) -> list[SpeechChunk]:
        speeches = self._vad(self._buffer)
        length = len(self._buffer)

        if not speeches:
            # Pure silence: keep only a short tail in case speech starts right at the boundary.
            self._drop(max(0, length - self._keep_tail))
            return []

        first_start = speeches[0]["start"]
        if final:
            return [self._cut(first_start, speeches[-1]["end"])]

        # Utterances followed by enough silence are complete.
        finished = [s for s in speeches if s["end"] + self._end_silence <= length]
        if finished:
            return [self._cut(first_start, finished[-1]["end"])]

        # Speaker has not paused for too long: force a cut so latency stays bounded.
        if length - first_start >= self._max_segment:
            return [self._cut(first_start, length)]

        # Speech still in progress: discard leading silence, keep the rest.
        self._drop(first_start)
        return []

    def _cut(self, start: int, end: int) -> SpeechChunk:
        chunk = SpeechChunk(
            start_sample=self._buffer_start + start,
            end_sample=self._buffer_start + end,
            audio=self._buffer[start:end].copy(),
        )
        self._drop(end)
        return chunk

    def _drop(self, n: int) -> None:
        self._buffer = self._buffer[n:]
        self._buffer_start += n
