"""Argon2id password hashing.

Argon2id is memory-hard: an attacker with a stolen hash cannot trade memory
for parallelism the way they can against a purely iterative function, which
is what makes GPU cracking of bcrypt hashes cheap. The parameters below are
tuned so one verification costs roughly 130 ms on this hardware (measured,
not assumed) — slow enough to matter across a stolen database, invisible to
a person logging in.

Two things here exist purely to avoid leaking whether an account exists.
``verify_or_dummy`` spends the same work when the user is absent as when the
password is wrong, and the caller returns the same response for both. An
endpoint that answers "no such user" quickly and "wrong password" slowly has
told an attacker which half of the credential they got right, which turns a
password-guessing problem into an account-enumeration problem first.

Rehashing: ``argon2-cffi`` can tell us when a stored hash was made with
weaker parameters than the current ones. ``needs_rehash`` surfaces that so
the login path can transparently upgrade a hash while it legitimately holds
the plaintext — the only moment it ever can.
"""

from __future__ import annotations

from typing import Final

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# time_cost=2, 64 MiB, 2 lanes: the argon2-cffi defaults for a server-side
# interactive login, which land in the target range on this hardware.
_HASHER: Final[PasswordHasher] = PasswordHasher(
    time_cost=2,
    memory_cost=64 * 1024,
    parallelism=2,
)

# A password may be long, but not unbounded: hashing is deliberately
# expensive, so an unbounded input is an unbounded amount of work an
# unauthenticated caller can ask for.
MIN_PASSWORD_LENGTH: Final[int] = 12
MAX_PASSWORD_LENGTH: Final[int] = 1024

# Hashed once at import against a value no one can supply, so that verifying
# a non-existent account costs the same as verifying a real one.
_DUMMY_HASH: Final[str] = _HASHER.hash("argon2-timing-equaliser-not-a-password")


class WeakPasswordError(ValueError):
    """The supplied password does not meet the length policy."""


def validate_password(password: str) -> None:
    """Raise ``WeakPasswordError`` unless the password is an acceptable length.

    Length only. Composition rules ("one digit, one symbol") measurably push
    people towards predictable substitutions and shorter secrets, so the
    policy here is a floor on length and nothing else.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise WeakPasswordError(
            f"Password must be at most {MAX_PASSWORD_LENGTH} characters."
        )


def hash_password(password: str) -> str:
    """Validate and hash. The returned string carries its own parameters."""
    validate_password(password)
    return _HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Whether ``password`` matches ``password_hash``. Never raises on mismatch."""
    try:
        return _HASHER.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def verify_or_dummy(password_hash: str | None, password: str) -> bool:
    """Verify, spending the same work when there is no hash to verify against.

    ``password_hash`` is ``None`` for an account that does not exist, and for
    the seeded service account, which has no password because it never logs
    in. Both must cost what a real rejection costs.
    """
    if not password_hash:
        verify_password(_DUMMY_HASH, password)
        return False
    return verify_password(password_hash, password)


def needs_rehash(password_hash: str) -> bool:
    """Whether the stored hash predates the current cost parameters."""
    try:
        return _HASHER.check_needs_rehash(password_hash)
    except InvalidHashError:
        # Unparseable, so it can never verify. Replacing it is the only
        # useful thing that can happen to it.
        return True
