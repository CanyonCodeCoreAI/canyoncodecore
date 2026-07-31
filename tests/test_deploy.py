import contextlib
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import ventis.deploy as deploy_module


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def set(self, key, value):
        self.store[key] = value

    def get(self, key):
        return self.store.get(key)

    def sadd(self, name, *values):
        self.store.setdefault(name, set()).update(values)


class _SyncThread:
    """Runs the target synchronously instead of on a real thread. None of
    these tests need real concurrency, and real daemon threads left running
    past their test method's return would otherwise fire against whichever
    mock happens to be active in a later test."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


def _noop_workflow(x=1):
    return {"x": x}


def _failing_workflow(x=1):
    raise RuntimeError("workflow blew up")


@contextlib.contextmanager
def _deployed_app(workflow_fn=_noop_workflow):
    """Run deploy() with Flask's blocking app.run() replaced by a no-op that
    captures the app instance, so the route handlers can be exercised via
    Flask's test client without starting a real server. The workflow's
    background thread is replaced with a synchronous stand-in -- and that
    patch (along with RedisClient's) must stay active for the whole `with`
    block, not just the deploy() call, since threading.Thread is only
    actually constructed later, inside handle_workflow(), when a request
    comes in via the test client."""
    captured = {}

    def fake_run(self, *args, **kwargs):
        captured["app"] = self

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(deploy_module.Flask, "run", fake_run))
        stack.enter_context(
            patch.object(deploy_module, "RedisClient", return_value=_FakeRedis())
        )
        stack.enter_context(
            patch.object(deploy_module.threading, "Thread", _SyncThread)
        )
        deploy_module.deploy(workflow_fn)
        yield captured["app"]


class DeployHandleWorkflowTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("VENTIS_DATABASE_URL", None)
        os.environ.pop("VENTIS_PROJECT_ID", None)

    def tearDown(self):
        os.environ.pop("VENTIS_DATABASE_URL", None)
        os.environ.pop("VENTIS_PROJECT_ID", None)

    def test_records_working_status_before_dispatch_when_configured(self):
        os.environ["VENTIS_DATABASE_URL"] = "postgresql://example/db"
        os.environ["VENTIS_PROJECT_ID"] = "11111111-1111-1111-1111-111111111111"

        with patch.object(deploy_module, "upsert_session") as mock_upsert, \
                _deployed_app() as app:
            client = app.test_client()
            resp = client.post("/_noop_workflow", json={"x": 2})

            self.assertEqual(resp.status_code, 202)
            request_id = resp.get_json()["request_id"]
            first_call_args = mock_upsert.call_args_list[0].args
            self.assertEqual(first_call_args[0], "postgresql://example/db")
            self.assertEqual(first_call_args[1], "11111111-1111-1111-1111-111111111111")
            self.assertEqual(first_call_args[2], request_id)
            self.assertEqual(first_call_args[3], "working")

    def test_skips_session_upsert_when_not_configured(self):
        with patch.object(deploy_module, "upsert_session") as mock_upsert, \
                _deployed_app() as app:
            client = app.test_client()
            resp = client.post("/_noop_workflow", json={"x": 2})

            self.assertEqual(resp.status_code, 202)
            mock_upsert.assert_not_called()

    def test_session_upsert_failure_does_not_fail_the_request(self):
        os.environ["VENTIS_DATABASE_URL"] = "postgresql://example/db"
        os.environ["VENTIS_PROJECT_ID"] = "11111111-1111-1111-1111-111111111111"

        with patch.object(
            deploy_module, "upsert_session", side_effect=RuntimeError("db down")
        ), _deployed_app() as app:
            client = app.test_client()
            resp = client.post("/_noop_workflow", json={"x": 2})

            self.assertEqual(resp.status_code, 202)
            self.assertIn("request_id", resp.get_json())

    def test_marks_session_success_when_workflow_completes(self):
        os.environ["VENTIS_DATABASE_URL"] = "postgresql://example/db"
        os.environ["VENTIS_PROJECT_ID"] = "11111111-1111-1111-1111-111111111111"

        with patch.object(deploy_module, "upsert_session") as mock_upsert, \
                _deployed_app() as app:
            client = app.test_client()
            resp = client.post("/_noop_workflow", json={"x": 2})

            self.assertEqual(resp.status_code, 202)
            statuses = [call.args[3] for call in mock_upsert.call_args_list]
            self.assertEqual(statuses, ["working", "success"])

    def test_marks_session_failed_when_workflow_raises(self):
        os.environ["VENTIS_DATABASE_URL"] = "postgresql://example/db"
        os.environ["VENTIS_PROJECT_ID"] = "11111111-1111-1111-1111-111111111111"

        with patch.object(deploy_module, "upsert_session") as mock_upsert, \
                _deployed_app(workflow_fn=_failing_workflow) as app:
            client = app.test_client()
            resp = client.post("/_failing_workflow", json={"x": 2})

            self.assertEqual(resp.status_code, 202)
            statuses = [call.args[3] for call in mock_upsert.call_args_list]
            self.assertEqual(statuses, ["working", "failed"])


if __name__ == "__main__":
    unittest.main()
