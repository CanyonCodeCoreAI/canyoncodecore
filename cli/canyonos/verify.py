"""
The two verification passes behind `canyonos test`.

`verify_build_artifact` checks the `.car/` tree a `canyonos build` produced,
before any container is started: the layout, the porting skill's own validator,
and whether the sources have moved on since the port was taken.

`verify_runtime` checks a running local deploy against what the config declared
-- every image built, every replica up -- because the controller logs a warning
and carries on when an agent never becomes healthy, so a workflow that answers
is not on its own proof that the deploy is complete.
"""

import hashlib
import json
import os
import subprocess
import sys

import yaml
from rich.table import Table

from canyonos import gc, ui
from canyonos.build import AGENTS, install_skill
from canyonos.constants import DEFAULT_API_PORT
from canyonos.init import STATE_DIR
from canyonos.theme import GREEN

ARTIFACT_DIR = ".car"
SOURCE_DIR = "app"
CONFIG_REL = "config/global_controller.yaml"
PORTING_STATE_REL = "config/.porting-state.json"

VALIDATOR_NAME = "validate.py"
SKILL_CACHE_DIR = os.path.join(STATE_DIR, "skill")

# These two rules decide their verdict by importing `canyonos` and probing it for
# env-file injection and editable-install support. The runtime lives in the
# Global Controller image, not on the host running this CLI, so the probe always
# comes back empty here and the rules report a failure that isn't one.
CAPABILITY_GATED_CHECKS = frozenset({"V030", "V031"})

RUNTIME_PREFIX = "canyonos-local-"


class VerificationError(Exception):
    """A check that should end the run, carrying a message fit to print."""


# ------------------------------------------------------------------ #
#  Build artifact                                                     #
# ------------------------------------------------------------------ #


def _find_validator(project_root):
    """Path to the porting skill's validate.py, fetching the skill if needed."""
    for spec in AGENTS.values():
        for skill_dir in spec["skill_dirs"].values():
            if not os.path.isabs(skill_dir):
                skill_dir = os.path.join(project_root, skill_dir)
            candidate = os.path.join(skill_dir, VALIDATOR_NAME)
            if os.path.isfile(candidate):
                return candidate

    cached = os.path.join(SKILL_CACHE_DIR, VALIDATOR_NAME)
    if os.path.isfile(cached):
        return cached
    if install_skill(SKILL_CACHE_DIR) and os.path.isfile(cached):
        return cached
    return None


def _run_validator(validator, artifact_dir):
    """The validator's parsed --json report, or None if it produced no report."""
    result = subprocess.run(
        [sys.executable, validator, artifact_dir, "-c", CONFIG_REL, "--json"],
        capture_output=True,
        text=True,
    )
    try:
        return json.loads(result.stdout)
    except ValueError:
        detail = (result.stderr or result.stdout).strip().splitlines()
        ui.warn(f"  The porting validator did not run: {detail[-1] if detail else 'no output'}")
        return None


def _drop_unprobeable(report):
    """Remove the rules that can only be judged with `canyonos` importable.

    Their verdict without it is not merely uncertain, it is wrong: V030 reports
    that the runtime never reads `env_file` when the container's runtime does.
    """
    if report.get("capabilities", {}).get("canyonos_core"):
        return 0

    kept = []
    dropped = 0
    for finding in report.get("findings") or []:
        if finding["check"] in CAPABILITY_GATED_CHECKS:
            if finding["level"] == "ERROR":
                report["errors"] = max(report.get("errors", 0) - 1, 0)
            elif finding["level"] == "WARN":
                report["warnings"] = max(report.get("warnings", 0) - 1, 0)
            dropped += 1
            continue
        kept.append(finding)
    report["findings"] = kept
    return dropped


_LEVEL_EMITTER = {"ERROR": ui.fail, "WARN": ui.warn}


def _report_findings(findings):
    for finding in sorted(findings, key=lambda f: (f["level"] != "ERROR", f["check"])):
        where = finding.get("path") or ""
        if where and finding.get("line"):
            where = f"{where}:{finding['line']}"
        parts = [finding["check"], where, finding["summary"]]
        line = "  ".join(part for part in parts if part)
        _LEVEL_EMITTER.get(finding["level"], ui.hint)(f"  {line}")


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _stale_sources(project_root, artifact_dir):
    """Recorded sources that changed or vanished since the port was taken."""
    try:
        with open(os.path.join(artifact_dir, PORTING_STATE_REL)) as f:
            state = json.load(f)
    except (OSError, ValueError):
        return []

    stale = []
    for relative, expected in (state.get("source_files") or {}).items():
        # The skill's own files are recorded alongside the project's; a newer
        # skill would otherwise read as the application having changed.
        if relative.startswith(".claude/"):
            continue
        path = os.path.join(project_root, relative)
        if not os.path.isfile(path) or _sha256(path) != expected:
            stale.append(relative)
    return sorted(stale)


