"""
The verification pass behind `canyonos test`.

`verify_runtime` checks a running local deploy against what the config declared
-- every image built, every replica up -- because the controller logs a warning
and carries on when an agent never becomes healthy, so a workflow that answers
is not on its own proof that the deploy is complete.

This file will also need lots of iteration based on what is needed, will expect it to change alot
"""

import os
import subprocess

import yaml
from rich.table import Table

from canyonos import gc, ui
from canyonos.constants import DEFAULT_API_PORT
from canyonos.theme import GREEN

RUNTIME_PREFIX = "canyonos-"


def _replica_prefix(agent_name):
    """canyonos-<namespace->-<agent>-, matching the namespaced names
    `canyonos_core.controller.utils.container_names` gives replica containers."""
    namespace = os.environ.get("CANYONOS_NAMESPACE")
    ns_part = f"{namespace}-" if namespace else ""
    return f"{RUNTIME_PREFIX}{ns_part}{agent_name.lower()}-"


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
    """Check the running deploy against the config. Raises RuntimeError on a gap."""
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
        running = sum(1 for c in containers if c.startswith(_replica_prefix(name)))
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
        raise RuntimeError("The deploy is incomplete -- " + "; ".join(problems))
    return {"agents": rows}
