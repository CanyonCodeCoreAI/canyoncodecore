"""Entry point: ``python -m canyonos_core.machine_metrics_poller``."""

from __future__ import annotations

try:
    from canyonos_core.machine_metrics_poller.poller import main
except ImportError:  # standalone in-container layout (flat modules at context root)
    from poller import main


if __name__ == "__main__":
    main()
