"""Manage the local CanyonOS dashboard stack."""

from __future__ import annotations

import importlib.resources
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

import yaml

DEFAULT_CONFIG_PATH = "config/global_controller.yaml"
COMPOSE_PROJECT = "canyonos-dashboard"
STACK_VERSION = "v0.1.0-rc.2"
API_IMAGE = f"ghcr.io/canyoncodecoreai/canyonos-api:{STACK_VERSION}"
WEB_IMAGE = f"ghcr.io/canyoncodecoreai/canyonos-web:{STACK_VERSION}"
HOST_GATEWAY = "host.docker.internal"
REDIS_HOST = HOST_GATEWAY
REDIS_PORT = "6379"
ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class ServeResult:
    ok: bool
    phase: str
    message: str
    url: str | None = None
    log_path: str | None = None


class PhaseFailure(Exception):
    def __init__(self, phase: str, message: str, *, had_containers: bool | None = None):
        super().__init__(message)
        self.phase = phase
        self.message = message
        self.had_containers = had_containers


@dataclass(frozen=True)
class DashboardStack:
    database_url: str | None
    state_dir: Path
    project_dir: Path
    web_port: int = 8080

    @property
    def env_path(self) -> Path:
        return self.project_dir / ".env"


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, check=False, text=True)


def _state_dir() -> Path:
    return Path.home() / ".canyonos" / "dashboard"


def _compose_argv(manifest: Path) -> list[str]:
    return [
        "docker",
        "compose",
        "-p",
        COMPOSE_PROJECT,
        "--env-file",
        "./.env",
        "-f",
        str(manifest),
    ]


def _managed_database_url(database_url: str) -> tuple[str, str | None]:
    parsed = urlsplit(database_url)
    if parsed.hostname not in {"localhost", "127.0.0.1"}:
        return database_url, None

    hostname = parsed.hostname
    credentials = ""
    if parsed.username is not None:
        credentials = parsed.username
        if parsed.password is not None:
            credentials = f"{credentials}:{parsed.password}"
        credentials = f"{credentials}@"
    port = f":{parsed.port}" if parsed.port is not None else ""
    rewritten = urlunsplit(
        (parsed.scheme, f"{credentials}{HOST_GATEWAY}{port}", parsed.path, parsed.query, parsed.fragment)
    )
    return rewritten, hostname


def _existing_dashboard_port() -> int | None:
    """The host port an already-running dashboard `web` container owns, if any
    -- so re-running `canyonos serve` reconnects to the same stack instead of
    picking a new port out from under it."""
    try:
        result = _run(["docker", "container", "inspect", "canyonos-dashboard-web-1"])
    except OSError:
        return None
    if result.returncode != 0:
        return None

    try:
        containers = json.loads(result.stdout)
        bindings = containers[0]["NetworkSettings"]["Ports"].get("8080/tcp") or []
    except (IndexError, KeyError, TypeError, json.JSONDecodeError):
        return None

    for binding in bindings:
        if binding.get("HostIp") in {"127.0.0.1", "0.0.0.0", "::"}:
            try:
                return int(binding["HostPort"])
            except (KeyError, TypeError, ValueError):
                continue
    return None


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _find_web_port(start: int = 8080, max_attempts: int = 50) -> int:
    """First free port at or after `start` -- same retry-on-conflict shape as
    init.py's GC port selection, so an unrelated process/container squatting
    on 8080 (e.g. a deployed Workflow's own api_port) doesn't hard-block serve.
    """
    for port in range(start, start + max_attempts):
        if _port_is_free(port):
            return port
    raise PhaseFailure(
        "validate", f"no free port found for the dashboard after {max_attempts} attempts starting at {start}"
    )


def _load_project_config(config_path: str) -> tuple[object, Path]:
    project_root = Path(os.path.abspath(os.path.join(os.path.dirname(config_path), "..")))
    dotenv_path = project_root / ".env"
    if dotenv_path.is_file():
        with dotenv_path.open(encoding="utf-8") as dotenv:
            for line in dotenv:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = _env_value(value)
                if key and key not in os.environ:
                    os.environ[key] = value

    with open(config_path, encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file)
    return _expand_env_value(config), project_root


def _expand_env_value(value: object) -> object:
    if isinstance(value, str):
        return ENV_REFERENCE.sub(lambda match: os.environ.get(match.group(1), match.group(0)), value)
    if isinstance(value, dict):
        return {key: _expand_env_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_env_value(item) for item in value]
    return value


