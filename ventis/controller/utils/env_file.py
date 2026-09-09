"""
Pass user secrets (API keys and friends) into agent containers.

Two scopes, never mixed: `env_file` in the config (self-hosted), and
`DEFAULT_SECRETS_FILE` (managed -- the project's `.env` was never uploaded
there). Below `resolve_env_file` both are just a path: containers on this
machine read it directly, remote ones get a short-lived 0600 copy, and either
way it reaches Docker as `--env-file`.
"""

import logging
import os
import re
from contextlib import contextmanager

logger = logging.getLogger(__name__)

REMOTE_ENV_DIR = "/tmp"
_UNSAFE_PATH_CHARS = re.compile(r"[^A-Za-z0-9_.-]")

# Where a managed deployment leaves the user's secrets. /var/run is tmpfs, so
# the file dies with the host instead of persisting on disk.
DEFAULT_SECRETS_FILE = "/var/run/ventis/secrets.env"


def platform_secrets_file():
    """
    The path a managed deployment leaves the user's secrets at.

    `VENTIS_SECRETS_FILE` overrides it for tests and for deployments that
    cannot write under /var/run. Nothing sets it in normal operation.
    """
    return os.environ.get("VENTIS_SECRETS_FILE", DEFAULT_SECRETS_FILE)


def resolve_env_file(config, base_dir=None):
    """
    Return the absolute path of the env file to hand containers, or None.

    A file at `platform_secrets_file()` wins over `env_file`. A convention
    beating an explicit setting only makes sense because the two describe
    different machines: `env_file: .env` describes the user's own, and
    carrying that config to a managed deployment does not make the `.env`
    exist there. So it warns rather than raising.

    The platform must create that file unconditionally, empty when no secrets
    are configured -- its presence means "managed", not "has secrets".

    Raises:
        ValueError: the file is configured but unusable. Deploy should fail
            here rather than start a fleet of agents with no API keys.
    """
    platform_path = platform_secrets_file()
    if os.path.isfile(platform_path):
        if not os.access(platform_path, os.R_OK):
            raise ValueError(f"platform secrets file is not readable: {platform_path}")
        if config.get("env_file"):
            logger.warning(
                "env_file %r is ignored in a managed deployment: secrets come "
                "from the platform. Configure them on the platform instead.",
                config["env_file"],
            )
        return platform_path

    return _resolve_project_env_file(config, base_dir)


def _resolve_project_env_file(config, base_dir):
    """
    Resolve the `env_file` a self-hosted deployment points at, or None.

    Relative paths resolve against `base_dir` (default: the current working
    directory), matching how `entrypoint` and `workflow_file` are resolved.
    """
    raw = config.get("env_file")
    if not raw:
        return None

    path = os.path.expanduser(str(raw))
    if not os.path.isabs(path):
        path = os.path.join(base_dir or os.getcwd(), path)
    path = os.path.abspath(path)

    if not os.path.exists(path):
        raise ValueError(f"env_file does not exist: {path} (from env_file: {raw})")
    if not os.path.isfile(path):
        raise ValueError(f"env_file is not a file: {path} (from env_file: {raw})")
    if not os.access(path, os.R_OK):
        raise ValueError(f"env_file is not readable: {path}")
    return path


def remote_env_path(container_name):
    """
    Where a remote host holds this container's copy of the env file.

    The name is scrubbed down to a shell-safe alphabet. This path is
    interpolated into remote commands that `_run_cmd` joins with spaces and
    hands to a shell unquoted, so a container name carrying a space would
    split the cleanup `rm` into two harmless arguments -- it would exit 0
    while the secrets stayed on the host, with nothing in the log to say so.
    """
    safe_name = _UNSAFE_PATH_CHARS.sub("-", container_name)
    return f"{REMOTE_ENV_DIR}/ventis-env-{safe_name}"


@contextmanager
def env_file_args(controller, host, user, container_name, is_local):
    """
    Yield the `docker run` flags that hand the user's env file to a container.

    A container on this machine reads the original file. A container on a
    remote host gets a 0600 copy, deleted as soon as the `with` body ends --
    success or failure, since by then the container holds the variables
    itself. Keep that body tight around `docker run` so the copy is never
    on the host longer than it has to be.

    Yields an empty list when no env file is configured, and when the one
    configured is empty.
    """
    env_file_path = getattr(controller, "env_file_path", None)
    if not env_file_path or _has_no_variables(env_file_path):
        yield []
        return

    if is_local:
        yield ["--env-file", env_file_path]
        return

    remote_path = remote_env_path(container_name)
    controller._push_file(env_file_path, remote_path, host, user=user)
    try:
        yield ["--env-file", remote_path]
    finally:
        _remove_remote_copy(controller, remote_path, host, user)


def _has_no_variables(path):
    """
    An empty platform file is routine, so skip it and spare a remote host a
    copy pushed and deleted per container. A file that vanished since
    resolution is not "empty" -- let it through so Docker reports it.
    """
    try:
        return os.path.getsize(path) == 0
    except OSError:
        return False


def _remove_remote_copy(controller, remote_path, host, user):
    """Delete a remote copy. Best effort -- never masks the caller's error."""
    try:
        result = controller._run_cmd(["rm", "-f", remote_path], host, user=user)
        if getattr(result, "returncode", 0) != 0:
            logger.warning("Failed to delete env file copy %s on %s", remote_path, host)
    except Exception as e:
        logger.warning(
            "Failed to delete env file copy %s on %s: %s", remote_path, host, e
        )
