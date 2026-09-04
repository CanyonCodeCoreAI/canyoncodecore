"""
Logic for `canyonos deploy`: copy the project into the container's /workspace
volume (via `canyonos sync`), then tell the Global Controller container to
build and deploy it. The container's `ventis deploy` handles both the build
(stubs, protos, Docker images) and the launch -- the CLI just ships files,
triggers it, and streams the logs.

Once the deploy's logs report the workflow is actually up, `canyonos serve`
is kicked off automatically so the local dashboard is ready without an extra
manual step.
"""

import subprocess

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from canyonos.constants import (
    WORKFLOW_ROUTE,
    default_config_path,
    workflow_api_port,
    workspace_relative,
)
from canyonos.gc import GCError, post_deploy
from canyonos.theme import GREEN, WHITE
from canyonos.init import load_state, run_init
from canyonos.serve import run_serve
from canyonos.sync import run_sync

# Logged exactly once by GlobalController.run(), right after `_wait_for_healthy()`
# returns -- the signal that the workflow finished coming up and entered its
# steady-state polling loop.
_WORKFLOW_UP_MARKER = "Global controller started, polling every"


def run_deploy(config_path=None, serve=True):
    # Left as None when unset: ventis resolves the artifact layout itself.
    if config_path is not None:
        config_path = workspace_relative(config_path)
        if config_path is None:
            print("Config must be inside the project directory being synced.")
            return

    run_init()

    # Copy the current project into the container before building/deploying.
    if not run_sync():
        return

    state = load_state()

    # Read for display only -- ventis resolves the path it actually deploys.
    api_port = workflow_api_port(config_path or default_config_path())

    try:
        post_deploy(state["port"], config_path)
        _stream_logs_and_autoserve(state["container_id"], api_port, serve=serve)
    except GCError as e:
        print(e)


def print_workflow_endpoint(console, api_port):
    """The one thing you need after a deploy: where to send requests.

    Printed at the workflow-up marker and again on exit, because `deploy` keeps
    tailing logs afterwards and would otherwise scroll it out of sight.
    """
    if api_port is None:
        return

    url = f"http://127.0.0.1:{api_port}/{WORKFLOW_ROUTE}"
    body = Text.assemble(
        ("POST   ", "dim"),
        (url, f"bold {GREEN}"),
        ("\nbody   ", "dim"),
        ('{"query": "your question here"}', WHITE),
        ("\npoll   ", "dim"),
        (f"http://127.0.0.1:{api_port}/status/<request_id>", WHITE),
    )
    console.print()
    console.print(
        Panel(
            body,
            title=f"[bold {GREEN}]Workflow is live[/]",
            title_align="left",
            border_style=GREEN,
            padding=(1, 4),
        )
    )
    console.print()


def _stream_logs_and_autoserve(container_id, api_port, serve=True):
    """Tail the GC container's logs, and once they show the workflow is up,
    print where to reach it -- plus, unless disabled via `serve=False`, launch
    `canyonos serve`. Log tailing continues afterwards.
    """
    console = Console()
    process = subprocess.Popen(
        ["docker", "logs", "-f", container_id],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    served = not serve
    workflow_up = False
    try:
        for line in process.stdout:
            print(line, end="")
            if not workflow_up and _WORKFLOW_UP_MARKER in line:
                workflow_up = True
                print_workflow_endpoint(console, api_port)
                if not served:
                    served = True
                    print("Starting the local dashboard (canyonos serve)...")
                    try:
                        run_serve()
                    except Exception as e:
                        print(f"Could not start the dashboard automatically: {e}")
                        print("Run `canyonos serve` manually to view it.")
    except KeyboardInterrupt:
        print("\nStopped monitoring log stream. Run `canyonos stop` to stop the deploy.")
        print("To resubscribe to log stream run `canyonos logs`.")
        if workflow_up:
            print_workflow_endpoint(console, api_port)
    finally:
        if process.poll() is None:
            process.terminate()
