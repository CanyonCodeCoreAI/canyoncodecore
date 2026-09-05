"""
Logic for `canyonos status`: reports whether a deploy is currently running,
and if so, where the workflow (and, if up, the dashboard) answer.
"""

from canyonos import ui
from canyonos.constants import WORKFLOW_ROUTE, default_config_path, workflow_api_port
from canyonos.dashboard_stack import _existing_dashboard_port
from canyonos.deploy import workflow_targets
from canyonos.gc import deploy_status, require_state


def run_status():
    state = require_state()
    if state is None:
        return

    status = deploy_status(state["port"])
    if not status or not status.get("running"):
        ui.warn("No deploy is currently running.")
        return

    ui.ok("Deploy is running.")

    # Same resolution `deploy` uses, so both report the address the container
    # actually placed the workflow at and fall back to the configured api_port
    # rather than a guess.
    targets = workflow_targets(state["port"], workflow_api_port(default_config_path()))
    for name, target_host, target_port in targets:
        label = f"Workflow {name}" if name else "Workflow"
        ui.say(f"{label}: {target_host}:{target_port}")
    if not targets:
        ui.hint("No workflow endpoints reported yet.")

    dashboard_port = _existing_dashboard_port()
    if dashboard_port:
        ui.say(f"Dashboard: 127.0.0.1:{dashboard_port}")
    else:
        ui.hint("Dashboard is not running. Run `canyonos serve` to start it.")

    if not targets:
        return

    # The body is splatted into the workflow entrypoint as kwargs, so its keys
    # are that function's parameter names -- `query` for every bundled example,
    # but swap in whatever yours actually takes.
    _, host, port = targets[0]
    ui.blank()
    ui.hint("Query the workflow:")
    ui.say(f"  curl -X POST http://{host}:{port}/{WORKFLOW_ROUTE} \\")
    ui.say('    -H "Content-Type: application/json" \\')
    ui.say("    -d '{\"query\": \"your question here\"}'")
    ui.blank()
    ui.hint("Check a request's result:")
    ui.say(f"  curl http://{host}:{port}/status/<request_id>")
