import os
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "grpc_stubs")))

from canyonos_core.controller.local_controller import LocalController


class LocalControllerShutdownTests(unittest.TestCase):
    def test_stop_returns_when_executor_work_does_not_finish(self):
        release_work = threading.Event()
        work_started = threading.Event()
        executor = ThreadPoolExecutor(max_workers=1)

        def hang():
            work_started.set()
            release_work.wait()

        executor_future = executor.submit(hang)
        self.assertTrue(work_started.wait(timeout=1))
        controller = SimpleNamespace(
            _metrics_stop_event=MagicMock(),
            _metrics_thread=MagicMock(),
            _executor=executor,
            _executor_futures={executor_future: "future-hung"},
            _executor_futures_lock=threading.Lock(),
            redis=MagicMock(),
            _status_key="controller:localhost:50051:status",
            server=MagicMock(),
        )
        stop_finished = threading.Event()
        stop_errors = []

        def stop_controller():
            try:
                LocalController.stop(controller)
            except BaseException as error:
                stop_errors.append(error)
            finally:
                stop_finished.set()

        try:
            with (
                patch(
                    "canyonos_core.controller.local_controller.EXECUTOR_SHUTDOWN_TIMEOUT_SECONDS",
                    0.05,
                ),
                self.assertLogs(
                    "canyonos_core.controller.local_controller", level="WARNING"
                ) as logs,
            ):
                started_at = time.monotonic()
                stop_thread = threading.Thread(target=stop_controller)
                stop_thread.start()
                returned_before_deadline = stop_finished.wait(timeout=0.5)
                elapsed = time.monotonic() - started_at
                if not returned_before_deadline:
                    release_work.set()
                stop_thread.join(timeout=1)

            self.assertTrue(returned_before_deadline)
            self.assertLess(elapsed, 0.5)
            self.assertEqual(stop_errors, [])
            self.assertIn("future-hung", "\n".join(logs.output))
            controller.redis.set.assert_called_once_with(
                "controller:localhost:50051:status", "stopped"
            )
            controller.server.stop.assert_called_once_with(0)
        finally:
            release_work.set()
            executor_future.result(timeout=1)


if __name__ == "__main__":
    unittest.main()
