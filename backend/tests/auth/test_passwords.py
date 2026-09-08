"""Password hashing, and the timing property that hides account existence."""
from __future__ import annotations

import time

import pytest

from auth import passwords


class TestHashing:
    def test_hash_verifies_against_its_own_password(self):
        stored = passwords.hash_password("correct-horse-battery-staple")
        assert passwords.verify_password(stored, "correct-horse-battery-staple")

    def test_a_wrong_password_does_not_verify(self):
        stored = passwords.hash_password("correct-horse-battery-staple")
        assert not passwords.verify_password(stored, "incorrect-horse")

    def test_the_same_password_hashes_differently_each_time(self):
        """Per-hash salt. Without it, identical passwords are visibly
        identical in the table, and one cracked hash cracks every account
        that shares it."""
        first = passwords.hash_password("correct-horse-battery-staple")
        second = passwords.hash_password("correct-horse-battery-staple")
        assert first != second

    def test_the_plaintext_never_appears_in_the_hash(self):
        stored = passwords.hash_password("correct-horse-battery-staple")
        assert "correct-horse" not in stored

    def test_it_is_argon2id(self):
        assert passwords.hash_password("correct-horse-battery-staple").startswith(
            "$argon2id$"
        )

    def test_garbage_in_the_hash_column_does_not_raise(self):
        """A corrupted or truncated hash must be a failed login, not a 500 --
        an exception here would take the endpoint down rather than reject
        one credential."""
        assert not passwords.verify_password("not-a-hash", "anything")


class TestPolicy:
    def test_short_passwords_are_refused(self):
        with pytest.raises(passwords.WeakPasswordError):
            passwords.hash_password("short")

    def test_absurdly_long_passwords_are_refused(self):
        """Hashing is deliberately expensive, so an unbounded input is an
        unbounded amount of work an unauthenticated caller can ask for."""
        with pytest.raises(passwords.WeakPasswordError):
            passwords.hash_password("a" * (passwords.MAX_PASSWORD_LENGTH + 1))


class TestAccountEnumeration:
    def test_a_missing_hash_still_returns_false(self):
        assert not passwords.verify_or_dummy(None, "anything")

    def test_absence_costs_about_what_rejection_costs(self):
        """The property that stops the login endpoint being an account
        oracle. If "no such user" returned quickly and "wrong password"
        slowly, an attacker learns which addresses have accounts -- most of
        the work of a targeted attack -- without guessing a single password.

        The bound is deliberately loose: this asserts the dummy verification
        actually happens, not a precise constant, because a tight bound would
        fail on a loaded CI machine for reasons unrelated to the code.
        """
        stored = passwords.hash_password("correct-horse-battery-staple")

        def elapsed(hash_value):
            start = time.perf_counter()
            passwords.verify_or_dummy(hash_value, "some-wrong-password")
            return time.perf_counter() - start

        # Warm: the first Argon2 call in a process pays one-off setup.
        elapsed(stored)

        absent = min(elapsed(None) for _ in range(3))
        rejected = min(elapsed(stored) for _ in range(3))

        assert absent > rejected / 3, (
            f"Absent-account path took {absent * 1000:.1f} ms against "
            f"{rejected * 1000:.1f} ms for a wrong password; that gap is an "
            f"account-enumeration oracle."
        )


class TestRehashing:
    def test_a_current_hash_does_not_need_rehashing(self):
        assert not passwords.needs_rehash(
            passwords.hash_password("correct-horse-battery-staple")
        )

    def test_an_unparseable_hash_needs_replacing(self):
        assert passwords.needs_rehash("$argon2id$nonsense")
