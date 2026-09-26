"""Validation for template and minutes form input (REQ-20)."""
from marshmallow import Schema, ValidationError, fields, validate, validates

from app.minutes.prompting import TemplateError, validate_template_body

MAX_TEMPLATE_CHARS = 20_000
MAX_MINUTES_CHARS = 100_000


class TemplateSchema(Schema):
    name = fields.String(required=True, validate=validate.Length(min=1, max=255))
    description = fields.String(load_default="", validate=validate.Length(max=1000))
    body = fields.String(required=True, validate=validate.Length(min=1, max=MAX_TEMPLATE_CHARS))

    @validates("body")
    def _renders_in_sandbox(self, value, **_kwargs):
        try:
            validate_template_body(value)
        except TemplateError as exc:
            raise ValidationError(f"範本語法錯誤或使用了不允許的語法：{exc}") from exc


class MinutesEditSchema(Schema):
    content_markdown = fields.String(required=True, validate=validate.Length(min=1, max=MAX_MINUTES_CHARS))


def load_form(schema: Schema, form, keys: tuple[str, ...]) -> dict:
    """Validate only the expected keys (the form also carries csrf_token etc.)."""
    return schema.load({k: form.get(k, "").replace("\r\n", "\n").strip() for k in keys if k in form})
