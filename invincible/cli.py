# invincible/cli.py
"""Click CLI for the Invincible gateway.

Both console scripts (``invincible`` and ``inv``) declared in pyproject.toml
point at the ``cli`` group defined here, so the two commands are identical.
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

import click
import uvicorn
from dotenv import load_dotenv

from invincible import __version__
from invincible.core.config import load_providers_config
from invincible.core.oauth_store import OAuthStore
from invincible.core.session_store import SessionStore

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
DB_PATH_HELP = (
    "Session database file path (default: INVINCIBLE_DB_PATH or ./sessions.db)."
)


def _generate_secret() -> str:
    """Generate a new cryptographically random secret (never echoed)."""
    return secrets.token_urlsafe(32)


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
@click.option("--db-path", type=click.Path(dir_okay=False), default=None,
              help="Session database file path.")
@click.option("--tunnel/--no-tunnel", default=True,
              help="Start a Cloudflare tunnel alongside the server.")
@click.option("--tunnel-name", default=None,
              help="Cloudflare tunnel name for `cloudflared tunnel run` "
                   f"(default: {TUNNEL_NAME_ENV_KEY} env var or "
                   f"'{DEFAULT_TUNNEL_NAME}').")
def start(host, port, reload, log_level, env_file, config_path, db_path,
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

    if db_path:
        os.environ["INVINCIBLE_DB_PATH"] = os.path.abspath(db_path)

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


async def _check_session_db(db_path):
    """Try to open and initialize the session database like the app does at
    startup (creating the file if missing). Returns (ok, note)."""
    store = SessionStore(db_path=db_path)
    try:
        await store.init()
    except Exception as exc:
        return False, f"cannot open {store.db_path}: {exc}"
    await store.close()
    return True, store.db_path


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

    ok, note = asyncio.run(_check_session_db(None))
    checks.append(("session database accessible", ok, note))

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


# --- oauth administration ---


def _format_ts(timestamp: float) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp))
    except (OverflowError, OSError, ValueError):
        return "?"


@click.group()
def oauth():
    """Inspect and revoke OAuth (MCP bearer-token) grants."""


@oauth.command("list")
@click.option("--db-path", type=click.Path(dir_okay=False), default=None,
              help=DB_PATH_HELP)
def oauth_list(db_path):
    """List registered OAuth clients and their active grants."""
    store = OAuthStore(db_path=db_path)

    async def _run():
        await store.init()
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
        await store.close()

    asyncio.run(_run())


@oauth.command("revoke")
@click.argument("client_id")
@click.option("--db-path", type=click.Path(dir_okay=False), default=None,
              help=DB_PATH_HELP)
def oauth_revoke(client_id, db_path):
    """Revoke every access/refresh token issued to a client.

    New connections from that client will fail until it is re-registered
    and approved again in the browser.

    Client ids are random URL-safe strings that may start with '-'. Because
    Click parses such an id as an option, pass it after a '--' separator:
    invincible oauth revoke -- <client_id>
    """
    store = OAuthStore(db_path=db_path)

    async def _run():
        await store.init()
        client = await store.get_client(client_id)
        if client is None:
            raise click.ClickException(f"Unknown client id: {client_id}")
        count = await store.revoke_client_tokens(client_id)
        click.echo(f"Revoked {count} token(s) for client {client_id}.")
        await store.close()

    try:
        asyncio.run(_run())
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(f"Could not revoke tokens: {exc}") from exc


@oauth.command("test-client")
@click.option("--redirect-uri", default="http://127.0.0.1:9999/callback",
              show_default=True,
              help="Loopback redirect URI to register for the test client.")
@click.option("--db-path", type=click.Path(dir_okay=False), default=None,
              help=DB_PATH_HELP)
def oauth_test_client(redirect_uri, db_path):
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

    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode("ascii")

    async def _run():
        store = OAuthStore(db_path=db_path)
        await store.init()
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
            await store.close()

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
        asyncio.run(_run())
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


@click.group()
@click.version_option(__version__, "--version", "-V", prog_name="invincible")
def cli():
    """Invincible - multi-provider AI gateway with MCP tool execution."""


cli.add_command(setup)
cli.add_command(start)
cli.add_command(doctor)
cli.add_command(secret)
cli.add_command(oauth)

if __name__ == "__main__":
    cli()
