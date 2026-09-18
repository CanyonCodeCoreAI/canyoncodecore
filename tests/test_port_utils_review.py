"""QA coverage for PR #141 (CAN-386) -- port hardening.

Everything here targets code the PR added or changed. The PR shipped zero tests
for its own new module, so these are written from the reachable paths.
"""

import socket
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from canyonos_core.controller.cloud_provider_logic.Local import (
    _runtime as local_runtime,
)
from canyonos_core.controller.utils import port_utils


def _bind(host, port=0, listen=True):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((host, port))
    if listen:
        s.listen(1)
    return s, s.getsockname()[1]


class IsPortConflictTests(unittest.TestCase):
    def test_recognises_both_documented_docker_messages(self):
        self.assertTrue(
            port_utils.is_port_conflict(
                "docker: Error response from daemon: driver failed programming external "
                "connectivity on endpoint x: Bind for 0.0.0.0:8080 failed: port is already allocated."
            )
        )
        self.assertTrue(
            port_utils.is_port_conflict(
                "docker: Error response from daemon: failed to bind host port for "
                "0.0.0.0:6379:172.18.0.2:6379/tcp: address already in use"
            )
        )

    def test_none_and_empty_are_not_conflicts(self):
        self.assertFalse(port_utils.is_port_conflict(None))
        self.assertFalse(port_utils.is_port_conflict(""))

    def test_unrelated_failures_are_not_conflicts(self):
        self.assertFalse(
            port_utils.is_port_conflict(
                "docker: Error response from daemon: Conflict. The container name "
                '"/canyonos-redis-localhost" is already in use'
            )
        )
        self.assertFalse(
            port_utils.is_port_conflict("Cannot connect to the Docker daemon")
        )

    def test_bytes_stderr_raises_instead_of_returning_false(self):
        """subprocess without text=True yields bytes; the helper is str-only."""
        with self.assertRaises(TypeError):
            port_utils.is_port_conflict(b"port is already allocated")


class IsPortFreeTests(unittest.TestCase):
    def test_free_port_reports_free(self):
        s, port = _bind("127.0.0.1")
        s.close()
        self.assertTrue(port_utils.is_port_free(port))

    def test_loopback_listener_reports_taken(self):
        s, port = _bind("127.0.0.1")
        self.addCleanup(s.close)
        self.assertFalse(port_utils.is_port_free(port))

    def test_wildcard_listener_reports_taken(self):
        s, port = _bind("0.0.0.0")
        self.addCleanup(s.close)
        self.assertFalse(port_utils.is_port_free(port))

    def test_time_wait_port_is_reported_taken(self):
        """REGRESSION: the shared probe dropped SO_REUSEADDR that main's _port_bound set.

        A connection left in TIME_WAIT makes a plain bind fail, while `docker -p`
        (which sets SO_REUSEADDR) would have bound it fine.
        """
        srv, port = _bind("127.0.0.1")
        cli = socket.create_connection(("127.0.0.1", port))
        conn, _ = srv.accept()
        cli.close()
        conn.close()
        srv.close()  # server socket gone; the closed conn lingers in TIME_WAIT
        reusable = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        reusable.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            reusable.bind(("127.0.0.1", port))
            docker_could_bind = True
        except OSError:
            docker_could_bind = False
        finally:
            reusable.close()
        if not docker_could_bind:
            self.skipTest("kernel did not leave the port in a REUSEADDR-bindable state")
        self.assertTrue(
            port_utils.is_port_free(port),
            "probe says the port is taken but docker (SO_REUSEADDR) can bind it",
        )

    def test_non_loopback_listener_is_missed_by_the_loopback_probe(self):
        """Documents the blind spot the 'probe on loopback, not 0.0.0.0' commit created."""
        addr = socket.gethostbyname(socket.gethostname())
        if addr.startswith("127."):
            self.skipTest("no non-loopback address on this host")
        s, port = _bind(addr)
        self.addCleanup(s.close)
        wildcard = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            wildcard.bind(("0.0.0.0", port))
            wildcard.close()
            self.skipTest("kernel allowed the wildcard bind anyway")
        except OSError:
            pass  # this is what `docker run -p` would hit
        self.assertFalse(
            port_utils.is_port_free(port),
            "loopback probe called the port free, but a 0.0.0.0 publish would fail",
        )

    def test_string_port_raises_typeerror(self):
        """YAML gives back `api_port: '8080'` as str; main's caller int()-ed it, the PR's does not."""
        with self.assertRaises(TypeError):
            port_utils.is_port_free("8080")


