"""CLI output for the local dashboard stack."""

from canyonos import ui
from .dashboard_stack import ServeResult, run_dashboard


def serve_dashboard() -> ServeResult:
    """Bring the dashboard up, reporting progress. Returns the stack's result."""
    # Phases drive the spinner while the stack comes up; the trace itself is
    # only printed when something fails and the user needs to see how far it got.
    trace = []

    with ui.status("Starting the dashboard...") as spinner:
        def report(phase: str, message: str) -> None:
            trace.append((phase, message))
            spinner.update(message)

        result = run_dashboard(report)

    if result.ok:
        return result

    for phase, message in trace:
        ui.hint(f"{phase}: {message}")
    ui.fail(f"serve failed in {result.phase}: {result.message}")
    if result.log_path:
        ui.hint(f"log: {result.log_path}")
    return result


def run_serve() -> int:
    result = serve_dashboard()
    if not result.ok:
        return 1

    ui.ok(f"Dashboard: {result.url}")
    return 0
