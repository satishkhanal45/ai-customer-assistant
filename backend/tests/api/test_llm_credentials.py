"""Provider API keys: the crypto, the resolver, and the endpoints.

Letting an administrator paste a provider key into a web form trades away
some of the safety of keeping it only in the environment. These tests pin
the three properties that make the trade defensible:

* the key is **encrypted at rest**, and is useless without `AUTH_SECRET`;
* it is **never returned** over HTTP -- the API exposes four characters;
* the environment **still works**, so a deployment that never touches this
  page behaves exactly as it did before the table existed.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import llm_credentials
from auth import roles
from db.models import AppUser, LlmCredential

KEY = "gsk_liveKeyNobodyShouldSee9876"


@pytest.fixture(autouse=True)
def clean_cache():
    """The credential cache is process-global, so a test that saves a key
    would otherwise leak it into the next one."""
    llm_credentials._reset_for_tests()
    yield
    llm_credentials._reset_for_tests()


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for table in (AppUser.__table__, LlmCredential.__table__):
            await conn.run_sync(table.create)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def client(build_app, db, as_role):
    from api.admin import router
    from db.engine import get_session

    app = build_app(router)

    async def _session():
        async with db() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    as_role(app, role=roles.ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


class TestEncryption:
    def test_roundtrip(self):
        assert llm_credentials.decrypt(llm_credentials.encrypt(KEY)) == KEY

    def test_the_same_key_encrypts_differently_each_time(self):
        """A fresh nonce per encryption. Without it, two providers sharing a
        key would be visibly identical in the table."""
        assert llm_credentials.encrypt(KEY) != llm_credentials.encrypt(KEY)

    def test_the_plaintext_does_not_appear_in_the_ciphertext(self):
        assert "9876" not in llm_credentials.encrypt(KEY)[:-8]

    def test_a_different_auth_secret_cannot_decrypt(self, monkeypatch):
        """The property that makes a database dump alone useless -- and the
        reason rotating AUTH_SECRET means re-entering these keys."""
        sealed = llm_credentials.encrypt(KEY)
        monkeypatch.setenv("AUTH_SECRET", "a-completely-different-secret-value-42")
        with pytest.raises(llm_credentials.CredentialError):
            llm_credentials.decrypt(sealed)

    def test_last4_never_exceeds_four_characters(self):
        assert llm_credentials.last4(KEY) == "9876"
        assert llm_credentials.last4("ab") == "**"


class TestResolution:
    def test_falls_back_to_the_environment(self, monkeypatch):
        """A deployment that never opens this page behaves exactly as it did
        when providers read os.environ directly."""
        monkeypatch.setenv("GROQ_API_KEY", "from-the-environment")
        assert llm_credentials.api_key_for("groq") == "from-the-environment"

    async def test_a_saved_key_shadows_the_environment(self, db, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "from-the-environment")
        async with db() as session:
            session.add(LlmCredential(
                provider="groq", encrypted_key=llm_credentials.encrypt(KEY),
                last4="9876", is_default=True))
            await session.commit()
            await llm_credentials.refresh(session)

        assert llm_credentials.api_key_for("groq") == KEY

    async def test_an_undecryptable_row_is_skipped_not_fatal(self, db, caplog):
        """One unreadable credential must not stop the app starting with the
        others -- this is called during startup."""
        async with db() as session:
            session.add(LlmCredential(provider="groq", encrypted_key="not-base64-at-all"))
            session.add(LlmCredential(
                provider="openai", encrypted_key=llm_credentials.encrypt("sk-fine"), last4="fine"))
            await session.commit()
            await llm_credentials.refresh(session)

        assert llm_credentials.api_key_for("openai") == "sk-fine"

    def test_groq_is_the_default_before_anyone_chooses(self):
        assert llm_credentials.default_provider() == "groq"

    def test_an_unknown_provider_has_no_key(self):
        assert llm_credentials.api_key_for("hal9000") is None


class TestListing:
    async def test_lists_every_supported_provider(self, client):
        body = (await client.get("/admin/llm-providers")).json()
        assert [p["name"] for p in body["providers"]] == ["groq", "openai", "gemini", "anthropic"]
        assert body["default"] == "groq"

    async def test_reports_where_a_key_comes_from(self, client, monkeypatch):
        """Without this an administrator cannot answer "I changed the key and
        nothing happened" -- a saved key shadows the environment."""
        monkeypatch.setenv("GEMINI_API_KEY", "env-key")
        await client.put("/admin/llm-providers/openai", json={"api_key": KEY})

        by_name = {p["name"]: p for p in (await client.get("/admin/llm-providers")).json()["providers"]}
        assert by_name["openai"]["source"] == "saved"
        assert by_name["gemini"]["source"] == "environment"
        assert by_name["anthropic"]["source"] is None
        assert by_name["anthropic"]["configured"] is False

    async def test_never_returns_a_key(self, client):
        """The property the whole design rests on."""
        await client.put("/admin/llm-providers/openai", json={"api_key": KEY})
        raw = (await client.get("/admin/llm-providers")).text

        assert KEY not in raw
        assert "liveKeyNobodyShouldSee" not in raw
        assert '"last4": "9876"' in raw.replace('"last4":"9876"', '"last4": "9876"')