class FindFreePortTests(unittest.TestCase):
    def test_returns_start_when_start_is_free(self):
        s, port = _bind("127.0.0.1")
        s.close()
        self.assertEqual(port_utils.find_free_port(port), port)

    def test_hops_past_a_taken_port(self):
        s, port = _bind("127.0.0.1")
        self.addCleanup(s.close)
        self.assertGreater(port_utils.find_free_port(port), port)

    def test_raises_when_no_port_is_free(self):
        with (
            patch.object(port_utils, "is_port_free", return_value=False),
            self.assertRaises(RuntimeError) as ctx,
        ):
            port_utils.find_free_port(9000, max_attempts=3)
        self.assertIn("9000", str(ctx.exception))

    def test_zero_max_attempts_raises_rather_than_looping(self):
        with self.assertRaises(RuntimeError):
            port_utils.find_free_port(9000, max_attempts=0)

    def test_scan_does_not_run_past_the_last_valid_port(self):
        """start near 65535 walks into port 65536 -> OSError, not a clean RuntimeError."""
        with self.assertRaises(RuntimeError):
            port_utils.find_free_port(65530, max_attempts=50)


class TwinModuleDriftTests(unittest.TestCase):
    """The module docstring requires the two copies stay byte-for-byte identical."""

    def test_the_two_copies_are_identical(self):
        root = Path(__file__).resolve().parents[1]
        core = root / "canyonos_core/controller/utils/port_utils.py"
        cli = root / "cli/canyonos/port_utils.py"
        self.assertEqual(
            core.read_bytes(),
            cli.read_bytes(),
            "port_utils twins have drifted; nothing in CI enforces this invariant",
        )


class WorkflowApiPortTests(unittest.TestCase):
    """_port_bound is the PR's fail-fast gate for a user-declared api_port."""

    def _spec(self, **over):
        spec = {"name": "wf", "type": "workflow", "host": "localhost", "api_port": 8080}
        spec.update(over)
        return spec

    def test_string_api_port_crashes_with_typeerror_not_a_clean_error(self):
        with self.assertRaises(TypeError):
            local_runtime._port_bound(self._spec(api_port="8080")["api_port"])

    def test_remote_host_api_port_is_probed_against_the_wrong_machine(self):
        """The PR dropped main's `and _is_local_host(host)` guard.

        For an EC2/remote host the probe binds the *controller's* loopback, so the
        answer describes the wrong machine either way.
        """
        src = Path(local_runtime.__file__).read_text()
        gate = [ln for ln in src.splitlines() if 'ctrl_type == "workflow"' in ln]
        self.assertTrue(gate)
        self.assertIn(
            "_is_local_host(host)",
            gate[0],
            "workflow api_port preflight runs for remote hosts too, probing local loopback",
        )


