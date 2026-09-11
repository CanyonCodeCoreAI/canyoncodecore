"""Instance metrics poller.

A standalone, best-effort process spawned per local-controller container (mirroring
the LLM proxy) that samples true machine-level metrics -- CPU, GPU, disk, memory, and
uptime -- and writes them to this instance's Redis metrics hash on a fixed interval.
GlobalController reads that hash on its own poll tick.

Deliberately scoped to machine-level signals only: queue length, request counters, and
the health heartbeat stay in LocalController because they are in-process state a separate
process cannot observe.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
