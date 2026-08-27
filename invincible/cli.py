# invincible/cli.py
"""Click CLI for the Invincible gateway.

Both console scripts (``invincible`` and ``inv``) declared in pyproject.toml
point at the ``cli`` group defined here, so the two commands are identical.

Config-surface note (Phase 13/16): the running application reads every env
var through ``core.settings``. This module is the documented exemption - it
acts as launcher/checker, not service code: ``setup`` writes .env files
(and provisions a dev database), ``start`` exports INVINCIBLE_* into the
process before importing the app, ``doctor`` checks key presence
dynamically, and the ``dev-db`` / ``db`` commands open the database
explicitly via INVINCIBLE_DB_URL.
"""
import asyncio
import contextlib
import importlib.resources
import os
import secrets
import shutil
import subprocess
import threading
import time

import asyncpg
import click
import uvicorn
from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from invincible import __version__
from invincible.core.config import load_providers_config
from invincible.core.db import (
    ensure_local_owner,
    make_engine,
    migration_heads,
    migrations_config,
    run_coro_sync,
    stored_schema_revision,
)
from invincible.core.db import metadata as db_metadata
from invincible.core.db_import import import_legacy_sqlite
from invincible.core.identity import ApiKeyStore
from invincible.core.identity import AuditLog as _AuditLog
from invincible.core.oauth_store import OAuthStore

SUPPORTED_ENV_KEYS = (
    "GATEWAY_API_KEY",
    "INVINCIBLE_OWNER_SECRET",
    "NVIDIA_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "GEMINI_API_KEY",
    "TOKENROUTER_API_KEY",
)
SECRET_ENV_KEYS = ("GATEWAY_API_KEY", "INVINCIBLE_OWNER_SECRET")
LEGACY_OWNER_SECRET_KEY = "MCP_SHARED_SECRET"

# --- local dev database (dev-db / setup) ------------------------------------
DEV_DB_PORT = 5433      # project convention: tests + local dev live here
DEV_DB_NAME = "invincible"
DEV_DB_USER = "invincible"
DEV_DB_PASSWORD = "invincible"  # dev-only credentials for containers WE start
DEV_DB_CONTAINER = "invincible-dev-pg"


class DevDbError(Exception):
    """Local Postgres could not be reached or started."""


def _generate_secret() -> str:
    """Generate a new cryptographically random secret (never echoed)."""
    return secrets.token_urlsafe(32)


# --- shared database-URL helpers ---------------------------------------------


def _mask_url(url: str | None) -> str:
    """DSN safe for display: password masked, everything else intact."""
    if not url:
        return ""
    try:
        return make_url(url).render_as_string(hide_password=True)
    except (ArgumentError, ValueError):
        return "<unparseable database url>"


def _normalize_db_url(raw: str) -> str:
    """Validate a pasted INVINCIBLE_DB_URL; returns the canonical
    ``postgresql+asyncpg`` form. Raises ``ValueError`` with an
    operator-facing message when the input cannot be used as-is.

    Plain ``postgresql://`` URLs are auto-upgraded to the asyncpg driver -
    they would otherwise pass a loose prefix check here and fail confusingly
    at `invincible start` later.
    """
    try:
        parsed = make_url(raw.strip())
    except ArgumentError as exc:
        raise ValueError(f"Not a valid database URL: {exc}") from exc
    driver = parsed.drivername
    if driver == "postgresql":
        click.echo(
            "Normalized postgresql:// -> postgresql+asyncpg:// "
            "(asyncpg is the only supported driver)"
        )
        parsed = parsed.set(drivername="postgresql+asyncpg")
    elif driver != "postgresql+asyncpg":
        raise ValueError(
            f"Unsupported driver '{driver}' - Invincible requires "
            "postgresql+asyncpg://"
        )
    # hide_password=False is REQUIRED: SA 2.0 masks by default, which would
    # silently write a broken DSN into .env.
    return parsed.render_as_string(hide_password=False)


def _resolve_db_url() -> str:
    """INVINCIBLE_DB_URL from the environment/.env - the single source of
    truth for every command that opens the database directly. The DSN is a
    secret-bearing connection string, so there is deliberately no
    --db-url flag anywhere; override per invocation via the environment."""
    url = os.getenv("INVINCIBLE_DB_URL")
    if not url:
        raise click.ClickException(
            "INVINCIBLE_DB_URL is not set. Run `invincible dev-db` to "
            "provision a local development database, or `invincible setup`."
        )
    return url


# --- local dev-database provisioning -----------------------------------------


def _plain_dsn(url: str) -> str:
    """Strip the +asyncpg driver for raw asyncpg.connect() calls."""
    return make_url(url).set(
        drivername="postgresql"
    ).render_as_string(hide_password=False)


async def _try_connect(dsn: str) -> bool:
    """Cheap reachability probe with a short timeout. Any failure counts
    as unreachable - this must never crash the caller."""
    try:
        conn = await asyncpg.connect(_plain_dsn(dsn), timeout=3)
    except Exception:
        return False
    await conn.close()
    return True


def _admin_dsn(host: str, port: int, user: str,
               password: str | None = None) -> str:
    auth = f":{password}" if password else ""
    return f"postgresql://{user}{auth}@{host}:{port}/postgres"


def _app_dsn_from_admin(dsn: str, database: str = DEV_DB_NAME) -> str:
    """The application-facing +asyncpg URL for the dev database."""
    return (
        make_url(dsn)
        .set(drivername="postgresql+asyncpg", database=database)
        .render_as_string(hide_password=False)
    )


async def _ensure_database(admin_dsn: str, database: str = DEV_DB_NAME) -> bool:
    """Create ``database`` on a reachable server if missing.

    Returns True when created, False when it already existed."""
    conn = await asyncpg.connect(admin_dsn, timeout=3)
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", database
        )
        if exists:
            return False
        # Identifiers cannot be query parameters in CREATE DATABASE.
        if not database.isidentifier():
            raise DevDbError(f"Unsafe database name: {database!r}")
        await conn.execute(f'CREATE DATABASE "{database}"')
        return True
    finally:
        await conn.close()


