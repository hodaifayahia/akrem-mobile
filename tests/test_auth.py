"""Authentication service tests."""

from datetime import datetime, timedelta, timezone

import bcrypt
import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import User
from app.db.session import session_scope
from app.services import auth


def _create_owner(
    session: Session, *, username: str = "owner", password: str = "secret1"
) -> User:
    """Create the first owner for a test database."""
    return auth.create_first_owner(
        session,
        username=username,
        password=password,
        password_confirmation=password,
    )


def test_first_owner_hashes_password_and_only_allows_one(memory_engine: Engine) -> None:
    """The initial account is an owner with a real salted bcrypt hash."""
    with session_scope(memory_engine) as session:
        assert not auth.owner_exists(session)
        user = _create_owner(session)
        assert user.role == "owner"
        assert not user.disabled
        assert user.password_hash != "secret1"
        assert user.password_hash.startswith(("$2a$", "$2b$", "$2y$"))
        assert bcrypt.checkpw(b"secret1", user.password_hash.encode("ascii"))
        assert auth.verify_password("secret1", user.password_hash)
        assert not auth.verify_password("incorrect", user.password_hash)
        assert auth.owner_exists(session)
        with pytest.raises(ValueError, match="already been created"):
            _create_owner(session, username="another")


@pytest.mark.parametrize(
    ("password", "confirmation", "error"),
    [
        ("short", "short", "at least 6"),
        ("secret1", "different", "does not match"),
        ("x" * 73, "x" * 73, "72 UTF-8 bytes"),
    ],
)
def test_first_owner_validates_password(
    password: str, confirmation: str, error: str, memory_engine: Engine
) -> None:
    """Setup requires a confirmed password of acceptable length."""
    with session_scope(memory_engine) as session:
        with pytest.raises(ValueError, match=error):
            auth.create_first_owner(
                session,
                username="owner",
                password=password,
                password_confirmation=confirmation,
            )
        assert session.scalar(select(User.id)) is None


def test_login_tracks_bad_attempts_and_locks_for_five_minutes(memory_engine: Engine) -> None:
    """The fifth invalid attempt starts a five-minute lockout and persists it."""
    start = datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)
    with session_scope(memory_engine) as session:
        user = _create_owner(session)
        user_id = user.id

    for attempt in range(1, auth.MAX_FAILED_ATTEMPTS + 1):
        with session_scope(memory_engine) as session:
            result = auth.authenticate(
                session, username="owner", password="wrongpass", now=start
            )
            expected = "locked" if attempt == auth.MAX_FAILED_ATTEMPTS else "invalid_credentials"
            assert result.status == expected
            assert result.user is None

    with session_scope(memory_engine) as session:
        user = session.get(User, user_id)
        assert user is not None
        assert user.failed_attempts == 5
        assert user.locked_until == datetime(2026, 9, 30, 10, 5)
        result = auth.authenticate(
            session,
            username="owner",
            password="secret1",
            now=start + timedelta(minutes=4, seconds=59),
        )
        assert result.status == "locked"

    with session_scope(memory_engine) as session:
        result = auth.authenticate(
            session,
            username="owner",
            password="secret1",
            now=start + timedelta(minutes=5),
        )
        assert result.succeeded
        assert result.user is not None
        assert result.user.last_activity_at == datetime(2026, 9, 30, 10, 5, tzinfo=timezone.utc)
        assert result.user.failed_attempts == 0
        assert result.user.locked_until is None


def test_login_resets_failed_attempts_and_reports_unknown_user(memory_engine: Engine) -> None:
    """A successful login clears earlier failures; missing names are generic failures."""
    with session_scope(memory_engine) as session:
        user = _create_owner(session)
        user.failed_attempts = 2
        assert auth.authenticate(
            session, username="owner", password="secret1"
        ).succeeded
        assert user.failed_attempts == 0
        missing = auth.authenticate(session, username="missing", password="secret1")
        assert missing.status == "invalid_credentials"
        assert missing.user is None


def test_disabled_account_cannot_authenticate_or_pass_role_check(memory_engine: Engine) -> None:
    """Disabled users cannot log in and authorization functions reject them."""
    with session_scope(memory_engine) as session:
        owner = _create_owner(session)
        owner.disabled = True
        result = auth.authenticate(session, username="owner", password="secret1")
        assert result.status == "disabled"
        with pytest.raises(auth.AuthorizationError):
            auth.require_owner(session, owner.id)


def test_role_checks_and_password_change(memory_engine: Engine) -> None:
    """Role guards enforce owner/seller separation and users can change passwords."""
    with session_scope(memory_engine) as session:
        owner = _create_owner(session)
        seller = User(
            username="seller",
            password_hash=auth.hash_password("seller1"),
            role="seller",
        )
        session.add(seller)
        session.flush()

        assert auth.is_owner(owner)
        assert not auth.is_seller(owner)
        assert auth.is_seller(seller)
        assert auth.require_owner(session, owner.id).id == owner.id
        assert auth.require_seller(session, seller.id).id == seller.id
        with pytest.raises(auth.AuthorizationError):
            auth.require_owner(session, seller.id)

        old_hash = owner.password_hash
        auth.change_password(
            session,
            user_id=owner.id,
            current_password="secret1",
            new_password="newpass",
            password_confirmation="newpass",
        )
        assert owner.password_hash != old_hash
        assert auth.verify_password("newpass", owner.password_hash)
        assert not auth.verify_password("secret1", owner.password_hash)
        assert auth.authenticate(
            session, username="owner", password="newpass"
        ).succeeded


@pytest.mark.parametrize(
    ("current", "new", "confirmation", "error"),
    [
        ("incorrect", "newpass", "newpass", "Current password"),
        ("secret1", "short", "short", "at least 6"),
        ("secret1", "newpass", "different", "does not match"),
    ],
)
def test_password_change_validates_credentials(
    current: str, new: str, confirmation: str, error: str, memory_engine: Engine
) -> None:
    """Password changes reject wrong current passwords and invalid replacements."""
    with session_scope(memory_engine) as session:
        user = _create_owner(session)
        with pytest.raises(ValueError, match=error):
            auth.change_password(
                session,
                user_id=user.id,
                current_password=current,
                new_password=new,
                password_confirmation=confirmation,
            )
        assert auth.verify_password("secret1", user.password_hash)
