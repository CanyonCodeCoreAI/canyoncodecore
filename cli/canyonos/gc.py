"""
Shared request helpers for the Global Controller container, so the commands
that talk to it don't each restate the same routes, payloads and failure modes.
"""

import json
import urllib.error
import urllib.request

from canyonos.init import load_state

_DEPLOY_CONFLICT = "Run `canyonos stop` to stop the running deploy first."


class GCError(Exception):
    """A failed Global Controller request, carrying a message fit to print."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def _error_detail(e):
    """The server's `error` field, falling back to the raw body when it isn't JSON."""
    body = e.read().decode(errors="replace").strip()
    try:
        return json.loads(body).get("error", body)
    except ValueError:
        return body or f"HTTP {e.code}"


def _request(url, action, data=None, method="GET"):
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise GCError(f"{action} failed: {_error_detail(e)}", code=e.code) from None
    except urllib.error.URLError as e:
        raise GCError(f"Could not reach Global Controller container: {e.reason}") from None


def require_state():
    """Recorded container state, or None after reporting that there is none."""
    try:
        return load_state()
    except FileNotFoundError:
        print("No Global Controller container is running. Run `canyonos deploy` first.")
        return None


def post_deploy(port, config_path=None):
    """Start a deploy inside the container. Raises GCError on failure.

    Omitting config_path lets ventis resolve it against the synced workspace.
    """
    body = json.dumps({"config_path": config_path} if config_path else {}).encode()
    try:
        return _request(f"http://127.0.0.1:{port}/deploy", "Deploy", data=body, method="POST")
    except GCError as e:
        if e.code == 409:
            raise GCError(f"{e}\n{_DEPLOY_CONFLICT}", code=409) from None
        raise


def post_clean(port):
    """Tear down the running deploy: SIGTERMs the in-container `ventis deploy`
    process, whose handler calls GlobalController.stop() and blocks until it
    returns. This is what actually removes the local controller and Redis
    containers a deploy spawned via docker-outside-of-docker.
    """
    return _request(f"http://127.0.0.1:{port}/clean", "Stop", method="POST")


def deploy_status(port):
    """Parsed /status payload, or None if the container is unreachable."""
    url = f"http://127.0.0.1:{port}/status"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read())
    except OSError:
        return None