def _start_postgres_via_docker(port: int) -> str:
    """Start a disposable Postgres container and return its admin DSN.

    Prefers the bundled compose pair when running from a checkout; falls
    back to a plain `docker run`. Raises DevDbError with guidance when
    Docker is unavailable or fails."""
    docker = shutil.which("docker")
    if not docker:
        raise DevDbError(
            "No reachable PostgreSQL found and Docker is not available. "
            "Install/start PostgreSQL locally, or run `docker run -d --name "
            f"{DEV_DB_CONTAINER} -e POSTGRES_USER={DEV_DB_USER} "
            f"-e POSTGRES_PASSWORD={DEV_DB_PASSWORD} -p {port}:5432 "
            "postgres:16-alpine`, then rerun this command."
        )
    repo_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    compose_file = os.path.join(repo_root, "docker-compose.yml")
    cwd = None
    if os.path.isfile(compose_file):
        cmd = [docker, "compose", "up", "-d", "db"]
        cwd = repo_root
    else:
        cmd = [
            docker, "run", "-d", "--name", DEV_DB_CONTAINER,
            "-e", f"POSTGRES_USER={DEV_DB_USER}",
            "-e", f"POSTGRES_PASSWORD={DEV_DB_PASSWORD}",
            "-p", f"127.0.0.1:{port}:5432",
            "postgres:16-alpine",
        ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=180, cwd=cwd,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DevDbError(f"Could not start Docker Postgres: {exc}") from exc
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()
        detail = tail[-1] if tail else f"exit code {result.returncode}"
        raise DevDbError(f"Docker Postgres failed to start: {detail}")
    return _admin_dsn("127.0.0.1", port, DEV_DB_USER, DEV_DB_PASSWORD)


async def _provision_dev_db_async(port: int) -> tuple[str, list[str]]:
    """Find or start a local Postgres server and make sure the dev database
    exists. Returns ``(app_url, notes)``. Raises DevDbError when nothing
    can be provisioned."""
    notes: list[str] = []

    existing = os.getenv("INVINCIBLE_DB_URL")
    if existing:
        if await _try_connect(existing):
            return existing, ["verified existing INVINCIBLE_DB_URL"]
        notes.append(
            "INVINCIBLE_DB_URL is set but unreachable; provisioning locally"
        )

    candidates = [
        _admin_dsn("127.0.0.1", port, DEV_DB_USER),
        _admin_dsn("127.0.0.1", port, "postgres"),
        _admin_dsn("localhost", 5432, DEV_DB_USER),
        _admin_dsn("localhost", 5432, "postgres"),
    ]
    admin = None
    for dsn in candidates:
        if await _try_connect(dsn):
            admin = dsn
            notes.append(f"found local Postgres ({_mask_url(dsn)})")
            break

    if admin is None:
        admin = _start_postgres_via_docker(port)
        notes.append("started Postgres via Docker")
        for _ in range(30):  # first start / image pull can take a while
            if await _try_connect(admin):
                break
            await asyncio.sleep(1)
        else:
            raise DevDbError(
                "Docker Postgres did not become reachable within 30s - "
                "check `docker logs invincible-dev-pg` and retry."
            )

    created = await _ensure_database(admin)
    verb = "created database" if created else "database already present:"
    notes.append(f"{verb} '{DEV_DB_NAME}'")
    return _app_dsn_from_admin(admin), notes


def _provision_dev_db(port: int = DEV_DB_PORT) -> tuple[str, list[str]]:
    """Sync entry point shared by `dev-db` and `setup`; never raises
    anything but ClickException."""
    try:
        return run_coro_sync(_provision_dev_db_async(port))
    except DevDbError as exc:
        raise click.ClickException(str(exc)) from exc


def _parse_env_line(line):
    """Best-effort KEY=VALUE parser that keeps inline comments intact.

    Returns ``(key, value, trailing)`` where ``trailing`` is any inline
    comment (including its leading whitespace) to re-attach when a line is
    rewritten, or None when the line does not define a key. Handles
    ``KEY=value``, ``KEY="value"`` and ``KEY='value'``.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith(("#", ";")):
        return None
    if "=" not in stripped:
        return None
    key, _, rest = stripped.partition("=")
    key = key.strip()
    if not key:
        return None
    value = rest.strip()
    trailing = ""
    if value.startswith(('"', "'")):
        quote = value[0]
        closing = value.find(quote, 1)
        if closing != -1:
            trailing = value[closing + 1:]
            value = value[1:closing]
    else:
        comment_at = value.find(" #")
        if comment_at != -1:
            trailing = value[comment_at:]
            value = value[:comment_at].rstrip()
    return key, value, trailing


@click.command()
@click.option(
    "--env-file", default=".env", show_default=True,
    help="Path of the .env file to create or update.",
)
@click.option(
    "--force", is_flag=True,
    help="Re-prompt variables that already have values.",
)
def setup(env_file, force):
    """Create or update a .env file with gateway and provider keys."""
    env_path = os.path.abspath(env_file)

    existing = {}
    lines = []
    if os.path.isfile(env_path):
        try:
            with open(env_path, encoding="utf-8") as f:
                content = f.read()
        except OSError as exc:
            msg = f"Could not read env file {env_path}: {exc}"
            raise click.ClickException(msg) from exc
        lines = content.splitlines(keepends=True)
        for line in lines:
            parsed = _parse_env_line(line)
            if parsed:
                existing.setdefault(parsed[0], parsed[1])

    new_values = {}

    # Migration: the owner secret replaced MCP_SHARED_SECRET. When the new
    # key is absent but the old one exists, carry the value over so existing
    # deployments work unchanged (the legacy line is kept as a fallback).
    if (
        "INVINCIBLE_OWNER_SECRET" not in existing
        and LEGACY_OWNER_SECRET_KEY in existing
    ):
        carried = existing[LEGACY_OWNER_SECRET_KEY]
        new_values["INVINCIBLE_OWNER_SECRET"] = carried
        existing["INVINCIBLE_OWNER_SECRET"] = carried
        click.echo(
            "Carried MCP_SHARED_SECRET over to INVINCIBLE_OWNER_SECRET "
            "(it is now the owner-login secret for approving MCP connections)."
        )

    for key in SUPPORTED_ENV_KEYS:
        current = existing.get(key, "")
        if key == "INVINCIBLE_OWNER_SECRET":
            label = (
                "INVINCIBLE_OWNER_SECRET (one-time login to /oauth/authorize "
                "for approving MCP connections; not sent on /mcp any more)"
            )
        else:
            label = key
        if key in SECRET_ENV_KEYS:
            if current and not force:
                continue
            if current:
                new_values[key] = click.prompt(
                    f"{label} (leave empty to keep the existing value)",
                    default=current, hide_input=True, show_default=False,
                )
            else:
                # Never printed; write straight to the env file.
                new_values[key] = _generate_secret()
        else:
            if current and not force:
                continue
            if current:
                entered = click.prompt(
                    f"{key} (leave empty to keep the existing value)",
                    default=current, hide_input=True, show_default=False,
                )
                if entered:
                    new_values[key] = entered
            else:
                entered = click.prompt(
                    f"{key} (leave empty to skip)",
                    default="", hide_input=True, show_default=False,
                )
                if entered:
                    new_values[key] = entered

    # Database backend (Phase 16): INVINCIBLE_DB_URL is a secret-bearing
    # connection string, so it gets its own interactive step instead of the
    # flat prompt list. Existing values are left alone unless --force.
    if not existing.get("INVINCIBLE_DB_URL") or force:
        choice = click.prompt(
            "Database backend (INVINCIBLE_DB_URL)",
            type=click.Choice(("paste", "dev-db", "skip")),
            default="skip",
        )
        if choice == "paste":
            while True:
                entered = click.prompt(
                    "PostgreSQL URL (postgresql+asyncpg://...)"
                )
                if not entered.strip():
                    break  # empty keeps any existing value untouched
                try:
                    new_values["INVINCIBLE_DB_URL"] = _normalize_db_url(entered)
                    break
                except ValueError as exc:
                    click.echo(f"Invalid URL: {exc}", err=True)
        elif choice == "dev-db":
            click.echo("Provisioning a local development database...")
            try:
                url, notes = _provision_dev_db()
            except (DevDbError, click.ClickException) as exc:
                message = (
                    exc.message
                    if isinstance(exc, click.ClickException)
                    else str(exc)
                )
                click.echo(
                    f"Could not provision locally ({message}); "
                    "skipping INVINCIBLE_DB_URL.",
                    err=True,
                )
            else:
                for note in notes:
                    click.echo(f"dev-db: {note}")
                new_values["INVINCIBLE_DB_URL"] = url

    _apply_env_updates(env_path, lines, new_values)

    click.echo(f"Configured {env_path}")


def _apply_env_updates(env_path, lines, new_values, remove_keys=()):
    """Rewrite an env file's lines in place: replace values for keys in
    ``new_values`` (re-attaching any inline comment), drop lines whose key is
    in ``remove_keys``, append keys in ``new_values`` that are not present,
    and leave every other line (comments, blank lines, unrelated vars,
    ordering) byte-for-byte untouched.

    Shared by ``setup`` and ``secret rotate`` so file handling never
    diverges between generation and rotation.
    """
    output_lines = []
    seen = set()
    for line in lines:
        parsed = _parse_env_line(line)
        if parsed:
            key = parsed[0]
            seen.add(key)
            if key in remove_keys:
                continue
            if key in new_values:
                output_lines.append(f"{key}={new_values[key]}{parsed[2]}\n")
                continue
        output_lines.append(line)

    if output_lines and not output_lines[-1].endswith("\n"):
        output_lines[-1] += "\n"
    for key, value in new_values.items():
        if key not in seen:
            output_lines.append(f"{key}={value}\n")

    text = "".join(output_lines)
    if text and not text.endswith("\n"):
        text += "\n"

    try:
        with open(env_path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as exc:
        msg = f"Could not write env file {env_path}: {exc}"
        raise click.ClickException(msg) from exc
    return text


def _load_env_file(env_file: str) -> str | None:
    """Load an env file into the process environment without overriding
    existing variables. Returns the absolute path when loaded, else None."""
    env_abs = os.path.abspath(env_file)
    if os.path.isfile(env_abs):
        load_dotenv(dotenv_path=env_abs, override=False)
        return env_abs
    return None


TUNNEL_NAME_ENV_KEY = "INVINCIBLE_TUNNEL_NAME"
DEFAULT_TUNNEL_NAME = "invincible"


def _resolve_tunnel_name(tunnel_name: str | None = None) -> str:
    """Tunnel name for `cloudflared tunnel run`: an explicit flag wins,
    then INVINCIBLE_TUNNEL_NAME, then the default."""
    if tunnel_name:
        return tunnel_name
    return os.getenv(TUNNEL_NAME_ENV_KEY) or DEFAULT_TUNNEL_NAME


def _tunnel_output_reader(proc, stopping):
    """Forward cloudflared output to the console and surface key events.

    Runs on a daemon thread so a wedged pipe can never block process exit.
    Never raises. ``stopping`` is a threading.Event set by _stop_tunnel to
    suppress the "tunnel exited" warning during a deliberate shutdown.
    """
    connected = False
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            click.echo(f"[tunnel] {line}")
            if "Registered tunnel connection" in line:
                if not connected:
                    connected = True
                    click.echo("[tunnel] Cloudflare tunnel connected.")
            elif "https://" in line:
                click.echo(f"[tunnel] Tunnel URL: {line.strip()}")
    except (OSError, ValueError):
        pass
    returncode = proc.poll()
    if returncode is not None and returncode != 0 and not stopping.is_set():
        click.echo(
            f"Warning: Cloudflare tunnel exited (code {returncode}) - "
            "check the tunnel credentials with `cloudflared tunnel list`; "
            "the server is still running locally", err=True,
        )


def _start_tunnel(tunnel_name: str):
    """Spawn `cloudflared tunnel run <name>` as a child of this process.

    The child shares our console process group (no creationflags, no
    start_new_session), so a terminal Ctrl+C reaches cloudflared directly.
    Returns ``(proc, reader, stopping)``, or ``(None, None, None)`` when
    cloudflared is unavailable. The caller MUST route every shutdown path
    through _stop_tunnel, which never raises.
    """
    cloudflared = shutil.which("cloudflared")
    if cloudflared is None:
        click.echo(
            "Warning: cloudflared not found on PATH; starting without "
            "tunnel (use --no-tunnel to silence this)", err=True,
        )
        return None, None, None
    click.echo(f"Starting Cloudflare tunnel '{tunnel_name}'...")
    try:
        proc = subprocess.Popen(
            [cloudflared, "tunnel", "run", tunnel_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except OSError as exc:
        # TOCTOU guard: the binary may vanish or stop being executable
        # between which() and Popen; without this the tunnel spawn would
        # crash `inv start` before the cleanup finally exists.
        click.echo(
            f"Warning: could not start cloudflared ({exc}); starting "
            "without tunnel (use --no-tunnel to silence this)", err=True,
        )
        return None, None, None
    stopping = threading.Event()
    reader = threading.Thread(
        target=_tunnel_output_reader,
        args=(proc, stopping),
        name="cloudflared-output",
        daemon=True,
    )
    reader.start()
    return proc, reader, stopping


def _stop_tunnel(proc, reader, stopping) -> None:
    """Tear down a spawned cloudflared process. NEVER RAISES.

    Every step is individually guarded because this function is the single
    cleanup funnel for the tunnel: the orphan-free guarantee rests on it.
    A slow QUIC/HTTP2 teardown on Windows can push cloudflared past the
    terminate grace period; the kill() fallback triggering then is
    expected, not a bug.
    """
    if proc is None:
        return
    stopping.set()
    was_running = proc.poll() is None
    if was_running:
        with contextlib.suppress(OSError):
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # Process may have exited between wait() and kill(); the
            # Ctrl+C broadcast on Windows makes this race real.
            with contextlib.suppress(OSError):
                proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                proc.wait(timeout=2)
        except OSError:
            pass
    if proc.stdout is not None:
        with contextlib.suppress(OSError):
            proc.stdout.close()
    reader.join(timeout=2.0)
    if was_running:
        click.echo("Cloudflare tunnel stopped.")


@click.command()
@click.option("--host", default="127.0.0.1", show_default=True,
              help="Interface to bind the server to.")
@click.option("--port", default=8000, show_default=True, type=int,
              help="Port to listen on (1-65535).")
@click.option("--reload", is_flag=True,
              help="Auto-reload on source changes (development).")
@click.option("--log-level", default="info", show_default=True,
              help="Uvicorn log level.")
@click.option("--env-file", default=".env", show_default=True,
              help=".env file to load before startup.")
@click.option("--config", "config_path", type=click.Path(dir_okay=False),
              default=None, help="Custom providers.yaml configuration.")
@click.option("--tunnel/--no-tunnel", default=True,
              help="Start a Cloudflare tunnel alongside the server.")
@click.option("--tunnel-name", default=None,
              help="Cloudflare tunnel name for `cloudflared tunnel run` "
                   f"(default: {TUNNEL_NAME_ENV_KEY} env var or "
                   f"'{DEFAULT_TUNNEL_NAME}').")
def start(host, port, reload, log_level, env_file, config_path,
          tunnel, tunnel_name):
    """Start the Invincible gateway server."""
    if not 1 <= port <= 65535:
        raise click.ClickException(f"--port must be between 1 and 65535 (got {port})")

    env_abs = _load_env_file(env_file)
    if env_abs:
        click.echo(f"Loaded environment from {env_abs}")
    else:
        click.echo(
            f"Warning: env file not found: {os.path.abspath(env_file)}", err=True
        )

    if config_path:
        config_abs = os.path.abspath(config_path)
        try:
            load_providers_config(config_abs)
        except (OSError, ValueError) as exc:
            raise click.ClickException(str(exc)) from exc
        os.environ["INVINCIBLE_CONFIG_PATH"] = config_abs

    # INVINCIBLE_DB_URL comes from the environment/.env only - there is no
    # --db-path/--db-url flag: the DSN is a secret-bearing connection string.
    if not env_abs and not os.getenv("INVINCIBLE_DB_URL"):
        click.echo(
            "Warning: INVINCIBLE_DB_URL is not set and no env file was "
            f"found at {os.path.abspath(env_file)} - startup will fail. "
            "Run `invincible dev-db` first.", err=True,
        )

    if host == "0.0.0.0":
        click.echo(
            f"Invincible starting; binding to 0.0.0.0:{port} "
            f"(local access: http://127.0.0.1:{port})"
        )
    else:
        click.echo(f"Invincible starting at http://{host}:{port}")

    # Spawning the tunnel is deliberately the LAST statement before the
    # `try` block: nothing may execute between the two, because any
    # exception raised there would orphan the cloudflared process before
    # the cleanup finally exists.
    tunnel_proc = tunnel_reader = tunnel_stopping = None
    if tunnel:
        tunnel_proc, tunnel_reader, tunnel_stopping = _start_tunnel(
            _resolve_tunnel_name(tunnel_name)
        )

    # With --reload the tunnel is tied to the uvicorn supervisor process,
    # not the app child: file-change restarts keep the tunnel up (correct),
    # and an app-child crash does not propagate as an exception (uvicorn
    # keeps supervising). Cleanup below still runs when the supervisor
    # itself stops. Do not "fix" this later.
    try:
        uvicorn.run(
            "invincible.main:app",
            host=host,
            port=port,
            reload=reload,
            log_level=log_level,
        )
    except KeyboardInterrupt:
        # Set stopping before any console output so the tunnel reader
        # does not race a Ctrl+C-driven cloudflared exit and print a
        # spurious 'tunnel exited' warning (Windows shares the console
        # process group; cloudflared receives CTRL_C_EVENT directly).
        if tunnel_stopping is not None:
            tunnel_stopping.set()
        click.echo("Shutting down.")
    finally:
        _stop_tunnel(tunnel_proc, tunnel_reader, tunnel_stopping)


def _legacy_providers_path() -> str:
    """Repository-root providers.yaml, mirroring router.py's fallback."""
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "providers.yaml",
    )


