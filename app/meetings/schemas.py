"""Validation for meeting form input (REQ-20)."""
from marshmallow import Schema, fields, validate


PLATFORMS = ("manual", "google_meet", "teams")


class MeetingSchema(Schema):
    title = fields.String(required=True, validate=validate.Length(min=1, max=500))
    platform = fields.String(load_default="manual", validate=validate.OneOf(PLATFORMS))
    platform_event_id = fields.String(load_default=None, allow_none=True,
                                      validate=[validate.Length(min=1, max=255),
                                                validate.Regexp(r"^[^\x00-\x20\x7f]+$")])  # URL-encoded when used


class ParticipantSchema(Schema):
    email = fields.Email(required=True, validate=validate.Length(max=255))
    display_name = fields.String(load_default="", validate=validate.Length(max=255))
