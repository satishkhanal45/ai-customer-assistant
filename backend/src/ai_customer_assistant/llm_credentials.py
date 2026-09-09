"""Provider API keys held in the database, encrypted at rest.

## Why this exists, and what it costs

Until now a provider key came from the environment and nowhere else:
`GROQ_API_KEY` in `backend/.env`, read at provider construction. That is
the safest possible arrangement -- the key never touches the database, and
an admin account cannot reach it. Letting an administrator paste a key into
a web form trades some of that away, deliberately, for the ability to
rotate a key without a redeploy.

Three things keep the trade defensible:

* **Encrypted at rest.** AES-GCM under a key derived from `AUTH_SECRET` by
  HKDF. A dump of the database alone does not yield a usable provider key;
  an attacker needs the row *and* the application secret.
* **Write-only over HTTP.** No endpoint ever returns a key. The API exposes
  the last four characters and nothing else, which is enough to answer "is
  this the key I think it is?" and not enough to use.
* **Environment still wins where it is set to.** The resolution order is
  database, then environment, so a deployment that prefers to keep keys out
  of the database simply never saves one and behaves exactly as before.

The tie to `AUTH_SECRET` has one consequence worth stating plainly:
**rotating `AUTH_SECRET` makes every stored provider key undecryptable.**
That is the correct behaviour -- the alternative is a second secret nobody
remembers to rotate -- but it means rotating it requires re-entering the
provider keys afterwards.

## Freshness

Keys are cached in the process because they are read on every provider
construction and change perhaps twice a year. `refresh()` reloads the cache
and is called at startup and after any write, so a save takes effect
without a restart. The cache is per-process; with several instances the
others pick the change up on their next refresh or restart, which for a
credential that changes twice a year is not worth a broadcast mechanism.
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from typing import Final

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger(__name__)

# The providers the application can actually talk to. A provider not in
# this table cannot be configured, which is what stops a typo'd name
# becoming a row nothing will ever read.
#
# `env_var` is the fallback this provider has always used, kept so a
# deployment that never saves a key behaves exactly as it did before.
@dataclass(frozen=True)
class Provider:
    name: str
    label: str
    env_var: str
    docs_url: str


PROVIDERS: Final[tuple[Provider, ...]] = (
    Provider("groq", "Groq", "GROQ_API_KEY", "https://console.groq.com/keys"),
    Provider("openai", "OpenAI", "OPENAI_API_KEY", "https://platform.openai.com/api-keys"),
    Provider("gemini", "Google Gemini", "GEMINI_API_KEY", "https://aistudio.google.com/app/apikey"),
    Provider("anthropic", "Anthropic", "ANTHROPIC_API_KEY", "https://console.anthropic.com/settings/keys"),
)

PROVIDERS_BY_NAME: Final[dict[str, Provider]] = {p.name: p for p in PROVIDERS}

# Groq is the default provider: it is what the deployment has been running
# on, what the timeout ladder was measured against, and the only one with a
# free tier this project is known to work within.
DEFAULT_PROVIDER: Final[str] = "groq"

_HKDF_INFO: Final[bytes] = b"ai-customer-assistant/llm-credential/v1"
_NONCE_BYTES: Final[int] = 12

# provider name -> plaintext key. Populated by refresh().
_cache: dict[str, str] = {}
_default: str = DEFAULT_PROVIDER


class CredentialError(RuntimeError):
    """The stored key could not be decrypted."""


def _encryption_key() -> bytes:
    """A 32-byte AES key derived from `AUTH_SECRET`.

    Derived rather than used directly so the signing secret and the
    encryption key are not the same bytes: a bug that leaks one does not
    hand over the other.
    """
    from auth.tokens import auth_secret

    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_HKDF_INFO,
    ).derive(auth_secret().encode("utf-8"))


def encrypt(plaintext: str) -> str:
    """Encrypt a key for storage. Returns base64 of `nonce || ciphertext`."""
    nonce = os.urandom(_NONCE_BYTES)
    sealed = AESGCM(_encryption_key()).encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + sealed).decode("ascii")


def decrypt(stored: str) -> str:
    """Reverse `encrypt`. Raises `CredentialError` if it cannot."""
    try:
        raw = base64.b64decode(stored)
        nonce, sealed = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
        return AESGCM(_encryption_key()).decrypt(nonce, sealed, None).decode("utf-8")
    except Exception as exc:  # InvalidTag, binascii, UnicodeDecodeError
        raise CredentialError(
            "Stored provider key could not be decrypted. This normally means "
            "AUTH_SECRET has changed since the key was saved; re-enter it."
        ) from exc


def last4(plaintext: str) -> str:
    """The tail of a key, for recognition. Never more than four characters."""
    return plaintext[-4:] if len(plaintext) >= 4 else "*" * len(plaintext)


def api_key_for(provider: str) -> str | None:
    """The key to use for `provider`: saved first, environment second.

    Called from provider construction, which is synchronous and happens
    during startup -- hence the cache rather than a query here.
    """
    saved = _cache.get(provider)
    if saved:
        return saved
    spec = PROVIDERS_BY_NAME.get(provider)
    return os.environ.get(spec.env_var) if spec else None


def default_provider() -> str:
    """The provider new work should use."""
    return _default


def configured_providers() -> list[str]:
    """Every provider with a usable key, saved or in the environment."""
    return [p.name for p in PROVIDERS if api_key_for(p.name)]


async def refresh(session) -> None:
    """Reload the process cache from the database.

    Called at startup and after every write. A row that will not decrypt is
    logged and skipped rather than raised: one unreadable credential must
    not stop the application from starting with the others.
    """
    from sqlalchemy import select

    from db.models import LlmCredential

    global _default
    rows = (await session.execute(select(LlmCredential))).scalars().all()

    fresh: dict[str, str] = {}
    for row in rows:
        if row.encrypted_key is None:
            continue
        try:
            fresh[row.provider] = decrypt(row.encrypted_key)
        except CredentialError:
            logger.warning(
                "Provider key for %r could not be decrypted; ignoring it.",
                row.provider,
            )

    _cache.clear()
    _cache.update(fresh)

    chosen = next((row.provider for row in rows if row.is_default), None)
    _default = chosen if chosen in PROVIDERS_BY_NAME else DEFAULT_PROVIDER


def _reset_for_tests() -> None:
    _cache.clear()
    global _default
    _default = DEFAULT_PROVIDER
