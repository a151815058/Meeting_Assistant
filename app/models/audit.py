import uuid
from datetime import datetime, timezone

from app.extensions import db


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AuditLog(db.Model):
    """Append-only audit trail for sensitive actions (SSDLC control AC-1).

    Written for: OAuth grant/revoke, meeting minutes generation, email
    dispatch, template create/update/delete.
    """

    __tablename__ = "audit_log"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    actor_user_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=True)

    action = db.Column(db.String(100), nullable=False)
    target_type = db.Column(db.String(50), nullable=False)
    target_id = db.Column(db.String(36), nullable=True)
    event_metadata = db.Column(db.JSON, nullable=False, default=dict)

    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False, index=True)

    def __repr__(self) -> str:
        return f"<AuditLog {self.action} on {self.target_type}:{self.target_id}>"
