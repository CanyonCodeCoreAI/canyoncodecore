"""Registry for OS processes GlobalController spawns and supervises.

register() + start_all() spawn processes; check_and_respawn() (call from GC's existing
poll tick) restarts any that exit unexpectedly; terminate_all() (call from GC's shutdown
path) stops them all cleanly. Deliberately GC-agnostic -- callers are responsible for not
calling check_and_respawn() during their own shutdown (see ventis/OTLP_Exporter/DESIGN.md's
shutdown-race note).
"""

import logging
import os
import subprocess

logger = logging.getLogger(__name__)


class ProcessSupervisor:
    def __init__(self):
        self._specs = {}  # name -> (argv, env) tuple
        self._procs = {}  # name -> subprocess.Popen

    def is_registered(self, name):
        return name in self._specs

    def register(self, name, argv, env=None):
        """Declare a process to manage. Does not start it -- call start_all() once
        everything is registered. `env`, if given, is merged on top of (not a
        replacement for) this process's own environment, so the child still inherits
        PATH etc."""
        self._specs[name] = (argv, env)

    def start_all(self):
        for name, (argv, env) in self._specs.items():
            self._start(name, argv, env)

    def _start(self, name, argv, env=None):
        merged_env = {**os.environ, **env} if env else None
        self._procs[name] = subprocess.Popen(argv, env=merged_env)

    def check_and_respawn(self):
        """Restart any registered process that has exited."""
        for name, proc in list(self._procs.items()):
            if proc.poll() is not None:
                logger.warning(
                    "Managed process %r exited (code %s), respawning",
                    name,
                    proc.returncode,
                )
                argv, env = self._specs[name]
                self._start(name, argv, env)

    def terminate_all(self, timeout=10):
        """Terminate every managed process, falling back to kill() on timeout."""
        for proc in self._procs.values():
            proc.terminate()
        for name, proc in self._procs.items():
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        self._procs.clear()
