# tests/test_fresh_install.py
"""R6: the fresh-install journey as permanent regression armor.

The 2026-09-01 dress rehearsal walked setup -> db upgrade -> start ->
register by hand on an isolated stack and produced findings R1-R6. This
file pins that journey in CI: a scratch database nobody has touched,
the real `setup` (R2 probe included), the real `db upgrade`, the real
FastAPI lifespan, the first self-registered account, and /health.

Live tier: auto-skips via pg_live on machines without a local Postgres,
same as the other scratch-database tests.
"""
import httpx
from click.testing import CliRunner
from dotenv import dotenv_values
from sqlalchemy import text

from invincible.cli import cli
from invincible.main import app, lifespan
from tests.conftest import TEST_DB_URL

SCRATCH_DB = "invincible_fresh_install"


async def _make_scratch_url(admin_pg) -> str:
    from sqlalchemy.engine import make_url

    await admin_pg(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)")
    await admin_pg(f"CREATE DATABASE {SCRATCH_DB}")
    return make_url(TEST_DB_URL).set(database=SCRATCH_DB).render_as_string()


async def _drop_scratch(admin_pg) -> None:
    await admin_pg(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)")


async def test_fresh_install_journey(admin_pg, pg_live, tmp_path, monkeypatch):
    """setup -> db upgrade -> real lifespan -> first account is operator."""
    # Isolate from any repo-root .env: the generated file is the only
    # source of truth for this journey, exactly as on a fresh machine.
    monkeypatch.chdir(tmp_path)

    scratch_url = await _make_scratch_url(admin_pg)
    env_file = tmp_path / ".env"

    # 1. setup against the scratch DSN - with the real connectivity
    # probe (R2): the database exists, so the probe must succeed.
    result = CliRunner().invoke(
        cli, ["setup", "--env-file", str(env_file), "--db-url", scratch_url]
    )
    assert result.exit_code == 0, result.output
    assert "Database connection verified" in result.output
    # R3: the generated gateway key is explained, not silent.
    assert "Generated GATEWAY_API_KEY" in result.output

    # 2. `invincible start` exports the .env before importing the app;
    # mirror that by loading the generated values into the process env.
    values = {k: v for k, v in dotenv_values(env_file).items() if v}
    assert set(values) >= {
        "GATEWAY_API_KEY", "INVINCIBLE_OWNER_SECRET",
        "INVINCIBLE_DB_URL", "INVINCIBLE_CREDENTIAL_KEY",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    assert values["INVINCIBLE_DB_URL"] == scratch_url

    # 3. Migrate the empty database to head.
    upgrade = CliRunner().invoke(cli, ["db", "upgrade"])
    assert upgrade.exit_code == 0, upgrade.output
    assert "Database upgraded to revision" in upgrade.output

    # 4. Boot through the REAL lifespan - not the hand-wired app.state
    # the conftest client fixture uses. This is the code `invincible
    # start` runs: metadata check, local-owner seed, every store init.
    async with lifespan(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            health = await client.get("/health")
            assert health.status_code == 200
            assert health.json()["status"] == "ok"

            # 5. First self-registered account bootstraps to operator
            # (FIRST-HUMAN BOOTSTRAP in core/accounts.py).
            first = await client.post(
                "/auth/register",
                json={"email": "founder@example.com",
                      "password": "longenough1"},
            )
            assert first.status_code == 201, first.text

            # 6. A second registration must stay a plain user - the
            # bootstrap fires exactly once.
            second = await client.post(
                "/auth/register",
                json={"email": "friend@example.com",
                      "password": "longenough1"},
            )
            assert second.status_code == 201, second.text

        async with app.state.engine.connect() as conn:
            roles = (await conn.execute(text(
                "SELECT email, role FROM users"
                " WHERE is_system = false ORDER BY id"
            ))).all()
        assert roles == [
            ("founder@example.com", "operator"),
            ("friend@example.com", "user"),
        ]

    await _drop_scratch(admin_pg)