def validate(config_path: str) -> DashboardStack:
    if shutil.which("docker") is None:
        raise PhaseFailure("validate", "docker is not on PATH")

    try:
        if _run(["docker", "info"]).returncode != 0:
            raise PhaseFailure("validate", "docker daemon or socket is unavailable")
        if _run(["docker", "compose", "version"]).returncode != 0:
            raise PhaseFailure("validate", "docker compose is unavailable")
    except OSError:
        raise PhaseFailure("validate", "docker daemon or socket is unavailable")

    try:
        # Keep interpolation consistent with GlobalController._load_config in ventis/controller/global_controller.py.
        config, project_root = _load_project_config(config_path)
    except (OSError, yaml.YAMLError):
        raise PhaseFailure("validate", f"config file is not readable: {config_path}")

    # database.url is optional -- the dashboard works without a database configured
    # (e.g. OTLP-only setups); if present, it still needs to actually be usable.
    database = config.get("database") if isinstance(config, dict) else None
    database_url = database.get("url") if isinstance(database, dict) else None
    if database_url is not None:
        if not isinstance(database_url, str) or not database_url.strip():
            raise PhaseFailure("validate", "database.url must be a non-empty string")
        unresolved = ENV_REFERENCE.search(database_url)
        if unresolved:
            name = unresolved.group(1)
            raise PhaseFailure(
                "validate", f"database.url needs ${{{name}}}, which is not set in the project .env"
            )
        database_url = database_url.strip()

    state_dir = _state_dir()
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        probe_path = state_dir / ".write-probe"
        with open(probe_path, "w", encoding="utf-8") as probe:
            probe.write("")
        probe_path.unlink()
    except OSError:
        raise PhaseFailure("validate", "dashboard state directory is not writable")

    web_port = _existing_dashboard_port() or _find_web_port()

    return DashboardStack(database_url, state_dir, project_root, web_port)


def _env_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _read_existing_secret(env_path: Path) -> str | None:
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    for line in lines:
        key, separator, value = line.partition("=")
        if separator and key == "CANYONOS_JWT_SECRET" and _env_value(value):
            return _env_value(value)
    return None


def _write_private_file(path: Path, contents: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(contents)


def _env_line(key: str, value: str) -> str:
    if " " in value or "#" in value:
        return f'{key}="{value}"\n'
    return f"{key}={value}\n"


def _write_project_env(env_path: Path, managed_env: dict[str, str]) -> None:
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
    except FileNotFoundError:
        lines = []

    managed_keys = set(managed_env)
    replaced: set[str] = set()
    updated_lines: list[str] = []
    for line in lines:
        key, separator, _ = line.partition("=")
        if separator and key in managed_keys:
            if key not in replaced:
                updated_lines.append(_env_line(key, managed_env[key]))
                replaced.add(key)
            continue
        updated_lines.append(line)

    for key, value in managed_env.items():
        if key not in replaced:
            updated_lines.append(_env_line(key, value))

    descriptor, temporary_name = tempfile.mkstemp(prefix=".env.", dir=env_path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            output.writelines(updated_lines)
        os.replace(temporary_path, env_path)
    except Exception:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise


def prepare(stack: DashboardStack) -> tuple[dict[str, str], str]:
    try:
        stack.state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(stack.state_dir, 0o700)
        managed_env = {
            "CANYONOS_JWT_SECRET": _read_existing_secret(stack.env_path) or secrets.token_urlsafe(32),
            "CANYONOS_REDIS_HOST": REDIS_HOST,
            "CANYONOS_REDIS_PORT": REDIS_PORT,
            "CANYONOS_API_IMAGE": API_IMAGE,
            "CANYONOS_WEB_IMAGE": WEB_IMAGE,
            "CANYONOS_WEB_PORT": str(stack.web_port),
        }
        rewritten_host = None
        if stack.database_url is not None:
            managed_database_url, rewritten_host = _managed_database_url(stack.database_url)
            managed_env["CANYONOS_DATABASE_URL"] = managed_database_url
        _write_project_env(stack.env_path, managed_env)
        (stack.state_dir / "stack.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "stack_version": STACK_VERSION,
                    "compose_project": COMPOSE_PROJECT,
                }
            )
            + "\n",
            encoding="utf-8",
        )
    except (OSError, ValueError):
        raise PhaseFailure("prepare", "could not prepare the dashboard state directory")

    message = "dashboard state prepared"
    if rewritten_host:
        message = (
            f"database host {rewritten_host} is reachable from the stack as host.docker.internal"
        )
    return managed_env, message


def _redact_stack_text(text: str, stack: DashboardStack, managed_env: dict[str, str]) -> str:
    secret = managed_env["CANYONOS_JWT_SECRET"]
    redacted = redact_logs(text, secret)
    if stack.database_url is not None:
        redacted = redact_logs(redacted.replace(stack.database_url, "[redacted]"), secret)
    return redacted


def _last_stderr_line(result: subprocess.CompletedProcess[str]) -> str | None:
    return next((line.strip() for line in reversed(result.stderr.splitlines()) if line.strip()), None)


def _command_failure_message(
    message: str,
    result: subprocess.CompletedProcess[str],
    stack: DashboardStack,
    managed_env: dict[str, str],
) -> str:
    detail = _last_stderr_line(result)
    if detail is None:
        return message
    return f"{message}: {_redact_stack_text(detail, stack, managed_env)}"


