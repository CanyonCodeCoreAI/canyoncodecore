"""Bounded Docker command execution for the CanyonOS CLI."""

import subprocess
import time

DOCKER_QUICK_TIMEOUT = 15
DOCKER_CLEANUP_TIMEOUT = 60
DOCKER_RUN_TIMEOUT = 120
DOCKER_PULL_TIMEOUT = 600
DOCKER_CLEANUP_RETRIES = 1
DOCKER_RETRY_DELAY = 0.5


def run_docker(argv, *, timeout, action, check=False):
    """Run Docker with a bounded wait and a user-facing failure message."""
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        raise RuntimeError("Docker is not installed or is not available on PATH.") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{action} timed out after {timeout}s.") from None
    except OSError as e:
        raise RuntimeError(f"{action} could not run: {e}") from None

    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"{action} failed: {detail or f'exit code {result.returncode}'}")
    return result


def cleanup_docker(argv, *, action, missing_text=None, timeout=DOCKER_CLEANUP_TIMEOUT):
    """Run a cleanup command, retrying once and returning its final result.

    A missing resource is already clean. Other failures are returned so callers
    can continue cleaning independent resources and report them together.
    """
    last_error = None
    for attempt in range(DOCKER_CLEANUP_RETRIES + 1):
        try:
            result = run_docker(argv, timeout=timeout, action=action)
        except RuntimeError as e:
            last_error = str(e)
        else:
            detail = (result.stderr or result.stdout or "").strip()
            if result.returncode == 0 or (
                missing_text and missing_text.casefold() in detail.casefold()
            ):
                return None
            last_error = f"{action} failed: {detail or f'exit code {result.returncode}'}"

        if attempt < DOCKER_CLEANUP_RETRIES:
            time.sleep(DOCKER_RETRY_DELAY)
    return last_error
