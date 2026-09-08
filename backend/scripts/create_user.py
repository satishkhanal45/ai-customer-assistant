#!/usr/bin/env python
"""Create or update an account, from the command line.

There is no self-signup: this is an internal tool, and an open registration
endpoint on it would be a way for anyone who can reach the port to mint
themselves a `member` account and read the entire corpus. Accounts are
created by an admin through `POST /auth/users` -- and this script exists to
create the *first* admin, which that endpoint cannot do because it requires
one to already exist.

    # the first admin
    uv run python scripts/create_user.py admin@example.com --role admin

    # a colleague, afterwards (or use POST /auth/users)
    uv run python scripts/create_user.py sam@example.com

    # reset a forgotten password
    uv run python scripts/create_user.py sam@example.com --update

The password is read from a prompt, never from an argument. A password on the
command line ends up in the shell history, in `ps` output for every other
user on the box, and in any process accounting that is running -- which is
three places it should not be. `--password-stdin` is the non-interactive form
for a provisioning script, and takes it from a pipe rather than from argv.

Run from the repository root; it is an entry point, so it loads
`backend/.env` itself (see config.load_env for why library modules must not).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "ai_customer_assistant"))

from config import load_env  # noqa: E402

load_env()

from auth import passwords, roles  # noqa: E402
from db.engine import dispose_engine, get_session_factory  # noqa: E402
from sqlalchemy import select  # noqa: E402


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        password = sys.stdin.readline().rstrip("\n")
        if not password:
            raise SystemExit("No password on stdin.")
        return password

    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Repeat password: "):
        raise SystemExit("Passwords do not match.")
    return password


async def _upsert(email: str, password: str, role: str, allow_update: bool) -> str:
    from db.models import AppUser

    async with get_session_factory()() as session:
        existing = (
            await session.execute(select(AppUser).where(AppUser.email == email))
        ).scalar_one_or_none()

        if existing is not None and not allow_update:
            raise SystemExit(
                f"{email} already exists (role {existing.role}). "
                f"Pass --update to change its password or role."
            )

        password_hash = passwords.hash_password(password)
        if existing is None:
            session.add(
                AppUser(
                    email=email,
                    password_hash=password_hash,
                    role=role,
                    is_active=True,
                    is_service_account=False,
                )
            )
            action = "Created"
        else:
            existing.password_hash = password_hash
            existing.role = role
            existing.is_active = True
            action = "Updated"

        await session.commit()
    return action


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("email")
    parser.add_argument(
        "--role",
        default=roles.DEFAULT_ROLE,
        choices=list(roles.ALL_ROLES),
        help=f"default: {roles.DEFAULT_ROLE}",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="allow overwriting an existing account's password and role",
    )
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from stdin instead of prompting",
    )
    args = parser.parse_args()

    email = args.email.strip().lower()
    password = _read_password(args.password_stdin)
    try:
        passwords.validate_password(password)
    except passwords.WeakPasswordError as exc:
        raise SystemExit(str(exc)) from exc

    async def run() -> str:
        try:
            return await _upsert(email, password, args.role, args.update)
        finally:
            await dispose_engine()

    action = asyncio.run(run())
    print(f"{action} {email} with role {args.role}.")


if __name__ == "__main__":
    main()
