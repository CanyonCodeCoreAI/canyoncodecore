"""CLI output for the local dashboard stack."""

from .dashboard_stack import DEFAULT_CONFIG_PATH, run_dashboard


def run_serve(config_path: str = DEFAULT_CONFIG_PATH) -> int:
    def report(phase: str, message: str) -> None:
        print(f"[serve] {phase}: {message}")

    result = run_dashboard(config_path, report)
    if result.ok:
        print(f"Dashboard: {result.url}")
        return 0

    print(f"serve failed in {result.phase}: {result.message}")
    if result.log_path:
        print(f"log: {result.log_path}")
    return 1
