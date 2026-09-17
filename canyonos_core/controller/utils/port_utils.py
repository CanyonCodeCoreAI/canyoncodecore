"""Standard port-selection and conflict-detection helpers.

One rule for the whole codebase:
  - Internal service ports (the Global Controller, its Redis, the dashboard's
    web/API containers) preflight-and-hop to the next free port on a conflict.
  - Only a user-declared `api_port` fails fast, because external clients read it
    back statically and must not be silently moved.

A port conflict surfaces in one of two ways, depending on where the caller runs:
  - On the host (the CLI): probe with a real socket bind BEFORE launching
    anything, via `is_port_free()` / `find_free_port()`.
  - Inside a container that publishes to the host (the GC launching Redis or
    agent replicas): a bind in the container's own network namespace cannot see
    a host-published conflict, so the only reliable signal is Docker's stderr --
    use `is_port_conflict()` on the `docker run` output and hop.

NOTE: this module is intentionally dependency-free (stdlib `socket` only) and is
duplicated as a twin in the other build artifact:

    cli/canyonos/port_utils.py  <->  canyonos_core/controller/utils/port_utils.py

The CLI and the canyonos_core Docker image ship separately and cannot share an
import, so keep the two copies byte-for-byte identical.
"""

from __future__ import annotations

import socket

# Docker emits the first message for a container-vs-container port clash, and the
# second when a non-container listener (a stray process, an ssh tunnel, a manual
# `docker run -p`) already holds the port.
PORT_CONFLICT_MARKERS = ("port is already allocated", "address already in use")

DEFAULT_MAX_PORT_ATTEMPTS = 50


def is_port_conflict(stderr: str | None) -> bool:
    """True if `docker run` stderr indicates the published host port was taken."""
    text = stderr or ""
    return any(marker in text for marker in PORT_CONFLICT_MARKERS)


def is_port_free(port: int, host: str = "127.0.0.1") -> bool:
    """True if `port` can be bound on `host` right now.

    Host-side check only: a bind here cannot detect a port that Docker has
    published from inside a *different* network namespace -- container launchers
    must use `is_port_conflict()` on the docker output instead.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def find_free_port(
    start: int,
    max_attempts: int = DEFAULT_MAX_PORT_ATTEMPTS,
    host: str = "127.0.0.1",
) -> int:
    """First bindable port at or after `start`, scanning up to `max_attempts`."""
    for port in range(start, start + max_attempts):
        if is_port_free(port, host):
            return port
    raise RuntimeError(
        f"no free port found after {max_attempts} attempts starting at {start}"
    )