def _doctor_config_source() -> str:
    """Resolve the effective providers.yaml path for diagnostics.

    INVINCIBLE_CONFIG_PATH wins when set; otherwise the packaged
    invincible/providers.yaml, falling back to the legacy repository-root
    copy - same resolution order as the router.
    """
    env_path = os.getenv("INVINCIBLE_CONFIG_PATH")
    if env_path:
        return os.path.abspath(env_path)
    try:
        ref = importlib.resources.files("invincible").joinpath("providers.yaml")
    except (ModuleNotFoundError, TypeError, AttributeError):
        return _legacy_providers_path()
    if ref.is_file():
        return str(ref)
    return _legacy_providers_path()


async def _has_any_schema_tables(engine) -> bool:
    """Whether any Phase 16 application table exists in the database."""
    names = sorted(db_metadata.tables)
    async with engine.connect() as conn:
        count = (await conn.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = ANY(:names)"
            ),
            {"names": names},
        )).scalar_one()
    return bool(count)


async def _schema_revision_row(engine):
    """(label, ok, note) comparing the stored alembic revision to the
    packaged migration head. Unmanaged or stale schemas are loud FAILs."""
    label = "schema revision matches head"
    heads = migration_heads()
    stored = await stored_schema_revision(engine)
    if heads is None:
        return (label, False, "packaged migration scripts unavailable")
    if stored is None:
        if await _has_any_schema_tables(engine):
            return (
                label, False,
                "tables exist but are unmanaged by Alembic - run "
                "`invincible db upgrade`",
            )
        return (
            label, False,
            "empty database - run `invincible db upgrade`",
        )
    if stored not in heads:
        return (
            label, False,
            f"database at {stored}, expected {'/'.join(heads)} - run "
            "`invincible db upgrade`",
        )
    return (label, True, f"revision {stored}")