def verify_build_artifact(project_root="."):
    """Check the `.car/` tree. Raises VerificationError if it can't be deployed."""
    artifact_dir = os.path.join(project_root, ARTIFACT_DIR)
    config_path = os.path.join(artifact_dir, CONFIG_REL)

    if not os.path.isfile(config_path) or not os.path.isdir(
        os.path.join(artifact_dir, SOURCE_DIR)
    ):
        raise VerificationError(
            f"No `{ARTIFACT_DIR}/` artifact here (expected {CONFIG_REL} beside "
            f"{SOURCE_DIR}/). Run `canyonos build` first."
        )
    ui.ok(f"{ARTIFACT_DIR}/ layout (config/ + {SOURCE_DIR}/)")

    summary = {"errors": 0, "warnings": 0, "findings": [], "stale": []}

    validator = _find_validator(project_root)
    if validator is None:
        ui.warn("Could not fetch the porting validator; skipping artifact checks.")
        ui.hint("  The deploy below still runs -- `canyonos doctor` checks the fetch path.")
    else:
        report = _run_validator(validator, os.path.abspath(artifact_dir))
        if report is not None:
            skipped = _drop_unprobeable(report)
            summary.update(
                errors=report.get("errors", 0),
                warnings=report.get("warnings", 0),
                findings=report.get("findings", []),
            )
            counts = f"{summary['errors']} error(s), {summary['warnings']} warning(s)"
            (ui.fail if summary["errors"] else ui.ok)(f"porting validator: {counts}")
            _report_findings(summary["findings"])
            if skipped:
                ui.hint(f"  {skipped} rule(s) need the canyonos runtime to judge and were skipped")

    summary["stale"] = _stale_sources(project_root, artifact_dir)
    for relative in summary["stale"]:
        ui.warn(f"  source changed since the port: {relative}")
    if summary["stale"]:
        ui.hint("  -> re-run `canyonos build` to bring the artifact back in step")

    if summary["errors"]:
        raise VerificationError(
            f"The build artifact has {summary['errors']} validation error(s); fix them "
            "or re-run `canyonos build`."
        )
    return summary


# ------------------------------------------------------------------ #
#  Runtime                                                            #
# ------------------------------------------------------------------ #


def _built_images():
    result = subprocess.run(
        ["docker", "images", "--format", "{{.Repository}}"], capture_output=True, text=True
    )
    return set(result.stdout.split())


def _running_containers():
    result = subprocess.run(
        ["docker", "ps", "--filter", f"name={RUNTIME_PREFIX}", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
    )
    return result.stdout.split()


def _runtime_table(rows):
    table = Table(border_style=GREEN, header_style=f"bold {GREEN}", title_style=f"bold {GREEN}")
    for column in ("Agent", "Image", "Replicas", "Endpoint"):
        table.add_column(column)
    for row in rows:
        replicas = f"{row['running']}/{row['expected']}"
        style = "" if row["ok"] else "bold red"
        table.add_row(
            row["name"],
            row["image"] if row["image_built"] else f"{row['image']} (missing)",
            replicas,
            row["endpoint"] or "-",
            style=style,
        )
    return table


def verify_runtime(config_path, gc_port):
    """Check the running deploy against the config. Raises VerificationError on a gap."""
    with open(config_path) as f:
        config = yaml.safe_load(f) or {}

    images = _built_images()
    containers = _running_containers()
    endpoints = {
        endpoint.get("name"): f"{endpoint['host']}:{endpoint['port']}"
        for endpoint in gc.workflow_endpoints(gc_port)
        if endpoint.get("host") and endpoint.get("port")
    }

    rows = []
    problems = []
    for agent in config.get("agents") or []:
        name = agent.get("name")
        if not name:
            continue
        # Image and container names the local provider derives from the agent name.
        image = f"canyonos-{name.lower()}"
        expected = int(agent.get("replicas", 1) or 1)
        running = sum(1 for c in containers if c.startswith(f"{RUNTIME_PREFIX}{name.lower()}-"))
        image_built = image in images

        if not image_built:
            problems.append(f"{name}: image {image} was never built")
        elif running < expected:
            problems.append(f"{name}: {running} of {expected} replicas running")

        endpoint = endpoints.get(name)
        if endpoint is None and agent.get("type") == "workflow":
            # The container only reports endpoints it has instance records for;
            # locally the published port is the one the config asked for.
            endpoint = f"127.0.0.1:{agent.get('api_port', DEFAULT_API_PORT)}"

        rows.append(
            {
                "name": name,
                "image": image,
                "image_built": image_built,
                "expected": expected,
                "running": running,
                "endpoint": endpoint,
                "ok": image_built and running >= expected,
            }
        )

    ui.panel(_runtime_table(rows))
    if problems:
        raise VerificationError("The deploy is incomplete -- " + "; ".join(problems))
    return {"agents": rows}
