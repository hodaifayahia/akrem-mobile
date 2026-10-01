"""Authentication and role authorization services."""

from __future__ import annotations

import bcrypt
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import User
from app.services import two_factor

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_DURATION = timedelta(minutes=5)
MIN_PASSWORD_LENGTH = 6
MAX_BCRYPT_PASSWORD_BYTES = 72

AuthStatus = Literal["success", "invalid_credentials", "locked", "disabled", "invalid_code"]
Role = Literal["owner", "seller"]


@dataclass(frozen=True)
class LoginResult:
    """Outcome of a login attempt; failed attempts remain in the session to commit."""

    status: AuthStatus
    user: User | None = None

    @property
    def succeeded(self) -> bool:
        """Return whether this result contains an authenticated user."""
        return self.status == "success" and self.user is not None


class AuthorizationError(PermissionError):
    """Raised when an account is missing, disabled, or lacks a required role."""


def owner_exists(session: Session) -> bool:
    """Return whether the database already has an owner account."""
    return session.scalar(select(User.id).where(User.role == "owner").limit(1)) is not None


def create_first_owner(
    session: Session,
    *,
    username: str,
    password: str,
    password_confirmation: str,
) -> User:
    """Create the initial owner account when the users table is empty."""
    clean_username = _validate_username(username)
    _validate_password(password, password_confirmation)
    if session.get_bind().dialect.name == "postgresql":
        # Serialize first-owner setup across clients sharing a PostgreSQL database.
        session.execute(select(func.pg_advisory_xact_lock(809775249632414)))
    if session.scalar(select(User.id).limit(1)) is not None:
        raise ValueError("The initial owner account has already been created")

    user = User(
        username=clean_username,
        password_hash=hash_password(password),
        role="owner",
        disabled=False,
    )
    try:
        with session.begin_nested():
            session.add(user)
            session.flush()
    except IntegrityError as error:
        raise ValueError("Username is already in use") from error
    return user


def hash_password(password: str) -> str:
    """Return a salted bcrypt hash for a password accepted by bcrypt."""
    if not isinstance(password, str):
        raise TypeError("Password must be text")
    encoded = password.encode("utf-8")
    if len(encoded) > MAX_BCRYPT_PASSWORD_BYTES:
        raise ValueError("Password cannot exceed 72 UTF-8 bytes")
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    """Check a plaintext password against a bcrypt hash without raising on invalid data."""
    if not isinstance(password, str) or not isinstance(password_hash, str):
        return False
    encoded = password.encode("utf-8")
    if len(encoded) > MAX_BCRYPT_PASSWORD_BYTES:
        return False
    try:
        return bcrypt.checkpw(encoded, password_hash.encode("ascii"))
    except (ValueError, TypeError, UnicodeEncodeError):
        return False