async def _check_database(url: str | None):
    """Connectivity + schema revision against INVINCIBLE_DB_URL (Phase 16).

    Returns three ``(label, ok, note)`` rows so doctor reports connectivity
    and schema status separately, per the acceptance criteria. The DSN is
    always rendered password-masked."""
    if not url:
        hint = "run `invincible dev-db` or `invincible setup`"
        return [
            ("INVINCIBLE_DB_URL exists", False, hint),
            ("PostgreSQL reachable", False, "no URL configured"),
            ("schema revision matches head", False, "no URL configured"),
        ]
    engine = make_engine(url)
    try:
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:
            # Connection failures can echo DSN internals; mask the URL and
            # keep only the first line of the error.
            detail = str(exc).splitlines()[0][:120]
            return [
                ("INVINCIBLE_DB_URL exists", True, ""),
                ("PostgreSQL reachable", False, f"{_mask_url(url)}: {detail}"),
                ("schema revision matches head", False,
                 "database unreachable"),
            ]
        return [
            ("INVINCIBLE_DB_URL exists", True, ""),
            ("PostgreSQL reachable", True, _mask_url(url)),
            await _schema_revision_row(engine),
        ]
    finally:
        await engine.dispose()


def _run_doctor_checks():
    """Return (label, ok, note) tuples for every diagnostic check."""
    checks = []

    source = _doctor_config_source()
    checks.append(("providers.yaml exists", os.path.isfile(source), source))

    try:
        load_providers_config(_doctor_config_source())
        checks.append(("providers.yaml loads", True, ""))
    except (OSError, ValueError) as exc:
        checks.append(("providers.yaml loads", False, str(exc)))

    checks.extend(
        run_coro_sync(_check_database(os.getenv("INVINCIBLE_DB_URL")))
    )

    for key in ("GATEWAY_API_KEY",):
        checks.append((f"{key} exists", bool(os.getenv(key)), ""))

    owner = os.getenv("INVINCIBLE_OWNER_SECRET")
    legacy = os.getenv(LEGACY_OWNER_SECRET_KEY)
    note = "falling back to MCP_SHARED_SECRET" if (legacy and not owner) else ""
    checks.append((
        "INVINCIBLE_OWNER_SECRET exists (owner login for /mcp)",
        bool(owner or legacy),
        note,
    ))

    return checks