class RedisHopTests(unittest.TestCase):
    """The PR made Redis hop instead of exiting. Check where the new port goes."""

    def _controller(self, run_cmd):
        from canyonos_core.controller.global_controller import GlobalController

        gc = GlobalController.__new__(GlobalController)
        gc.controllers = [
            {"user": None, "redis_port": 6379, "replicas": 1, "host": "localhost"}
        ]
        gc.redis_containers = {}
        gc.node_redis = {}
        gc.redis = None
        gc._run_cmd = run_cmd
        gc._get_replica_placements = lambda ctrl: [("localhost", 5001)]
        gc._redis_container_healthy = lambda *a, **k: False
        return gc

    def _run_cmd_factory(self, conflicts):
        state = {"n": 0}

        def run_cmd(cmd, host=None, user=None):
            res = MagicMock()
            if cmd[:2] == ["docker", "run"]:
                state["n"] += 1
                if state["n"] <= conflicts:
                    res.returncode = 1
                    res.stderr = (
                        "Bind for 0.0.0.0:6379 failed: port is already allocated"
                    )
                else:
                    res.returncode = 0
                    res.stderr = ""
                    run_cmd.published = cmd[cmd.index("-p") + 1]
            else:
                res.returncode = 0
                res.stderr = ""
                res.stdout = ""
            return res

        run_cmd.published = None
        return run_cmd

    def test_hops_to_the_next_port_on_conflict(self):
        run_cmd = self._run_cmd_factory(conflicts=2)
        gc = self._controller(run_cmd)
        with (
            patch("canyonos_core.controller.global_controller.RedisClient"),
            patch("canyonos_core.controller.global_controller._wait_for_redis"),
        ):
            gc._launch_redis_containers()
        self.assertEqual(run_cmd.published, "6381:6379")

    def test_hopped_port_is_not_persisted_for_the_next_run(self):
        """After hopping to 6381, teardown and the next run still look at 6379."""
        run_cmd = self._run_cmd_factory(conflicts=2)
        gc = self._controller(run_cmd)
        with (
            patch("canyonos_core.controller.global_controller.RedisClient"),
            patch("canyonos_core.controller.global_controller._wait_for_redis"),
        ):
            gc._launch_redis_containers()
        self.assertEqual(gc.controllers[0]["redis_port"], 6379)
        self.assertEqual(
            gc.controllers[0]["redis_port"],
            int(run_cmd.published.split(":")[0]),
            "the hopped host port is dropped on the floor -- the next launch/teardown "
            "re-reads redis_port=6379 from config and will not find this container",
        )

    def test_string_redis_port_crashes_while_hopping(self):
        run_cmd = self._run_cmd_factory(conflicts=1)
        gc = self._controller(run_cmd)
        gc.controllers[0]["redis_port"] = "6379"
        with (
            patch("canyonos_core.controller.global_controller.RedisClient"),
            patch("canyonos_core.controller.global_controller._wait_for_redis"),
            self.assertRaises(TypeError),
        ):
            gc._launch_redis_containers()

    def test_container_name_conflict_still_hard_exits(self):
        def run_cmd(cmd, host=None, user=None):
            res = MagicMock()
            res.stdout = ""
            if cmd[:2] == ["docker", "run"]:
                res.returncode = 125
                res.stderr = (
                    "docker: Error response from daemon: Conflict. The container "
                    'name "/canyonos-redis-localhost" is already in use'
                )
            else:
                res.returncode = 0
                res.stderr = ""
            return res

        gc = self._controller(run_cmd)
        with (
            patch("canyonos_core.controller.global_controller.RedisClient"),
            patch("canyonos_core.controller.global_controller._wait_for_redis"),
            self.assertRaises(SystemExit),
        ):
            gc._launch_redis_containers()

    def test_exhausting_all_attempts_exits_rather_than_hanging(self):
        run_cmd = self._run_cmd_factory(conflicts=999)
        gc = self._controller(run_cmd)
        with (
            patch("canyonos_core.controller.global_controller.RedisClient"),
            patch("canyonos_core.controller.global_controller._wait_for_redis"),
            self.assertRaises(SystemExit),
        ):
            gc._launch_redis_containers()


if __name__ == "__main__":
    unittest.main()


class WriteProjectEnvNewlineTests(unittest.TestCase):
    """The PR's second change: don't glue a new key onto an unterminated last line."""

    def setUp(self):
        import tempfile

        from canyonos import dashboard_stack

        self.dashboard_stack = dashboard_stack
        self.dir = tempfile.mkdtemp()

    def _write(self, existing, managed):
        p = Path(self.dir) / ".env"
        p.write_text(existing, encoding="utf-8")
        self.dashboard_stack._write_project_env(p, managed)
        return p.read_text(encoding="utf-8")

    def test_unterminated_comment_last_line_no_longer_swallows_the_new_key(self):
        out = self._write("# trailing comment", {"CANYONOS_WEB_PORT": "8081"})
        self.assertIn("# trailing comment\n", out)
        self.assertIn("CANYONOS_WEB_PORT=8081\n", out)
        self.assertNotIn("# trailing commentCANYONOS_WEB_PORT", out)

    def test_unterminated_key_last_line_is_preserved(self):
        out = self._write("FOO=bar", {"CANYONOS_WEB_PORT": "8081"})
        self.assertEqual(out, "FOO=bar\nCANYONOS_WEB_PORT=8081\n")

    def test_replacing_an_existing_managed_key_does_not_duplicate_it(self):
        out = self._write(
            "CANYONOS_WEB_PORT=1\nCANYONOS_WEB_PORT=2\n", {"CANYONOS_WEB_PORT": "8081"}
        )
        self.assertEqual(out.count("CANYONOS_WEB_PORT"), 1)

    def test_a_commented_out_managed_key_is_left_alone_and_the_key_is_appended(self):
        out = self._write("#CANYONOS_WEB_PORT=9\n", {"CANYONOS_WEB_PORT": "8081"})
        self.assertIn("#CANYONOS_WEB_PORT=9\n", out)
        self.assertIn("CANYONOS_WEB_PORT=8081\n", out)

    def test_empty_file_still_writes_the_managed_keys(self):
        out = self._write("", {"CANYONOS_WEB_PORT": "8081"})
        self.assertEqual(out, "CANYONOS_WEB_PORT=8081\n")