class TestSaving:
    async def test_stores_ciphertext_not_the_key(self, client, db):
        await client.put("/admin/llm-providers/openai", json={"api_key": KEY})

        async with db() as session:
            row = await session.get(LlmCredential, "openai")
        assert row.encrypted_key and KEY not in row.encrypted_key
        assert llm_credentials.decrypt(row.encrypted_key) == KEY
        assert row.last4 == "9876"

    async def test_the_response_carries_only_the_tail(self, client):
        body = (await client.put("/admin/llm-providers/openai", json={"api_key": KEY})).json()
        assert body == {"provider": "openai", "last4": "9876", "is_default": False}

    async def test_takes_effect_without_a_restart(self, client):
        """`refresh()` runs on write, so the next provider construction picks
        the new key up rather than waiting for a redeploy."""
        await client.put("/admin/llm-providers/openai", json={"api_key": KEY})
        assert llm_credentials.api_key_for("openai") == KEY

    async def test_an_unknown_provider_is_refused(self, client):
        """A typo must not become a row nothing will ever read."""
        response = await client.put("/admin/llm-providers/hal9000", json={"api_key": KEY})
        assert response.status_code == 404

    async def test_a_too_short_key_is_refused(self, client):
        assert (await client.put("/admin/llm-providers/openai", json={"api_key": "abc"})).status_code == 422


class TestDefaults:
    async def test_choosing_a_default_moves_it(self, client):
        await client.post("/admin/llm-providers/gemini/default")
        body = (await client.get("/admin/llm-providers")).json()

        assert body["default"] == "gemini"
        assert [p["name"] for p in body["providers"] if p["is_default"]] == ["gemini"]

    async def test_only_one_provider_is_ever_default(self, client, db):
        """The database has a partial unique index enforcing this; the
        handler clears the old default first so the write does not violate
        it."""
        for provider in ("gemini", "openai", "anthropic"):
            await client.post(f"/admin/llm-providers/{provider}/default")

        async with db() as session:
            from sqlalchemy import func, select
            count = (await session.execute(
                select(func.count()).select_from(LlmCredential).where(LlmCredential.is_default)
            )).scalar_one()
        assert count == 1

    async def test_saving_with_make_default_does_both(self, client):
        body = (await client.put(
            "/admin/llm-providers/openai", json={"api_key": KEY, "make_default": True}
        )).json()
        assert body["is_default"] is True
        assert (await client.get("/admin/llm-providers")).json()["default"] == "openai"


class TestClearing:
    async def test_clearing_falls_back_rather_than_switching_off(self, client, monkeypatch):
        """Deleting a key should return the deployment to how it behaved
        before anyone used this page, not disable the provider."""
        monkeypatch.setenv("OPENAI_API_KEY", "env-key")
        await client.put("/admin/llm-providers/openai", json={"api_key": KEY})

        assert (await client.delete("/admin/llm-providers/openai")).status_code == 204

        by_name = {p["name"]: p for p in (await client.get("/admin/llm-providers")).json()["providers"]}
        assert by_name["openai"]["source"] == "environment"
        assert llm_credentials.api_key_for("openai") == "env-key"

    async def test_clearing_keeps_the_default_choice(self, client):
        await client.put("/admin/llm-providers/openai", json={"api_key": KEY, "make_default": True})
        await client.delete("/admin/llm-providers/openai")

        assert (await client.get("/admin/llm-providers")).json()["default"] == "openai"


class TestAccess:
    async def test_a_member_can_neither_read_nor_write(self, build_app, db, as_role):
        from api.admin import router
        from db.engine import get_session

        app = build_app(router)

        async def _session():
            async with db() as session:
                yield session

        app.dependency_overrides[get_session] = _session
        as_role(app, role=roles.MEMBER)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            assert (await c.get("/admin/llm-providers")).status_code == 403
            assert (await c.put("/admin/llm-providers/openai", json={"api_key": KEY})).status_code == 403
            assert (await c.delete("/admin/llm-providers/openai")).status_code == 403
            assert (await c.post("/admin/llm-providers/openai/default")).status_code == 403