def _doctor_console():
    """Return a rich Console if rich is installed, else None."""
    try:
        from rich.console import Console

        return Console()
    except ImportError:
        return None


@click.command()
@click.option("--env-file", default=".env", show_default=True,
              help=".env file to load before running checks.")
def doctor(env_file):
    """Run environment and configuration diagnostics."""
    _load_env_file(env_file)
    checks = _run_doctor_checks()
    console = _doctor_console()

    lines = []
    lines.append(f"Invincible version: {__version__}")
    for label, ok, note in checks:
        mark = "OK" if ok else "FAIL"
        line = f"{mark}  {label}"
        if note:
            line += f"  ({note})"
        lines.append(line)

    if console is not None:
        for line in lines:
            colored = line.replace("OK  ", "[green]OK[/green]  ", 1)
            colored = colored.replace("FAIL", "[red]FAIL[/red]", 1)
            console.print(colored)
    else:
        for line in lines:
            click.echo(line)

    if any(not ok for _, ok, _ in checks):
        raise click.exceptions.Exit(1)


# --- secret rotation ---


@click.group()
def secret():
    """Rotate gateway secrets stored in the .env file."""


@secret.command("rotate")
@click.option("--env-file", default=".env", show_default=True,
              help="Path of the .env file to rotate the secret in.")
@click.option("--show", is_flag=True,
              help="Print the new secret to the terminal (off by default).")
def secret_rotate(env_file, show):
    """Generate a new INVINCIBLE_OWNER_SECRET and write it to .env.

    Preserves every other line, comment, and ordering; a legacy
    MCP_SHARED_SECRET line (if present) is migrated to the new key at the
    same time. The new value is never echoed unless --show is passed.
    Existing OAuth grants are NOT invalidated by rotation - use
    `invincible oauth revoke <client_id>` for that.
    """
    env_path = os.path.abspath(env_file)

    if not os.path.isfile(env_path):
        raise click.ClickException(
            f"No env file found at {env_path}. Run `invincible setup` "
            "first to create one."
        )
    try:
        with open(env_path, encoding="utf-8") as f:
            content = f.read()
    except OSError as exc:
        msg = f"Could not read env file {env_path}: {exc}"
        raise click.ClickException(msg) from exc
    lines = content.splitlines(keepends=True)

    parsed_keys = {
        p[0] for p in (_parse_env_line(line) for line in lines) if p
    }
    if not (
        "INVINCIBLE_OWNER_SECRET" in parsed_keys
        or LEGACY_OWNER_SECRET_KEY in parsed_keys
    ):
        raise click.ClickException(
            f"No owner secret found in {env_path}. Run `invincible setup` "
            "first so it can create one for you."
        )

    new_secret = _generate_secret()
    remove_keys = (
        (LEGACY_OWNER_SECRET_KEY,)
        if LEGACY_OWNER_SECRET_KEY in parsed_keys
        else ()
    )
    _apply_env_updates(
        env_path, lines, {"INVINCIBLE_OWNER_SECRET": new_secret},
        remove_keys=remove_keys,
    )

    click.echo("New owner secret generated and saved to .env")
    click.echo("Restart Invincible for the new secret to take effect")
    click.echo(
        "Anyone with an existing browser session (from the old consent-page "
        "login) will need to log in again next time they approve a new "
        "connection"
    )
    if show:
        click.echo(f"INVINCIBLE_OWNER_SECRET={new_secret}")




