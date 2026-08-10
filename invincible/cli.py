# invincible/cli.py
"""Click CLI for the Invincible gateway.

Both console scripts (``invincible`` and ``inv``) declared in pyproject.toml
point at the ``cli`` group defined here, so the two commands are identical.
"""
import asyncio
import importlib.resources
import os
import secrets

import click
import uvicorn
from dotenv import load_dotenv

from invincible import __version__
from invincible.core.router import load_providers_config
from invincible.core.session_store import SessionStore

SUPPORTED_ENV_KEYS = (
    "GATEWAY_API_KEY",
    "MCP_SHARED_SECRET",
    "NVIDIA_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "GEMINI_API_KEY",
)
SECRET_ENV_KEYS = ("GATEWAY_API_KEY", "MCP_SHARED_SECRET")


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

    for key in SUPPORTED_ENV_KEYS:
        current = existing.get(key, "")
        if key in SECRET_ENV_KEYS:
            if current and not force:
                continue
            if current:
                new_values[key] = click.prompt(
                    f"{key} (leave empty to keep the existing value)",
                    default=current, hide_input=True, show_default=False,
                )
            else:
                # Never printed; write straight to the env file.
                new_values[key] = secrets.token_urlsafe(32)
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

    output_lines = []
    seen = set()
    for line in lines:
        parsed = _parse_env_line(line)
        if parsed:
            key = parsed[0]
            seen.add(key)
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

    click.echo(f"Configured {env_path}")


def _load_env_file(env_file: str) -> str | None:
    """Load an env file into the process environment without overriding
    existing variables. Returns the absolute path when loaded, else None."""
    env_abs = os.path.abspath(env_file)
    if os.path.isfile(env_abs):
        load_dotenv(dotenv_path=env_abs, override=False)
        return env_abs
    return None


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
def start(host, port, reload, log_level, env_file, config_path, db_path):
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

    try:
        uvicorn.run(
            "invincible.main:app",
            host=host,
            port=port,
            reload=reload,
            log_level=log_level,
        )
    except KeyboardInterrupt:
        click.echo("Shutting down.")


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

    for key in ("GATEWAY_API_KEY", "MCP_SHARED_SECRET"):
        checks.append((f"{key} exists", bool(os.getenv(key)), ""))

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


@click.group()
@click.version_option(__version__, "--version", "-V", prog_name="invincible")
def cli():
    """Invincible - multi-provider AI gateway with MCP tool execution."""


cli.add_command(setup)
cli.add_command(start)
cli.add_command(doctor)

if __name__ == "__main__":
    cli()
