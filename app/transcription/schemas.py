"""Validation for every client-supplied WebSocket payload (REQ-20, threat_model.md Tampering)."""
import uuid

from marshmallow import Schema, ValidationError, fields, validates


class StartRecordingSchema(Schema):
    # Unknown keys are rejected (marshmallow default: RAISE).
    meeting_id = fields.String(required=True)

    @validates("meeting_id")
    def _is_uuid(self, value, **_kwargs):
        try:
            uuid.UUID(value)
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValidationError("meeting_id must be a UUID") from exc


def validate_audio_chunk(data, max_bytes: int) -> bytes:
    """Audio chunks are raw 16 kHz mono PCM16 (little-endian) sent as a binary frame."""
    if not isinstance(data, (bytes, bytearray)):
        raise ValidationError("audio chunk must be binary")
    if not 0 < len(data) <= max_bytes:
        raise ValidationError(f"audio chunk must be 1..{max_bytes} bytes")
    if len(data) % 2:
        raise ValidationError("audio chunk must contain whole 16-bit samples")
    return bytes(data)