@secret.command("credential-key")
@click.option("--env-file", default=".env", show_default=True,
              help="Path of the .env file to write the credential key into.")
@click.option("--show", is_flag=True,
              help="Print the new key to the terminal (off by default).")
def secret_credential_key(env_file, show):
    """Generate a Fernet key for INVINCIBLE_CREDENTIAL_KEY and write it to .env.

    Required before BYOK provider connections can store encrypted user API
    keys. Missing or malformed values make every /providers/mine surface
    fail closed. The new value is never echoed unless --show is passed.
    Restart Invincible after writing so the running process picks it up.
    """
    from cryptography.fernet import Fernet

    env_path = os.path.abspath(env_file)

    if not os.path.isfile(env_path):
        raise click.ClickException(
            f"No env file found at {env_path}. Run `invincible setup` "
            "first to create one."
        )
    try:
        with open(env_path, encoding="utf-8") as f:
            content = f.read()
    except OSError as exc:
        msg = f"Could not read env file {env_path}: {exc}"
        raise click.ClickException(msg) from exc
    lines = content.splitlines(keepends=True)

    new_key = Fernet.generate_key().decode("ascii")
    _apply_env_updates(
        env_path, lines, {"INVINCIBLE_CREDENTIAL_KEY": new_key},
    )

    click.echo("INVINCIBLE_CREDENTIAL_KEY generated and saved to .env")
    click.echo("Restart Invincible for the new key to take effect")
    click.echo(
        "Existing BYOK credentials encrypted under a previous key will "
        "not decrypt until re-connected."
    )
    if show:
        click.echo(f"INVINCIBLE_CREDENTIAL_KEY={new_key}")


# --- oauth administration ---


def _format_ts(timestamp: float) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp))
    except (OverflowError, OSError, ValueError):
        return "?"


@click.group()
@click.option("--env-file", default=".env", show_default=True,
              help=".env file to load before running (existing process "
                   "environment always wins).")
@click.pass_context
def oauth(ctx, env_file):
    """Inspect and revoke OAuth (MCP bearer-token) grants."""
    _load_env_file(env_file)


def _open_oauth_store():
    """OAuthStore over a fresh engine built from INVINCIBLE_DB_URL.

    The caller owns the engine and must dispose it (the store's own
    init/close are no-ops since Phase 16 - engines belong to callers)."""
    return OAuthStore(make_engine(_resolve_db_url()))


@oauth.command("list")
def oauth_list():
    """List registered OAuth clients and their active grants."""
    url = _resolve_db_url()
    engine = make_engine(url)
    store = OAuthStore(engine)

    async def _run():
        try:
            clients = await store.list_clients()
            for client in clients:
                name = client["client_name"] or "(unnamed)"
                uris = ", ".join(client["redirect_uris"])
                click.echo(
                    f"{client['client_id']}  {name}\n"
                    f"  registered: {_format_ts(client['created_at'])}\n"
                    f"  redirect URIs: {uris}"
                )
                tokens = await store.list_active_tokens(client["client_id"])
                active = [t for t in tokens if not t["revoked"]]
                revoked = [t for t in tokens if t["revoked"]]
                for token in active:
                    click.echo(
                        f"  active {token['token_type']}: expires "
                        f"{_format_ts(token['expires_at'])}"
                    )
                if revoked:
                    click.echo(f"  revoked: {len(revoked)}")
                if not tokens:
                    click.echo("  no grants")
            if not clients:
                click.echo("No registered OAuth clients.")
        finally:
            await engine.dispose()

    run_coro_sync(_run())


@oauth.command("revoke")
@click.argument("client_id")
def oauth_revoke(client_id):
    """Revoke every access/refresh token issued to a client.

    New connections from that client will fail until it is re-registered
    and approved again in the browser.

    Client ids are random URL-safe strings that may start with '-'. Because
    Click parses such an id as an option, pass it after a '--' separator:
    invincible oauth revoke -- <client_id>
    """
    url = _resolve_db_url()
    engine = make_engine(url)
    store = OAuthStore(engine)

    async def _run():
        try:
            client = await store.get_client(client_id)
            if client is None:
                raise click.ClickException(f"Unknown client id: {client_id}")
            count = await store.revoke_client_tokens(client_id)
            click.echo(f"Revoked {count} token(s) for client {client_id}.")
        finally:
            await engine.dispose()

    try:
        run_coro_sync(_run())
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(f"Could not revoke tokens: {exc}") from exc


@oauth.command("test-client")
@click.option("--redirect-uri", default="http://127.0.0.1:9999/callback",
              show_default=True,
              help="Loopback redirect URI to register for the test client.")
def oauth_test_client(redirect_uri):
    """Headless helper: register a client, approve it, and print a Bearer
    token - so /mcp can be exercised with curl without a browser."""
    import base64
    import hashlib
    from urllib.parse import urlparse

    import httpx

    from invincible.main import app

    owner = os.getenv("INVINCIBLE_OWNER_SECRET") or os.getenv(LEGACY_OWNER_SECRET_KEY)
    if not owner:
        raise click.ClickException(
            "INVINCIBLE_OWNER_SECRET is not set; cannot authenticate as owner."
        )
    parsed = urlparse(redirect_uri)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise click.ClickException(f"Invalid redirect URI: {redirect_uri}")
    db_url = _resolve_db_url()

    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode("ascii")

    async def _run():
        engine = make_engine(db_url)
        store = OAuthStore(engine)
        app.state.oauth_store = store
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                cookies={},
            ) as client:
                client_id, code = await _headless_approve(
                    client, owner, redirect_uri, challenge
                )
                response = await client.post(
                    "/oauth/token",
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "client_id": client_id,
                        "redirect_uri": redirect_uri,
                        "code_verifier": verifier,
                    },
                )
            if response.status_code != 200:
                raise click.ClickException(
                    f"Token exchange failed ({response.status_code}): {response.text}"
                )
            tokens = response.json()
        finally:
            await engine.dispose()

        click.echo(f"client_id:   {client_id}")
        click.echo("registered:  http://127.0.0.1:8000/oauth/authorize (approve)")
        click.echo(f"access token expires in {tokens['expires_in']}s")
        click.echo("")
        click.echo("List MCP tools with:")
        click.echo(
            f'curl -X POST http://127.0.0.1:8000/mcp '
            f'-H "Authorization: Bearer {tokens["access_token"]}" '
            f'-H "Content-Type: application/json" '
            f'-d \'{{"jsonrpc":"2.0","id":1,"method":"tools/list"}}\''
        )
        click.echo("")
        click.echo(
            f"Full OAuth response saved below (refresh token included):\n{tokens}"
        )

    try:
        run_coro_sync(_run())
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(f"Test client failed: {exc}") from exc


