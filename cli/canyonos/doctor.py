"""
Logic for `canyonos doctor`: a simple checklist of environment checks
(Docker installed/running, Compose available). Each check just reports
pass/fail plus a suggested fix -- nothing here attempts to auto-fix anything.
"""

import shutil
import subprocess

from canyonos.build import AGENTS
from canyonos.init import docker_start_command


def _docker_installed():
    return shutil.which("docker") is not None


def _docker_daemon_running():
    result = subprocess.run(["docker", "info"], capture_output=True)
    return result.returncode == 0


def _compose_available():
    result = subprocess.run(["docker", "compose", "version"], capture_output=True)
    return result.returncode == 0


def _git_available():
    return shutil.which("git") is not None


def _docker_daemon_fix():
    """Names the command for the active docker context, since `canyonos deploy`
    would run exactly that itself."""
    command = docker_start_command()
    if command:
        return f"run `{' '.join(command)}` -- or just run `canyonos deploy`, which starts it for you"
    return "start your Docker runtime (on Linux: `sudo systemctl start docker`)"


def _coding_agent_available():
    return any(shutil.which(spec["cli"]) for spec in AGENTS.values())


def _checks():
    """Built fresh on each call (not a module-level constant) so tests can
    patch the individual `_check_*` functions by name and have it take effect.
    """
    return [
        (
            "Docker installed",
            _docker_installed,
            "install Docker: https://docs.docker.com/get-docker/",
        ),
        (
            "Docker daemon running",
            _docker_daemon_running,
            _docker_daemon_fix(),
        ),
        (
            "Docker Compose available",
            _compose_available,
            "update Docker to a version that includes Compose v2 (needed for `canyonos serve`)",
        ),
        (
            "git available",
            _git_available,
            "install git (`canyonos build` fetches the porting skill with it; "
            "without git it falls back to a full-repo tarball download)",
        ),
        (
            "Coding agent available",
            _coding_agent_available,
            "install one of "
            + " or ".join(spec["label"] for spec in AGENTS.values())
            + " (`canyonos build` runs the port through it)",
        ),
    ]


def run_doctor():
    """Run every check, print a pass/fail checklist, and return True iff all passed."""
    all_ok = True
    for label, check, fix in _checks():
        try:
            passed = bool(check())
        except OSError as e:
            passed = False
            fix = f"{fix} (error: {e})"

        print(f"{'✓' if passed else '✗'} {label}")
        if not passed:
            print(f"  -> {fix}")
            all_ok = False

    return all_ok
