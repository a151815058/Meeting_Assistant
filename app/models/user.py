import uuid
from datetime import datetime, timezone

from flask_login import UserMixin

from app.extensions import db
from app.security.crypto import decrypt_token, encrypt_token


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    display_name = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False)

    oauth_accounts = db.relationship("OAuthAccount", back_populates="user", cascade="all, delete-orphan")
    meetings = db.relationship("Meeting", back_populates="organizer", cascade="all, delete-orphan")

    def get_id(self) -> str:
        return self.id

    def __repr__(self) -> str:
        return f"<User {self.email}>"


class OAuthAccount(db.Model):
    """One row per (user, provider). Refresh tokens are stored encrypted at rest.

    provider: "google" | "microsoft"
    scopes: space-separated scope string as granted by the provider.
    """

    __tablename__ = "oauth_accounts"
    __table_args__ = (
        db.UniqueConstraint("user_id", "provider", name="uq_oauth_account_user_provider"),
    )

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    user_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False)
    provider = db.Column(db.String(20), nullable=False)
    provider_account_id = db.Column(db.String(255), nullable=False)
    scopes = db.Column(db.String(1024), nullable=False, default="")

    _refresh_token_encrypted = db.Column("refresh_token_encrypted", db.Text, nullable=True)
    access_token_expires_at = db.Column(db.DateTime(timezone=True), nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    user = db.relationship("User", back_populates="oauth_accounts")

    @property
    def refresh_token(self) -> str | None:
        if not self._refresh_token_encrypted:
            return None
        return decrypt_token(self._refresh_token_encrypted)

    @refresh_token.setter
    def refresh_token(self, value: str | None) -> None:
        self._refresh_token_encrypted = encrypt_token(value) if value else None

    def has_scope(self, scope: str) -> bool:
        return scope in (self.scopes or "").split()

    def __repr__(self) -> str:
        return f"<OAuthAccount {self.provider}:{self.provider_account_id}>"