async def _headless_approve(client, owner_secret_value, redirect_uri, challenge):
    """Drive register -> login -> consent through the real endpoints.
    Returns (client_id, authorization code)."""
    from urllib.parse import parse_qs, urlparse

    registration = await client.post(
        "/oauth/register",
        json={
            "redirect_uris": [redirect_uri],
            "client_name": "invincible oauth test-client",
        },
    )
    if registration.status_code != 201:
        raise click.ClickException(
            f"Registration failed ({registration.status_code}): {registration.text}"
        )
    client_id = registration.json()["client_id"]
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    login = await client.post(
        "/oauth/authorize", data={**params, "owner_secret": owner_secret_value}
    )
    if login.status_code != 302:
        raise click.ClickException(
            f"Owner login failed ({login.status_code}): {login.text[:200]}"
        )
    approved = await client.post(
        "/oauth/authorize",
        data={**params, "action": "approve"},
        follow_redirects=False,
    )
    location = approved.headers.get("location", "")
    code = parse_qs(urlparse(location).query).get("code", [None])[0]
    if approved.status_code != 302 or not code:
        raise click.ClickException(
            f"Consent approval failed ({approved.status_code}): {approved.text[:200]}"
        )
    return client_id, code


# --- API keys (Platform Phase 1) -----------------------------------------------


@click.group("api-key")
def api_key():
    """Manage gateway API keys (hashed at rest; raw shown once)."""


@api_key.command("create")
@click.option("--label", default="", show_default=True,
              help="Free-form label shown by `api-key list`.")
def api_key_create(label):
    """Mint an API key under the system *local* owner.

    The raw key is printed ONCE and never stored - only its SHA-256 hash
    and a visible prefix are kept. Use it as
    `Authorization: Bearer <key>` on the /v1/* endpoints.
    """
    engine = make_engine(_resolve_db_url())

    async def _run():
        try:
            user_id, _ = await ensure_local_owner(engine)
            record = await ApiKeyStore(engine).create(user_id, label=label)
            await _AuditLog(engine).record(
                "auth.api_key_created",
                actor_user_id=user_id,
                actor_kind="system",
                resource_type="api_key",
                resource_id=record["prefix"],
                meta={"label": label},
            )
            return record
        finally:
            await engine.dispose()

    record = run_coro_sync(_run())
    click.echo(f"API key created (prefix {record['prefix']}).")
    click.echo(record["raw"])
    click.echo("Store it now - it is shown once and cannot be recovered.")


@api_key.command("list")
def api_key_list():
    """List API keys. Hashes are never displayed; raw keys were shown once
    at creation."""
    engine = make_engine(_resolve_db_url())

    async def _run():
        try:
            return await ApiKeyStore(engine).list()
        finally:
            await engine.dispose()

    keys = run_coro_sync(_run())
    if not keys:
        click.echo("No API keys.")
        return
    for key in keys:
        status = (
            f"revoked {_format_ts(key['revoked_at'])}"
            if key["revoked_at"]
            else "active"
        )
        last_used = (
            f"last used {_format_ts(key['last_used_at'])}"
            if key["last_used_at"]
            else "never used"
        )
        name = f"  {key['label']}" if key["label"] else ""
        click.echo(
            f"{key['prefix']}  #{key['id']}{name}\n"
            f"  created {_format_ts(key['created_at'])}, "
            f"{last_used}, {status}"
        )


@api_key.command("revoke")
@click.argument("key_ref")
def api_key_revoke(key_ref):
    """Revoke a key by its numeric id or visible prefix.

    Existing requests carrying the raw key stop working immediately.
    """
    engine = make_engine(_resolve_db_url())

    async def _run():
        try:
            ref: int | str = int(key_ref)
        except ValueError:
            ref = key_ref
        try:
            revoked = await ApiKeyStore(engine).revoke(ref)
        finally:
            await engine.dispose()
        if revoked:
            await _AuditLog(make_engine(_resolve_db_url())).record(
                "auth.api_key_revoked",
                actor_kind="system",
                resource_type="api_key",
                resource_id=str(key_ref),
            )
        return revoked

    revoked = run_coro_sync(_run())
    if revoked:
        click.echo(f"Revoked {key_ref}.")
    else:
        raise click.ClickException(
            f"No active API key matches {key_ref!r} "
            "(already revoked, or unknown id/prefix)."
        )


# --- local dev database -------------------------------------------------------


@click.command("login")
@click.option("--server", default="http://127.0.0.1:8000",
              show_default=True, envvar="INVINCIBLE_SERVER")
@click.option("--config", "config_path",
              type=click.Path(dir_okay=False, path_type=str), default=None,
              help="Where to store the paired credentials "
                   "(default ~/.invincible/config.json).")
def login(server: str, config_path: str | None):
    """Pair this machine with an Invincible server (device flow).

    Prints a URL + short code; approve the request in a browser where you
    are signed in, and this command stores the minted API key locally.
    """
    server = server.rstrip("/")

    async def _run():
        async def _on_code(url: str, code: str) -> None:
            click.echo(f"1. Open:  {url}")
            click.echo(f"2. Code:  {code}  (approve within 10 minutes)")

        return await _pair_device(server, on_code=_on_code)

    try:
        token = run_coro_sync(_run())
    except _DevicePairError as exc:
        raise click.ClickException(f"Device pairing failed: {exc}") from exc
    path = _save_client_config(
        server=server, api_key=token["access_token"], path=config_path)
    click.echo(f"Paired. API key {token['prefix']} saved to {path}")


# --- device pairing plumbing (shared by tests via ASGI transport) ------------

CLIENT_CONFIG_DIRNAME = ".invincible"
CLIENT_CONFIG_FILENAME = "config.json"


class _DevicePairError(Exception):
    """The pairing request failed, was denied, or expired."""


