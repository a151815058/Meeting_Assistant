"""Speaker diarization (REQ-09): cluster transcript segments into Speaker A/B/C…

Runs once after a recording stops, over that recording's audio. Labels are
voice clusters, NOT verified identities (see RTM RISK-01); a verified identity
from Phase 6 goes into ``TranscriptSegment.platform_speaker_id`` instead.

pyannote.audio (and torch) are optional: install ``requirements-diarization.txt``
and set DIARIZATION_ENABLED=true + HF_TOKEN to turn this on.
"""
import string
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from app.transcription.vad import SAMPLE_RATE


@dataclass
class SpeakerTurn:
    start_ms: int
    end_ms: int
    speaker: str  # raw cluster id from the diarization model


class Diarizer(Protocol):
    def diarize(self, audio: np.ndarray) -> list[SpeakerTurn]: ...


class PyannoteDiarizer:
    def __init__(self, model: str, hf_token: str):
        if not hf_token:
            raise ValueError("DIARIZATION_ENABLED requires HF_TOKEN")
        self._model = model
        self._hf_token = hf_token
        self._pipeline = None

    def _get_pipeline(self):
        if self._pipeline is None:
            from pyannote.audio import Pipeline

            self._pipeline = Pipeline.from_pretrained(self._model, token=self._hf_token)
        return self._pipeline

    def diarize(self, audio: np.ndarray) -> list[SpeakerTurn]:
        import torch

        waveform = torch.from_numpy(audio.astype(np.float32)).unsqueeze(0)
        output = self._get_pipeline()({"waveform": waveform, "sample_rate": SAMPLE_RATE})
        # pyannote 4.x wraps the Annotation; 3.x returns it directly.
        annotation = getattr(output, "speaker_diarization", output)
        return [
            SpeakerTurn(int(turn.start * 1000), int(turn.end * 1000), str(speaker))
            for turn, _, speaker in annotation.itertracks(yield_label=True)
        ]


def assign_speaker_labels(segments: list[tuple[str, int, int]], turns: list[SpeakerTurn]) -> dict[str, str]:
    """Map each (segment_id, start_ms, end_ms) to "Speaker A/B/…" by largest time overlap.

    Letters are assigned in order of first appearance so the first voice heard is Speaker A.
    Segments that overlap no turn get no label.
    """
    raw_by_segment: dict[str, str] = {}
    for seg_id, start, end in sorted(segments, key=lambda s: s[1]):
        best, best_overlap = None, 0
        for turn in turns:
            overlap = min(end, turn.end_ms) - max(start, turn.start_ms)
            if overlap > best_overlap:
                best, best_overlap = turn.speaker, overlap
        if best is not None:
            raw_by_segment[seg_id] = best

    letters: dict[str, str] = {}
    for raw in raw_by_segment.values():
        if raw not in letters:
            letters[raw] = _speaker_name(len(letters))
    return {seg_id: letters[raw] for seg_id, raw in raw_by_segment.items()}


def _speaker_name(index: int) -> str:
    letters = string.ascii_uppercase
    suffix = letters[index % 26] + (str(index // 26) if index >= 26 else "")
    return f"Speaker {suffix}"
