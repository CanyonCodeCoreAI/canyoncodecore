import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from canyonos_core.controller.utils.process_supervisor import ProcessSupervisor

# A child that lives long enough to be observed but never outlasts the test run.
_SLEEP_ARGV = [sys.executable, "-c", "import time; time.sleep(30)"]
_EXIT_ARGV = [sys.executable, "-c", ""]

# Dumps the env the child actually received to the file named by VENTIS_TEST_OUT.
_DUMP_ENV_SRC = (
    "import json, os\n"
    "with open(os.environ['VENTIS_TEST_OUT'], 'w') as f:\n"
    "    json.dump({k: os.environ.get(k) for k in ('PATH', 'VENTIS_TEST_MARKER')}, f)\n"
)


class ProcessSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.supervisor = ProcessSupervisor()
        self.addCleanup(self.supervisor.terminate_all, 5)

    def test_register_and_start_all_spawns_each_process(self):
        self.supervisor.register("a", _SLEEP_ARGV)
        self.supervisor.register("b", _SLEEP_ARGV)

        self.supervisor.start_all()

        self.assertEqual(set(self.supervisor._procs), {"a", "b"})
        for proc in self.supervisor._procs.values():
            self.assertIsNone(proc.poll())

    def test_check_and_respawn_restarts_an_exited_process_with_a_new_pid(self):
        self.supervisor.register("dies", _EXIT_ARGV)
        self.supervisor.start_all()
        first = self.supervisor._procs["dies"]
        first.wait(timeout=10)

        self.supervisor.check_and_respawn()

        second = self.supervisor._procs["dies"]
        self.assertNotEqual(second.pid, first.pid)
        second.wait(timeout=10)

    def test_check_and_respawn_leaves_a_live_process_alone(self):
        self.supervisor.register("alive", _SLEEP_ARGV)
        self.supervisor.start_all()
        original_pid = self.supervisor._procs["alive"].pid

        self.supervisor.check_and_respawn()

        self.assertEqual(self.supervisor._procs["alive"].pid, original_pid)
        self.assertIsNone(self.supervisor._procs["alive"].poll())

    def test_terminate_all_stops_everything_and_clears_the_registry(self):
        self.supervisor.register("a", _SLEEP_ARGV)
        self.supervisor.register("b", _SLEEP_ARGV)
        self.supervisor.start_all()
        procs = list(self.supervisor._procs.values())

        self.supervisor.terminate_all(timeout=5)

        self.assertEqual(self.supervisor._procs, {})
        for proc in procs:
            self.assertIsNotNone(proc.poll())

    def test_register_env_merges_on_top_of_os_environ(self):
        out_dir = tempfile.TemporaryDirectory()
        self.addCleanup(out_dir.cleanup)
        out_path = os.path.join(out_dir.name, "env.json")
        self.supervisor.register(
            "env",
            [sys.executable, "-c", _DUMP_ENV_SRC],
            env={"VENTIS_TEST_MARKER": "set", "VENTIS_TEST_OUT": out_path},
        )

        self.supervisor.start_all()
        self.assertEqual(self.supervisor._procs["env"].wait(timeout=10), 0)

        with open(out_path) as f:
            child_env = json.load(f)
        self.assertEqual(child_env["VENTIS_TEST_MARKER"], "set")
        self.assertEqual(child_env["PATH"], os.environ["PATH"])
        self.assertNotIn("VENTIS_TEST_MARKER", os.environ)


if __name__ == "__main__":
    unittest.main()