def pull(
    stack: DashboardStack,
    manifest: Path,
    managed_env: dict[str, str],
    had_containers: bool,
) -> str:
    try:
        result = _run([*_compose_argv(manifest), "pull"])
    except OSError:
        raise PhaseFailure("pull", "could not run docker compose pull", had_containers=had_containers)
    if result.returncode != 0:
        raise PhaseFailure(
            "pull",
            _command_failure_message("docker compose pull failed", result, stack, managed_env),
            had_containers=had_containers,
        )
    return "dashboard images pulled"


def _project_has_running_containers(stack: DashboardStack, manifest: Path) -> bool:
    try:
        result = _run([*_compose_argv(manifest), "ps", "-q"])
    except OSError:
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def start(stack: DashboardStack, manifest: Path, managed_env: dict[str, str]) -> bool:
    had_containers = _project_has_running_containers(stack, manifest)
    try:
        result = _run(
            [*_compose_argv(manifest), "up", "-d", "--wait", "--wait-timeout", "180"]
        )
    except OSError:
        raise PhaseFailure("start", "could not run docker compose up", had_containers=had_containers)
    if result.returncode != 0:
        raise PhaseFailure(
            "start",
            _command_failure_message("docker compose up failed", result, stack, managed_env),
            had_containers=had_containers,
        )
    return had_containers


def verify(port: int) -> str:
    dashboard_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    endpoints = (f"{dashboard_url}/healthz", f"{dashboard_url}/api/healthz")
    while time.monotonic() < deadline:
        healthy = True
        for endpoint in endpoints:
            try:
                response = urllib.request.urlopen(endpoint, timeout=5)
                try:
                    status = response.status
                finally:
                    response.close()
            except (OSError, urllib.error.URLError):
                healthy = False
                break
            if status != 200:
                healthy = False
                break
        if healthy:
            return dashboard_url
        if time.monotonic() < deadline:
            time.sleep(1)
    raise PhaseFailure("verify", "dashboard health checks did not return 200 within 30 seconds")


def redact_logs(logs: str, jwt_secret: str) -> str:
    redacted = logs.replace(jwt_secret, "[redacted]")
    return re.sub(r"://[^/\s@]+@", "://[redacted]@", redacted)


def _capture_failure_logs(stack: DashboardStack, manifest: Path, managed_env: dict[str, str]) -> Path:
    try:
        result = _run([*_compose_argv(manifest), "logs", "--no-color", "--tail", "200"])
        logs = f"{result.stdout}\n{result.stderr}"
    except OSError:
        logs = "Unable to collect docker compose logs."

    log_dir = stack.state_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = log_dir / f"serve-{timestamp}.log"
    _write_private_file(
        log_path,
        _redact_stack_text(logs, stack, managed_env),
    )
    return log_path


def _cleanup(stack: DashboardStack, manifest: Path) -> None:
    try:
        _run([*_compose_argv(manifest), "down"])
    except OSError:
        return


def run_dashboard(
    config_path: str = DEFAULT_CONFIG_PATH,
    phase_reporter: Callable[[str, str], None] | None = None,
) -> ServeResult:
    def report(result: ServeResult) -> None:
        if phase_reporter is not None:
            phase_reporter(result.phase, result.message)

    stack: DashboardStack | None = None
    managed_env: dict[str, str] | None = None
    manifest: Path | None = None
    had_containers = False
    with ExitStack() as resources:
        try:
            stack = validate(config_path)
            report(ServeResult(True, "validate", "dashboard prerequisites validated"))

            managed_env, prepare_message = prepare(stack)
            report(ServeResult(True, "prepare", prepare_message))

            manifest_resource = importlib.resources.files("canyonos").joinpath("dashboard.compose.yml")
            manifest = resources.enter_context(importlib.resources.as_file(manifest_resource))
            had_containers_before_pull = _project_has_running_containers(stack, manifest)
            pull_message = pull(stack, manifest, managed_env, had_containers_before_pull)
            report(ServeResult(True, "pull", pull_message))

            had_containers = start(stack, manifest, managed_env)
            report(ServeResult(True, "start", "dashboard stack started"))

            url = verify(stack.web_port)
            report(ServeResult(True, "verify", "dashboard health checks passed", url))
            return ServeResult(True, "verify", "dashboard health checks passed", url)
        except PhaseFailure as failure:
            log_path = None
            if failure.phase in {"pull", "start", "verify"} and stack and managed_env and manifest:
                log_path = _capture_failure_logs(stack, manifest, managed_env)
                if not (failure.had_containers if failure.had_containers is not None else had_containers):
                    _cleanup(stack, manifest)
            return ServeResult(
                False, failure.phase, failure.message, None, str(log_path) if log_path else None
            )
