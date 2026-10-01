"""TOTP primitives and two-step verification account workflows."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import User
from app.db.session import session_scope
from app.services import auth, two_factor

RFC_SECRET = base64.b32encode(b"12345678901234567890").decode("ascii")
START = datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)
PASSWORD = "secret1"


@pytest.mark.parametrize(
    ("unix_time", "code"),
    [
        (59, "287082"),
        (1111111109, "081804"),
        (1111111111, "050471"),
        (1234567890, "005924"),
        (2000000000, "279037"),
        (20000000000, "353130"),
    ],
)
def test_rfc6238_sha1_vectors(unix_time: int, code: str) -> None:
    """Codes match the low six digits of the published RFC 6238 SHA1 values."""
    at = datetime.fromtimestamp(unix_time, tz=timezone.utc)
    assert two_factor.totp_code(RFC_SECRET, at) == code
    assert two_factor.matching_counter(RFC_SECRET, code, at) == unix_time // 30


def test_naive_datetimes_are_utc() -> None:
    """A naive datetime is interpreted as UTC, not local time."""
    aware = datetime(2026, 10, 1, 12, 0, 15, tzinfo=timezone.utc)
    naive = aware.replace(tzinfo=None)
    assert two_factor.counter_for(naive) == two_factor.counter_for(aware)
    assert two_factor.counter_for(aware) == int(aware.timestamp()) // 30


def test_generate_secret_is_random_unpadded_base32() -> None:
    """Secrets carry 160 bits as 32 base32 characters without padding."""
    first = two_factor.generate_secret()
    second = two_factor.generate_secret()
    assert len(first) == 32 and "=" not in first
    assert len(base64.b32decode(first)) == 20
    assert first != second


def test_provisioning_uri_and_formatted_secret() -> None:
    """The URI follows the Key Uri Format and secrets group in fours."""
    uri = two_factor.provisioning_uri(RFC_SECRET, "أكرم بائع")
    parts = urlsplit(uri)
    assert parts.scheme == "otpauth" and parts.netloc == "totp"
    assert " " not in uri
    assert unquote(parts.path) == "/AkremMobile:أكرم بائع"
    query = parse_qs(parts.query)
    assert query == {
        "secret": [RFC_SECRET],
        "issuer": ["AkremMobile"],
        "algorithm": ["SHA1"],
        "digits": ["6"],
        "period": ["30"],
    }
    assert two_factor.format_secret("ABCDEFGHIJ") == "ABCD EFGH IJ"
    formatted = two_factor.format_secret(RFC_SECRET)
    assert formatted.replace(" ", "") == RFC_SECRET
    assert all(len(group) == 4 for group in formatted.split(" "))


def test_matching_counter_window_spaces_and_bad_input() -> None:
    """Codes from one step either side match; others and malformed input do not."""
    counter = two_factor.counter_for(START)
    previous = two_factor.totp_code(RFC_SECRET, START - timedelta(seconds=30))
    following = two_factor.totp_code(RFC_SECRET, START + timedelta(seconds=30))
    too_old = two_factor.totp_code(RFC_SECRET, START - timedelta(seconds=60))
    current = two_factor.totp_code(RFC_SECRET, START)
    assert two_factor.matching_counter(RFC_SECRET, previous, START) == counter - 1
    assert two_factor.matching_counter(RFC_SECRET, following, START) == counter + 1
    assert two_factor.matching_counter(RFC_SECRET, too_old, START) is None
    assert two_factor.matching_counter(RFC_SECRET, too_old, START, window=2) == counter - 2
    assert two_factor.matching_counter(RFC_SECRET, f"{current[:3]} {current[3:]}", START) == counter
    for bad in ("", "12345", "1234567", "abcdef", "١٢٣٤٥٦"):
        assert two_factor.matching_counter(RFC_SECRET, bad, START) is None


def _owner(session: Session) -> User:
    """Create the first owner account."""
    return auth.create_first_owner(
        session, username="owner", password=PASSWORD, password_confirmation=PASSWORD
    )


def _enable(session: Session, user_id: int, at: datetime = START) -> str:
    """Start and confirm setup for a user, returning the secret."""
    secret = auth.start_two_factor_setup(session, user_id)
    auth.confirm_two_factor_setup(
        session, user_id, two_factor.totp_code(secret, at), now=at
    )
    return secret


def test_create_user_roles_and_owner_only(memory_engine: Engine) -> None:
    """Owners create owner or seller accounts; sellers cannot create accounts."""
    with session_scope(memory_engine) as session:
        owner = _owner(session)
        second_owner = auth.create_user(
            session, owner.id, username=" boss2 ", password=PASSWORD,
            password_confirmation=PASSWORD, role="owner",
        )
        seller = auth.create_seller(
            session, owner.id, username="seller", password=PASSWORD,
            password_confirmation=PASSWORD,
        )
        assert (second_owner.username, second_owner.role) == ("boss2", "owner")
        assert seller.role == "seller"
        with pytest.raises(ValueError, match="already in use"):
            auth.create_user(
                session, owner.id, username="seller", password=PASSWORD,
                password_confirmation=PASSWORD, role="owner",
            )
        with pytest.raises(ValueError, match="Unsupported role"):
            auth.create_user(
                session, owner.id, username="x1", password=PASSWORD,
                password_confirmation=PASSWORD, role="admin",  # type: ignore[arg-type]
            )
        with pytest.raises(ValueError, match="does not match"):
            auth.create_user(
                session, owner.id, username="x2", password=PASSWORD,
                password_confirmation="other12", role="seller",
            )
        with pytest.raises(auth.AuthorizationError):
            auth.create_user(
                session, seller.id, username="x3", password=PASSWORD,
                password_confirmation=PASSWORD, role="seller",
            )


def test_setup_is_pending_until_confirmed(memory_engine: Engine) -> None:
    """A started setup stores a secret but enables nothing until a valid code."""
    with session_scope(memory_engine) as session:
        owner = _owner(session)
        assert not auth.requires_second_factor(owner)
        with pytest.raises(ValueError, match="not been started"):
            auth.confirm_two_factor_setup(session, owner.id, "123456", now=START)

        secret = auth.start_two_factor_setup(session, owner.id)
        assert owner.totp_secret == secret
        assert owner.totp_enabled is False and owner.totp_last_counter is None
        assert not auth.requires_second_factor(owner)

        wrong = two_factor.totp_code(secret, START - timedelta(minutes=5))
        with pytest.raises(ValueError, match="Invalid verification code"):
            auth.confirm_two_factor_setup(session, owner.id, wrong, now=START)
        assert owner.totp_enabled is False

        auth.confirm_two_factor_setup(
            session, owner.id, two_factor.totp_code(secret, START), now=START
        )
        assert owner.totp_enabled is True
        assert owner.totp_last_counter == two_factor.counter_for(START)
        assert auth.requires_second_factor(owner)
        with pytest.raises(ValueError, match="already enabled"):
            auth.confirm_two_factor_setup(
                session, owner.id, two_factor.totp_code(secret, START), now=START
            )

        # Restarting setup replaces the secret and turns verification off until confirmed.
        new_secret = auth.start_two_factor_setup(session, owner.id)
        assert new_secret != secret
        assert not auth.requires_second_factor(owner)
        assert owner.totp_last_counter is None


def test_disabled_account_cannot_start_setup_or_need_code(memory_engine: Engine) -> None:
    """Setup is refused for disabled accounts, which never require a code."""
    with session_scope(memory_engine) as session:
        owner = _owner(session)
        seller = auth.create_seller(
            session, owner.id, username="seller", password=PASSWORD,
            password_confirmation=PASSWORD,
        )
        _enable(session, seller.id)
        auth.disable_user(session, owner.id, seller.id)
        assert not auth.requires_second_factor(seller)
        with pytest.raises(auth.AuthorizationError):
            auth.start_two_factor_setup(session, seller.id)
        assert auth.verify_second_factor(session, seller.id, "000000", now=START).status == "disabled"


def test_login_code_success_and_replay_rejected(memory_engine: Engine) -> None:
    """A newer code logs in; the same or an older time step is refused."""
    with session_scope(memory_engine) as session:
        owner = _owner(session)
        secret = _enable(session, owner.id)
        owner_id = owner.id

    later = START + timedelta(minutes=2)
    with session_scope(memory_engine) as session:
        password_step = auth.authenticate(session, username="owner", password=PASSWORD, now=later)
        assert password_step.succeeded
        assert auth.requires_second_factor(password_step.user)

        # The code used during setup cannot be replayed at login.
        setup_code = two_factor.totp_code(secret, START)
        assert auth.verify_second_factor(session, owner_id, setup_code, now=START).status == (
            "invalid_code"
        )
        code = two_factor.totp_code(secret, later)
        result = auth.verify_second_factor(session, owner_id, code, now=later)
        assert result.succeeded and result.user is not None
        assert result.user.totp_last_counter == two_factor.counter_for(later)
        assert result.user.failed_attempts == 0
        assert result.user.last_activity_at == later

        replay = auth.verify_second_factor(session, owner_id, code, now=later)
        assert replay.status == "invalid_code" and replay.user is None
        previous = two_factor.totp_code(secret, later - timedelta(seconds=30))
        assert auth.verify_second_factor(session, owner_id, previous, now=later).status == (
            "invalid_code"
        )
        following = two_factor.totp_code(secret, later + timedelta(seconds=30))
        assert auth.verify_second_factor(session, owner_id, following, now=later).succeeded


def test_five_bad_codes_lock_the_account(memory_engine: Engine) -> None:
    """Bad codes share the password lockout: five failures lock for five minutes."""
    with session_scope(memory_engine) as session:
        owner = _owner(session)
        secret = _enable(session, owner.id)
        owner_id = owner.id
    later = START + timedelta(minutes=1)
    bad = two_factor.totp_code(secret, later - timedelta(minutes=10))

    for attempt in range(1, auth.MAX_FAILED_ATTEMPTS + 1):
        with session_scope(memory_engine) as session:
            status = auth.verify_second_factor(session, owner_id, bad, now=later).status
            expected = "locked" if attempt == auth.MAX_FAILED_ATTEMPTS else "invalid_code"
            assert status == expected

    good = two_factor.totp_code(secret, later)
    with session_scope(memory_engine) as session:
        assert auth.verify_second_factor(session, owner_id, good, now=later).status == "locked"
        assert auth.authenticate(
            session, username="owner", password=PASSWORD, now=later
        ).status == "locked"
        user = session.get(User, owner_id)
        assert user is not None and user.locked_until is not None

    unlocked = later + auth.LOCKOUT_DURATION + timedelta(seconds=1)
    with session_scope(memory_engine) as session:
        code = two_factor.totp_code(secret, unlocked)
        assert auth.verify_second_factor(session, owner_id, code, now=unlocked).succeeded


def test_password_success_does_not_reset_code_failures(memory_engine: Engine) -> None:
    """Re-entering the password cannot be used to get unlimited code guesses."""
    with session_scope(memory_engine) as session:
        owner = _owner(session)
        secret = _enable(session, owner.id)
        owner_id = owner.id
    later = START + timedelta(minutes=1)
    bad = two_factor.totp_code(secret, later - timedelta(minutes=10))
    with session_scope(memory_engine) as session:
        for _ in range(auth.MAX_FAILED_ATTEMPTS - 1):
            auth.verify_second_factor(session, owner_id, bad, now=later)
        assert auth.authenticate(session, username="owner", password=PASSWORD, now=later).succeeded
        assert session.get(User, owner_id).failed_attempts == auth.MAX_FAILED_ATTEMPTS - 1
        assert auth.verify_second_factor(session, owner_id, bad, now=later).status == "locked"


def test_disable_requires_current_password(memory_engine: Engine) -> None:
    """Users turn their own verification off only with the right password."""
    with session_scope(memory_engine) as session:
        owner = _owner(session)
        _enable(session, owner.id)
        with pytest.raises(ValueError, match="Current password is incorrect"):
            auth.disable_two_factor(session, owner.id, password="wrong-pass")
        assert auth.requires_second_factor(owner)
        auth.disable_two_factor(session, owner.id, password=PASSWORD)
        assert owner.totp_secret is None
        assert owner.totp_enabled is False and owner.totp_last_counter is None
        assert auth.verify_second_factor(session, owner.id, "123456", now=START).status == (
            "invalid_code"
        )


def test_owner_resets_lost_phone_and_seller_cannot(memory_engine: Engine) -> None:
    """Only an active owner may clear another account's verification."""
    with session_scope(memory_engine) as session:
        owner = _owner(session)
        seller = auth.create_seller(
            session, owner.id, username="seller", password=PASSWORD,
            password_confirmation=PASSWORD,
        )
        other = auth.create_seller(
            session, owner.id, username="other", password=PASSWORD,
            password_confirmation=PASSWORD,
        )
        _enable(session, seller.id)
        _enable(session, other.id)
        with pytest.raises(auth.AuthorizationError):
            auth.reset_two_factor(session, seller.id, other.id)
        assert auth.requires_second_factor(other)

        reset = auth.reset_two_factor(session, owner.id, other.id)
        assert reset is other
        assert not auth.requires_second_factor(other)
        assert other.totp_secret is None and other.totp_last_counter is None
        with pytest.raises(ValueError, match="User not found"):
            auth.reset_two_factor(session, owner.id, 9999)
