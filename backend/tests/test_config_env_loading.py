"""Configuration loading is an entry point's job, never an import side effect (P0-2).

`agents/ticket_agent/store.py` used to call `load_dotenv()` at module scope.
Importing a ticket-persistence module therefore injected `GROQ_API_KEY`,
`SMTP_PASSWORD` and every other secret in `backend/.env` into `os.environ` for
the whole process. The visible symptom was that
`test_providers.py::test_config_default_anthropic_without_key_falls_back_to_stub`
passed when run alone and failed when run alongside the ticket tests — the
Knowledge provider resolver picks a backend by asking which API keys exist, so
its answer changed with import order.

The first test below is the important one: it fails if anyone reintroduces a
module-scope `load_dotenv()` anywhere in the package.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import config

SRC_ROOT = Path(config.__file__).resolve().parent
BACKEND_ROOT = SRC_ROOT.parents[1]


class TestNoImportTimeEnvMutation:
    def test_importing_library_modules_injects_nothing_from_dotenv(self):
        """Importing library code must not install any key from backend/.env.

        Run in a subprocess: the parent pytest process has already imported
        half the package, so only a clean interpreter can prove this.

        The assertion is scoped to keys that actually appear in `.env` rather
        than to "no new variables at all" — importing `sentence_transformers`
        legitimately pulls in torch, which sets its own `KMP_*` /
        `TORCHINDUCTOR_*` runtime variables. Those are third-party runtime
        settings, not this project's configuration, and are not the defect.
        """
        env_file = BACKEND_ROOT / ".env"
        if not env_file.is_file():
            pytest.skip("backend/.env not present; nothing to leak")

        dotenv_keys = {
            line.split("=", 1)[0].strip()
            for line in env_file.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#") and "=" in line
        }
        assert dotenv_keys, "backend/.env parsed as empty — the guard would be vacuous"

        probe = (
            "import os;"
            "before=set(os.environ);"
            "import agents.ticket_agent.store;"
            "import agents.supervisor.cli;"
            "import agents.knowledge.providers;"
            "import services.chat_service;"
            "print(sorted(set(os.environ)-before))"
        )
        env = {k: v for k, v in os.environ.items() if k not in dotenv_keys}
        env["PYTHONPATH"] = str(SRC_ROOT)

        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True, env=env, cwd=str(BACKEND_ROOT),
        )
        assert result.returncode == 0, result.stderr

        added = set(eval(result.stdout.strip()))  # noqa: S307 - our own repr
        leaked = sorted(added & dotenv_keys)
        assert not leaked, f"importing library modules injected .env secrets: {leaked}"

    def test_no_module_scope_load_dotenv_remains(self):
        """A grep-style guard: `load_dotenv` may only be called inside
        `config.py`, which is the one module allowed to install environment."""
        offenders = []
        for path in SRC_ROOT.rglob("*.py"):
            if path.name == "config.py" and path.parent == SRC_ROOT:
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("load_dotenv(") or stripped.startswith("dotenv.load_dotenv("):
                    offenders.append(f"{path.relative_to(SRC_ROOT)}:{number}")
        assert not offenders, f"load_dotenv() called outside config.py: {offenders}"


class TestLoadEnv:
    @pytest.fixture(autouse=True)
    def _reset_module_state(self):
        original = config._loaded
        yield
        config._loaded = original

    def test_missing_file_is_not_an_error(self, tmp_path):
        """Docker has no .env in the image — the environment comes from
        compose — so a missing file must be a quiet no-op."""
        assert config.load_env(tmp_path / "nope.env", force=True) is False

    def test_reads_values_from_the_given_file(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("P0_2_PROBE=from_file\n")
        monkeypatch.delenv("P0_2_PROBE", raising=False)

        assert config.load_env(env_file, force=True) is True
        assert os.environ["P0_2_PROBE"] == "from_file"

    def test_real_environment_wins_by_default(self, tmp_path, monkeypatch):
        """`override=False` is what keeps a stray .env from silently
        reconfiguring a container whose values came from compose."""
        env_file = tmp_path / ".env"
        env_file.write_text("P0_2_PROBE=from_file\n")
        monkeypatch.setenv("P0_2_PROBE", "from_environment")

        config.load_env(env_file, force=True)
        assert os.environ["P0_2_PROBE"] == "from_environment"

    def test_override_is_available_when_explicitly_asked_for(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("P0_2_PROBE=from_file\n")
        monkeypatch.setenv("P0_2_PROBE", "from_environment")

        config.load_env(env_file, override=True, force=True)
        assert os.environ["P0_2_PROBE"] == "from_file"

    def test_is_idempotent_without_force(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("P0_2_PROBE=x\n")
        assert config.load_env(env_file, force=True) is True
        assert config.load_env(env_file) is False

    def test_default_path_is_anchored_to_the_repo_not_the_cwd(self):
        """Bare `load_dotenv()` searched upward from the working directory, so
        config changed depending on where the process was launched."""
        assert config.DEFAULT_ENV_PATH == BACKEND_ROOT / ".env"
        assert config.DEFAULT_ENV_PATH.is_absolute()


class TestFallbackParser:
    def test_parses_without_python_dotenv(self, tmp_path, monkeypatch):
        """`scripts/run_worker.py` carried this fallback so the worker starts
        in a stripped-down environment; it moved here rather than being lost."""
        env_file = tmp_path / ".env"
        env_file.write_text(
            "# a comment\n"
            "\n"
            "P0_2_A=one\n"
            "P0_2_B = two \n"
            "not_a_pair\n"
        )
        monkeypatch.delenv("P0_2_A", raising=False)
        monkeypatch.delenv("P0_2_B", raising=False)

        config._parse_without_dotenv(env_file, override=False)

        assert os.environ["P0_2_A"] == "one"
        assert os.environ["P0_2_B"] == "two"

    def test_fallback_respects_override_false(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("P0_2_A=from_file\n")
        monkeypatch.setenv("P0_2_A", "from_environment")

        config._parse_without_dotenv(env_file, override=False)
        assert os.environ["P0_2_A"] == "from_environment"