async def _pair_device(base_url: str, *, client=None,
                       sleep=None,
                       on_code=None) -> dict:
    """RFC 8628-style client loop against ``POST /auth/device/code`` and
    ``/auth/device/token``. ``sleep`` and ``client`` are injectable so
    tests run this hermetically against the ASGI app."""
    import httpx

    owns_client = client is None
    http = client or httpx.AsyncClient(base_url=base_url, timeout=30)
    tick = sleep or asyncio.sleep
    try:
        started = await http.post(f"{base_url}/auth/device/code")
        started.raise_for_status()
        payload = started.json()
        device_code = payload["device_code"]
        interval = float(payload.get("interval", 5))
        expires_in = float(payload.get("expires_in", 600))
        verification_uri = payload.get("verification_uri",
                                       f"{base_url}/login")
        user_code = payload["user_code"]
        if on_code is not None:
            result = on_code(verification_uri, user_code)
            if hasattr(result, "__await__"):
                await result

        deadline = time.monotonic() + expires_in
        form = {"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code}
        while True:
            if time.monotonic() >= deadline:
                raise _DevicePairError("the code expired before approval")
            polled = await http.post(f"{base_url}/auth/device/token",
                                     data=form)
            data = polled.json()
            error = data.get("error")
            if polled.status_code == 200 and not error:
                return data
            if error == "authorization_pending":
                await tick(interval)
                continue
            if error == "slow_down":
                interval = max(interval,
                               float(data.get("interval", interval)))
                await tick(interval)
                continue
            raise _DevicePairError(
                data.get("error_description") or error or
                f"HTTP {polled.status_code}")
    finally:
        if owns_client:
            await http.aclose()


def _client_config_path(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    home = os.path.expanduser("~")
    return os.path.join(home, CLIENT_CONFIG_DIRNAME,
                        CLIENT_CONFIG_FILENAME)


def _save_client_config(*, server: str, api_key: str,
                        path: str | None = None) -> str:
    """Persist pairing credentials for later CLI/API use. The raw key sits
    in a user-owned file - same trust level as any local credential."""
    import json

    target = _client_config_path(path)
    directory = os.path.dirname(target)
    os.makedirs(directory, exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        json.dump({"server": server, "api_key": api_key}, handle)
    # best effort on platforms without mode bits (Windows)
    with contextlib.suppress(OSError):
        os.chmod(target, 0o600)
    return target


@click.command("dev-db")
@click.option("--port", default=DEV_DB_PORT, show_default=True, type=int,
              help="Local Postgres port to probe (and to publish when "
                   "starting one via Docker).")
@click.option("--env-file", default=".env", show_default=True,
              help=".env file used by --write-env.")
@click.option("--write-env/--no-write-env", default=False,
              help="Write the resulting INVINCIBLE_DB_URL into the .env "
                   "file.")
def dev_db(port, env_file, write_env):
    """Spin up or verify a local Postgres development database.

    Probes an existing server first (INVINCIBLE_DB_URL, then conventional
    localhost ports), starts the bundled Docker pair when nothing is
    reachable, creates the 'invincible' database if needed, and prints a
    working INVINCIBLE_DB_URL."""
    _load_env_file(env_file)
    try:
        url, notes = _provision_dev_db(port)
    except DevDbError as exc:
        raise click.ClickException(str(exc)) from exc

    for note in notes:
        click.echo(f"dev-db: {note}")
    click.echo(f"Database ready: {_mask_url(url)}")
    click.echo(f"INVINCIBLE_DB_URL={url}")
    click.echo(
        'Add it to your .env (or rerun with --write-env), then run '
        '`invincible db upgrade`.'
    )
    if write_env:
        env_path = os.path.abspath(env_file)
        lines = []
        if os.path.isfile(env_path):
            try:
                with open(env_path, encoding="utf-8") as f:
                    lines = f.read().splitlines(keepends=True)
            except OSError as exc:
                raise click.ClickException(
                    f"Could not read env file {env_path}: {exc}"
                ) from exc
        _apply_env_updates(env_path, lines, {"INVINCIBLE_DB_URL": url})
        click.echo(f"Wrote INVINCIBLE_DB_URL to {env_path}")


# --- database maintenance -----------------------------------------------------


@click.group()
@click.option("--env-file", default=".env", show_default=True,
              help=".env file to load before running (existing process "
                   "environment always wins).")
@click.pass_context
def db(ctx, env_file):
    """Database maintenance: Alembic migrations and legacy SQLite import."""
    _load_env_file(env_file)


@db.command("upgrade")
def db_upgrade():
    """Run Alembic migrations to head against INVINCIBLE_DB_URL.

    Migrations are explicit by design - neither this tool nor `start` ever
    auto-runs them against production without being asked."""
    url = _resolve_db_url()
    heads = migration_heads()
    if not heads:
        raise click.ClickException("Packaged migration scripts unavailable")
    cfg = migrations_config(db_url=url)
    try:
        from alembic import command as alembic_command

        alembic_command.upgrade(cfg, "head")
    except Exception as exc:
        raise click.ClickException(
            f"Migration failed: {str(exc).splitlines()[0][:200]}"
        ) from exc
    click.echo(f"Database upgraded to revision {'/'.join(heads)} "
               f"({_mask_url(url)})")


@db.command("import")
@click.argument("sqlite_path", type=click.Path(exists=True, dir_okay=False))
def db_import_cmd(sqlite_path):
    """Import a legacy Phase <= 15 sessions.db (SQLite) into PostgreSQL.

    One-shot importer covering sessions/turns/messages, facts, and OAuth
    rows; row ids are preserved and identity sequences re-synced. Existing
    target rows are left untouched."""
    url = _resolve_db_url()
    engine = make_engine(url)
    try:
        counts = run_coro_sync(import_legacy_sqlite(engine, sqlite_path))
    except Exception as exc:
        raise click.ClickException(
            f"Import failed: {str(exc).splitlines()[0][:200]}"
        ) from exc
    finally:
        run_coro_sync(engine.dispose())
    for table in sorted(counts):
        click.echo(f"{table}: imported {counts[table]} row(s)")
    total = sum(counts.values())
    click.echo(f"Import complete ({total} row(s) total) into {_mask_url(url)}")


@click.group()
@click.version_option(__version__, "--version", "-V", prog_name="invincible")
def cli():
    """Invincible - multi-provider AI gateway with MCP tool execution."""


cli.add_command(setup)
cli.add_command(start)
cli.add_command(login)
cli.add_command(doctor)
cli.add_command(secret)
cli.add_command(oauth)
cli.add_command(api_key)
cli.add_command(db)
cli.add_command(dev_db)

if __name__ == "__main__":
    cli()
