"""Faster-Whisper speech recognition (REQ-04)."""
import math
import threading
from dataclasses import dataclass

import numpy as np

# Whisper tends to "hear" these on noise/silence; drop segments it is unsure about.
_NO_SPEECH_PROB_MAX = 0.6
_AVG_LOGPROB_MIN = -1.0


@dataclass
class TranscriptionResult:
    text: str
    confidence: float | None


class WhisperTranscriber:
    """Lazily loads the model on first use; safe to share between threads."""

    def __init__(self, model_size: str, device: str = "cpu", compute_type: str = "int8",
                 language: str | None = "zh", initial_prompt: str | None = None, cpu_threads: int = 0):
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._cpu_threads = cpu_threads
        self._language = language
        self._initial_prompt = initial_prompt
        self._model = None
        self._load_lock = threading.Lock()

    def load(self) -> None:
        self._get_model()

    def _get_model(self):
        with self._load_lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                self._model = WhisperModel(self._model_size, device=self._device,
                                           compute_type=self._compute_type, cpu_threads=self._cpu_threads)
            return self._model

    def transcribe(self, audio: np.ndarray) -> TranscriptionResult:
        segments, _info = self._get_model().transcribe(
            audio,
            language=self._language,
            initial_prompt=self._initial_prompt,
            beam_size=5,
            vad_filter=False,  # audio is already VAD-segmented upstream
            condition_on_previous_text=False,
        )
        kept = [
            s for s in segments
            if s.text.strip()
            and not (s.no_speech_prob > _NO_SPEECH_PROB_MAX and s.avg_logprob < _AVG_LOGPROB_MIN)
        ]
        if not kept:
            return TranscriptionResult(text="", confidence=None)

        text = "".join(s.text for s in kept).strip()
        confidence = sum(math.exp(s.avg_logprob) for s in kept) / len(kept)
        return TranscriptionResult(text=text, confidence=round(confidence, 4))