def authenticate(
    session: Session,
    *,
    username: str,
    password: str,
    now: datetime | None = None,
) -> LoginResult:
    """Authenticate an account and record lockout state after invalid passwords.

    Expected failures are returned as statuses instead of raised exceptions so a
    surrounding ``session_scope`` can commit the failed-attempt counter.

    A ``"success"`` for an account where ``requires_second_factor`` is true only
    proves the password: the caller must then call ``verify_second_factor``.
    For such accounts the failed-attempt counter is not reset here, so a known
    password cannot be replayed to get unlimited verification-code guesses.
    """
    clean_username = username.strip() if isinstance(username, str) else ""
    current_time = _utc(now or datetime.now(timezone.utc))
    user = session.scalar(
        select(User)
        .where(User.username == clean_username)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if user is None:
        return LoginResult("invalid_credentials")
    if user.disabled:
        return LoginResult("disabled")

    if user.locked_until is not None:
        locked_until = _utc(user.locked_until)
        if locked_until > current_time:
            return LoginResult("locked")
        # Expired lockouts are cleared before processing this attempt.
        user.failed_attempts = 0
        user.locked_until = None

    if verify_password(password, user.password_hash):
        if not requires_second_factor(user):
            user.failed_attempts = 0
            user.locked_until = None
            user.last_activity_at = current_time
        session.flush()
        return LoginResult("success", user)

    if _record_failed_attempt(user, current_time):
        session.flush()
        return LoginResult("locked")
    session.flush()
    return LoginResult("invalid_credentials")


def _record_failed_attempt(user: User, current_time: datetime) -> bool:
    """Count one failed password or code attempt; return True when it starts a lockout."""
    user.failed_attempts += 1
    if user.failed_attempts >= MAX_FAILED_ATTEMPTS:
        user.failed_attempts = MAX_FAILED_ATTEMPTS
        user.locked_until = current_time + LOCKOUT_DURATION
        return True
    return False


def change_password(
    session: Session,
    *,
    user_id: int,
    current_password: str,
    new_password: str,
    password_confirmation: str,
) -> User:
    """Change an active user's password after verifying the current password."""
    user = session.get(User, user_id)
    if user is None or user.disabled:
        raise AuthorizationError("User account is unavailable")
    if not verify_password(current_password, user.password_hash):
        raise ValueError("Current password is incorrect")
    _validate_password(new_password, password_confirmation)
    user.password_hash = hash_password(new_password)
    user.failed_attempts = 0
    user.locked_until = None
    session.flush()
    return user


def is_owner(user: User) -> bool:
    """Return whether this account is an active owner."""
    return user.role == "owner" and not user.disabled


def is_seller(user: User) -> bool:
    """Return whether this account is an active seller."""
    return user.role == "seller" and not user.disabled


def require_role(session: Session, user_id: int, role: Role) -> User:
    """Return the active user when its role matches, otherwise deny the action."""
    if role not in ("owner", "seller"):
        raise ValueError(f"Unsupported role: {role}")
    user = session.get(User, user_id)
    if user is None or user.disabled or user.role != role:
        raise AuthorizationError(f"An active {role} account is required")
    return user


def require_owner(session: Session, user_id: int) -> User:
    """Return an active owner or raise an authorization error."""
    return require_role(session, user_id, "owner")


def require_seller(session: Session, user_id: int) -> User:
    """Return an active seller or raise an authorization error."""
    return require_role(session, user_id, "seller")


def create_seller(
    session: Session,
    owner_user_id: int,
    *,
    username: str,
    password: str,
    password_confirmation: str,
) -> User:
    """Create an enabled seller account; only an active owner may do so."""
    return create_user(
        session,
        owner_user_id,
        username=username,
        password=password,
        password_confirmation=password_confirmation,
        role="seller",
    )


def create_user(
    session: Session,
    owner_user_id: int,
    *,
    username: str,
    password: str,
    password_confirmation: str,
    role: Role,
) -> User:
    """Create an enabled owner or seller account; only an active owner may do so."""
    require_owner(session, owner_user_id)
    if role not in ("owner", "seller"):
        raise ValueError(f"Unsupported role: {role}")
    clean_username = _validate_username(username)
    _validate_password(password, password_confirmation)
    user = User(
        username=clean_username,
        password_hash=hash_password(password),
        role=role,
        disabled=False,
    )
    try:
        with session.begin_nested():
            session.add(user)
            session.flush()
    except IntegrityError as error:
        raise ValueError("Username is already in use") from error
    return user


def list_users(session: Session, owner_user_id: int) -> list[User]:
    """Return user accounts in stable role/name order for owner settings."""
    require_owner(session, owner_user_id)
    return list(session.scalars(select(User).order_by(User.role, User.username, User.id)))


def set_user_disabled(
    session: Session,
    owner_user_id: int,
    target_user_id: int,
    disabled: bool,
) -> User:
    """Enable or disable an account, protecting the final active owner."""
    require_owner(session, owner_user_id)
    if not isinstance(disabled, bool):
        raise ValueError("Disabled state must be true or false")
    target = session.get(User, target_user_id)
    if target is None:
        raise ValueError("User not found")
    if disabled and target.role == "owner" and not target.disabled:
        if session.get_bind().dialect.name == "postgresql":
            # Prevent two owner sessions disabling the final active owners at once.
            session.execute(select(func.pg_advisory_xact_lock(809775249632415)))
            session.refresh(target)
            if target.disabled:
                return target
        active_owners = session.scalar(
            select(func.count()).select_from(User).where(
                User.role == "owner",
                User.disabled.is_(False),
            )
        ) or 0
        if active_owners <= 1:
            raise ValueError("Cannot disable the last active owner")
    target.disabled = disabled
    target.failed_attempts = 0
    target.locked_until = None
    session.flush()
    return target


def enable_user(session: Session, owner_user_id: int, target_user_id: int) -> User:
    """Enable a seller or owner account through the owner-only service guard."""
    return set_user_disabled(session, owner_user_id, target_user_id, False)


def disable_user(session: Session, owner_user_id: int, target_user_id: int) -> User:
    """Disable an account through the owner-only service guard."""
    return set_user_disabled(session, owner_user_id, target_user_id, True)


def reset_password(
    session: Session,
    owner_user_id: int,
    target_user_id: int,
    *,
    new_password: str,
    password_confirmation: str,
) -> User:
    """Reset an account password without changing its role or disabled state."""
    require_owner(session, owner_user_id)
    target = session.get(User, target_user_id)
    if target is None:
        raise ValueError("User not found")
    _validate_password(new_password, password_confirmation)
    target.password_hash = hash_password(new_password)
    target.failed_attempts = 0
    target.locked_until = None
    session.flush()
    return target


def requires_second_factor(user: User) -> bool:
    """Return whether this active account must pass a TOTP code after its password."""
    return bool(user.totp_enabled and user.totp_secret and not user.disabled)


def start_two_factor_setup(session: Session, user_id: int) -> str:
    """Store a new pending TOTP secret for an active user and return it.

    Two-step verification stays off (also for an account that had it enabled)
    until ``confirm_two_factor_setup`` accepts a code from the new secret.
    """
    user = _active_user(session, user_id)
    secret = two_factor.generate_secret()
    user.totp_secret = secret
    user.totp_enabled = False
    user.totp_last_counter = None
    session.flush()
    return secret


def confirm_two_factor_setup(
    session: Session,
    user_id: int,
    code: str,
    *,
    now: datetime | None = None,
) -> None:
    """Enable two-step verification once a code from the pending secret is correct."""
    user = _active_user(session, user_id)
    if not user.totp_secret:
        raise ValueError("Two-step verification setup has not been started")
    if user.totp_enabled:
        raise ValueError("Two-step verification is already enabled")
    current_time = _utc(now or datetime.now(timezone.utc))
    counter = two_factor.matching_counter(user.totp_secret, code, current_time)
    if counter is None:
        raise ValueError("Invalid verification code")
    user.totp_enabled = True
    user.totp_last_counter = counter
    session.flush()


def disable_two_factor(session: Session, user_id: int, *, password: str) -> None:
    """Turn off the user's own two-step verification after re-checking the password."""
    user = _active_user(session, user_id)
    if not verify_password(password, user.password_hash):
        raise ValueError("Current password is incorrect")
    _clear_two_factor(user)
    session.flush()


def reset_two_factor(session: Session, owner_user_id: int, target_user_id: int) -> User:
    """Clear another account's two-step verification (for a lost phone); owner only."""
    require_owner(session, owner_user_id)
    target = session.get(User, target_user_id)
    if target is None:
        raise ValueError("User not found")
    _clear_two_factor(target)
    session.flush()
    return target


def verify_second_factor(
    session: Session,
    user_id: int,
    code: str,
    *,
    now: datetime | None = None,
) -> LoginResult:
    """Check the login verification code that follows a successful password.

    A code is accepted once: its time step must be newer than the last accepted
    one. Wrong or replayed codes share the password lockout counter.
    """
    current_time = _utc(now or datetime.now(timezone.utc))
    user = session.scalar(
        select(User)
        .where(User.id == user_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if user is None:
        return LoginResult("invalid_credentials")
    if user.disabled:
        return LoginResult("disabled")
    if user.locked_until is not None:
        if _utc(user.locked_until) > current_time:
            return LoginResult("locked")
        user.failed_attempts = 0
        user.locked_until = None
    if not requires_second_factor(user):
        session.flush()
        return LoginResult("invalid_code")

    counter = two_factor.matching_counter(user.totp_secret or "", code, current_time)
    last_counter = user.totp_last_counter
    if counter is not None and (last_counter is None or counter > last_counter):
        user.totp_last_counter = counter
        user.failed_attempts = 0
        user.locked_until = None
        user.last_activity_at = current_time
        session.flush()
        return LoginResult("success", user)

    locked = _record_failed_attempt(user, current_time)
    session.flush()
    return LoginResult("locked" if locked else "invalid_code")


def _active_user(session: Session, user_id: int) -> User:
    """Return an enabled account or raise an authorization error."""
    user = session.get(User, user_id)
    if user is None or user.disabled:
        raise AuthorizationError("User account is unavailable")
    return user


def _clear_two_factor(user: User) -> None:
    """Remove the TOTP secret, enabled flag, and replay counter."""
    user.totp_secret = None
    user.totp_enabled = False
    user.totp_last_counter = None


def _validate_username(username: str) -> str:
    """Trim and validate a login name against the model's maximum length."""
    if not isinstance(username, str):
        raise ValueError("Username is required")
    clean_username = username.strip()
    if not clean_username:
        raise ValueError("Username cannot be empty")
    if len(clean_username) > 80:
        raise ValueError("Username cannot exceed 80 characters")
    return clean_username


def _validate_password(password: str, confirmation: str) -> None:
    """Validate minimum length, confirmation, and bcrypt's byte limit."""
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError("Password must contain at least 6 characters")
    if password != confirmation:
        raise ValueError("Password confirmation does not match")
    if len(password.encode("utf-8")) > MAX_BCRYPT_PASSWORD_BYTES:
        raise ValueError("Password cannot exceed 72 UTF-8 bytes")


def _utc(value: datetime) -> datetime:
    """Normalize SQLAlchemy's possibly-naive SQLite datetime to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
