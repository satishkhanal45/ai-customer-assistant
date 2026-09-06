"""Process configuration: loading `backend/.env` at an entry point.

## Why this exists

`agents/ticket_agent/store.py` used to call `load_dotenv()` at module scope.
That is a library module, and `load_dotenv()` does not return values — it
*mutates `os.environ` for the whole process*. So merely importing a
ticket-persistence module injected `GROQ_API_KEY`, `SMTP_PASSWORD` and every
other secret in `backend/.env` into global state, as a side effect nobody
asked for.

Three concrete consequences, all observed:

1. **Behaviour depended on import order.** The Knowledge provider resolver
   picks a backend by asking which API keys exist, so its answer changed
   depending on whether an unrelated module had been imported first.
   `test_providers.py` passed alone and failed when run alongside the ticket
   tests — same code, same machine.
2. **Behaviour depended on the working directory.** Bare `load_dotenv()`
   searches upward from the CWD, so config silently changed based on where
   the process was launched from.
3. **Secrets entered `os.environ` unintentionally**, putting them in scope
   for anything that dumps the environment on error.

## The contract

Loading configuration is an *entry point's* job, done once, explicitly.
Library modules read `os.environ` (or better, take injected settings); they
never install it. The entry points are:

  - `main.py`                  — the FastAPI app
  - `scripts/run_worker.py`    — the ingestion worker
  - `scripts/crawl_and_ingest.py`
  - `scripts/test_graph_queries.py`

`override=False` is the default and matters: in Docker the real environment
comes from compose's `env_file`/`environment`, and `backend/.env` is excluded
from the image by `.dockerignore` anyway. A real environment variable must
always beat a dotenv file, so containers are never silently reconfigured by a
stray file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

# backend/.env — anchored to this file, never to the current working directory.
DEFAULT_ENV_PATH: Path = Path(__file__).resolve().parents[2] / ".env"

_loaded: bool = False


def _parse_without_dotenv(env_path: Path, *, override: bool) -> None:
    """Minimal `KEY=value` parser for when python-dotenv is not installed.

    Preserves the fallback `scripts/run_worker.py` already carried, so the
    worker keeps starting in a stripped-down environment. Deliberately simple:
    no interpolation, no multi-line values, no `export ` prefix handling — if
    the .env grows to need those, install python-dotenv.
    """
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if override:
            os.environ[key] = value
        else:
            os.environ.setdefault(key, value)


def load_env(
    env_path: Optional[Path] = None,
    *,
    override: bool = False,
    force: bool = False,
) -> bool:
    """Load `backend/.env` into `os.environ`. Returns whether a file was read.

    Call this once, early, from a process entry point — never from a library
    module at import time (see the module docstring).

    Args:
        env_path: file to read; defaults to `backend/.env` resolved relative
            to this file, so it does not depend on the working directory.
        override: whether dotenv values beat existing environment variables.
            Defaults to False so container/CI configuration always wins.
        force: reload even if this process already loaded one. Only useful in
            tests; normal callers leave it False so repeat calls are cheap.

    Missing file is not an error: in Docker there is no `.env` in the image
    and the environment is supplied by compose.
    """
    global _loaded
    if _loaded and not force:
        return False

    resolved = Path(env_path) if env_path is not None else DEFAULT_ENV_PATH
    if not resolved.is_file():
        _loaded = True
        return False

    try:
        from dotenv import load_dotenv
    except ImportError:
        _parse_without_dotenv(resolved, override=override)
    else:
        load_dotenv(resolved, override=override)

    _loaded = True
    return True
