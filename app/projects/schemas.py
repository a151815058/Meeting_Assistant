"""Validation for project form input (REQ-20, REQ-64)."""
from itertools import zip_longest

from marshmallow import Schema, ValidationError, fields, validate, validates_schema

from app.models.project import PROJECT_STATUSES

MAX_DESCRIPTION_CHARS = 5000
MAX_STAKEHOLDERS = 50

STAKEHOLDER_FIELDS = ("name", "email", "role")


class StakeholderSchema(Schema):
    name = fields.String(required=True, validate=validate.Length(min=1, max=255))
    email = fields.Email(load_default=None, allow_none=True, validate=validate.Length(max=255))
    role = fields.String(load_default="", validate=validate.Length(max=255))


class ProjectSchema(Schema):
    name = fields.String(required=True, validate=validate.Length(min=1, max=255))
    description = fields.String(load_default="", validate=validate.Length(max=MAX_DESCRIPTION_CHARS))
    start_date = fields.Date(load_default=None, allow_none=True)
    end_date = fields.Date(load_default=None, allow_none=True)
    status = fields.String(load_default="active", validate=validate.OneOf(PROJECT_STATUSES))
    stakeholders = fields.List(fields.Nested(StakeholderSchema), load_default=list,
                               validate=validate.Length(max=MAX_STAKEHOLDERS))

    @validates_schema
    def _period(self, data, **_kwargs):
        if data.get("start_date") and data.get("end_date") and data["start_date"] > data["end_date"]:
            raise ValidationError("結束日期不能早於開始日期", "end_date")


def _one_line(value: str) -> str:
    return " ".join(value.split())


def form_to_raw(form) -> dict:
    """The project form as plain data for ProjectSchema. Stakeholders arrive as three parallel
    lists (one entry per table row); rows left completely blank are dropped."""
    stakeholders = []
    rows = zip_longest(*(form.getlist(f"stakeholder_{field}") for field in STAKEHOLDER_FIELDS), fillvalue="")
    for name, email, role in rows:
        row = {"name": _one_line(name), "email": email.strip().lower() or None, "role": _one_line(role)}
        if row["name"] or row["email"] or row["role"]:
            stakeholders.append(row)
    return {
        "name": _one_line(form.get("name", "")),
        "description": form.get("description", "").replace("\r\n", "\n").strip(),
        "start_date": form.get("start_date", "").strip() or None,
        "end_date": form.get("end_date", "").strip() or None,
        "status": form.get("status", "active"),
        "stakeholders": stakeholders,
    }


FIELD_LABELS = {"name": "專案名稱", "description": "專案說明", "start_date": "開始日期", "end_date": "結束日期",
                "status": "狀態", "stakeholders": "利害關係人"}
STAKEHOLDER_LABELS = {"name": "姓名", "email": "Email", "role": "角色／單位"}


def error_messages(err: ValidationError) -> list[str]:
    """Flash messages for a failed ProjectSchema load, naming the field (and stakeholder row)."""
    messages = []
    for field_name, detail in err.messages.items():
        label = FIELD_LABELS.get(field_name, field_name)
        if field_name == "stakeholders" and isinstance(detail, dict):
            for index, row_errors in sorted(detail.items()):
                for column in row_errors:
                    messages.append(f"利害關係人第 {int(index) + 1} 列：{STAKEHOLDER_LABELS.get(column, column)}格式不正確或未填")
        elif field_name == "end_date" and "結束日期不能早於開始日期" in detail:
            messages.append("結束日期不能早於開始日期")
        elif field_name == "stakeholders":
            messages.append(f"利害關係人最多 {MAX_STAKEHOLDERS} 位")
        else:
            messages.append(f"{label}格式不正確或未填")
    return messages
