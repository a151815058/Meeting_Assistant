import pytest
from sqlalchemy.exc import IntegrityError

from app.models.meeting import Meeting, Participant
from app.models.user import OAuthAccount, User


def _make_user(db, email="alice@example.com"):
    user = User(email=email, display_name="Alice")
    db.session.add(user)
    db.session.commit()
    return user


def test_oauth_account_refresh_token_stored_encrypted_at_rest(app, db):
    with app.app_context():
        user = _make_user(db)
        account = OAuthAccount(user_id=user.id, provider="google", provider_account_id="g-123")
        account.refresh_token = "super-secret-refresh-token"
        db.session.add(account)
        db.session.commit()

        assert account._refresh_token_encrypted != "super-secret-refresh-token"
        assert account.refresh_token == "super-secret-refresh-token"


def test_oauth_account_unique_per_user_and_provider(app, db):
    with app.app_context():
        user = _make_user(db)
        db.session.add(OAuthAccount(user_id=user.id, provider="google", provider_account_id="g-1"))
        db.session.commit()

        db.session.add(OAuthAccount(user_id=user.id, provider="google", provider_account_id="g-2"))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


def test_participant_unique_per_meeting_and_email(app, db):
    with app.app_context():
        user = _make_user(db)
        meeting = Meeting(organizer_id=user.id, title="Weekly sync", platform="manual")
        db.session.add(meeting)
        db.session.commit()

        db.session.add(Participant(meeting_id=meeting.id, email="bob@example.com"))
        db.session.commit()

        db.session.add(Participant(meeting_id=meeting.id, email="bob@example.com"))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()
