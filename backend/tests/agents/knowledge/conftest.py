"""Shared fakes for the Knowledge Agent tests."""
from __future__ import annotations


class FakeSession:
    """A session that does nothing.

    Retrieval functions are injected as fakes in these tests, so the session
    is never actually used — it only has to satisfy the async
    context-manager protocol that `nodes.py` opens it with.
    """

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False


def fake_session_factory():
    """A callable shaped like `async_sessionmaker`: a new session per call.

    The per-call part matters. The retrieval nodes open one session each
    precisely because the two arms run concurrently, and a single
    `AsyncSession` cannot serve two coroutines at once.
    """
    return lambda: FakeSession()
