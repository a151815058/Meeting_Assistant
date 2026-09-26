from app.extensions import db
from app.models.audit import AuditLog


def record_audit_event(*, actor_user_id, action: str, target_type: str, target_id: str | None = None,
                        metadata: dict | None = None) -> AuditLog:
    """Write a tamper-evident audit trail entry for a sensitive action.

    Called for: OAuth grant/revoke, meeting minutes generation, email dispatch,
    template changes. Required by threat_model.md control AC-1.
    """
    entry = AuditLog(
        actor_user_id=actor_user_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        event_metadata=metadata or {},
    )
    db.session.add(entry)
    db.session.commit()
    return entry
