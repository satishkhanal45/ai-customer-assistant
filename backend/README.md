# AI Customer Assistant — Backend

Setup, commands and architecture live in the [root README](../README.md).
This file covers the two things specific to working inside `backend/`.

## Running the tests

```bash
make test                       # from the repository root — preferred
```

Or directly:

```bash
cd backend
env -u PYTHONPATH ./.venv/bin/python -m pytest -q
```

**Not `uv run pytest`, and not a bare `pytest`.** An activated conda
environment — or a stale `VIRTUAL_ENV` from another checkout — puts a
different interpreter first on `PATH`, and the suite fails with about two
dozen collection errors that look like missing dependencies and are not.
Invoking `backend/.venv`'s Python by absolute path is what avoids it, and is
what `make test` does.

`env -u PYTHONPATH` matters for the same reason: an inherited `PYTHONPATH`
can shadow the package under test.

## Crawling (Playwright)

Site crawls render pages with Playwright, which needs a Chromium binary the
Python package does not install:

```bash
cd backend
uv run playwright install chromium
```

Add `--with-deps` to also install the OS libraries Chromium needs; that
requires root on Linux. Without the browser, four crawler tests error and one
fails — everything else passes.
