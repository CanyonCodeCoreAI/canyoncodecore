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
        self.ttls = {}

    def set(self, key, value):
        self.store[key] = value

    def get(self, key):
        return self.store.get(key)

    def sadd(self, name, *values):
        self.store.setdefault(name, set()).update(values)

    def expire(self, key, seconds):
        """Record the TTL only for keys that exist, matching Redis's no-op on
        missing keys -- the tests assert on which keys actually got one."""
        if key in self.store:
            self.ttls[key] = seconds


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
    fake_redis = _FakeRedis()

    def fake_run(self, *args, **kwargs):
        captured["app"] = self

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(deploy_module.Flask, "run", fake_run))
        stack.enter_context(
            patch.object(deploy_module, "RedisClient", return_value=fake_redis)
        )
        stack.enter_context(
            patch.object(deploy_module.threading, "Thread", _SyncThread)
        )
        deploy_module.deploy(workflow_fn)
        app = captured["app"]
        # deploy() keeps its RedisClient in a closure, so hang the fake off the app
        # to give tests a way to inspect what landed in Redis.
        app.fake_redis = fake_redis
        yield app


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
            self.assertEqual(first_call_args[3], "running")
            first_call_kwargs = mock_upsert.call_args_list[0].kwargs
            self.assertEqual(first_call_kwargs["input_payload"], {"x": 2})

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
            self.assertEqual(statuses, ["running", "completed"])
            success_call_kwargs = mock_upsert.call_args_list[1].kwargs
            self.assertEqual(success_call_kwargs["output_payload"], {"x": 2})

    def test_marks_session_failed_when_workflow_raises(self):
        os.environ["VENTIS_DATABASE_URL"] = "postgresql://example/db"
        os.environ["VENTIS_PROJECT_ID"] = "11111111-1111-1111-1111-111111111111"

        with patch.object(deploy_module, "upsert_session") as mock_upsert, \
                _deployed_app(workflow_fn=_failing_workflow) as app:
            client = app.test_client()
            resp = client.post("/_failing_workflow", json={"x": 2})

            self.assertEqual(resp.status_code, 202)
            statuses = [call.args[3] for call in mock_upsert.call_args_list]
            self.assertEqual(statuses, ["running", "failed"])
            failed_call_kwargs = mock_upsert.call_args_list[1].kwargs
            self.assertEqual(
                failed_call_kwargs["output_payload"], {"error": "workflow blew up"}
            )

    def test_skips_session_upsert_when_project_id_is_missing(self):
        # project_id is NOT NULL in the session table, so a URL without a project
        # id can only produce failing writes -- don't attempt them at all.
        os.environ["VENTIS_DATABASE_URL"] = "postgresql://example/db"

        with patch.object(deploy_module, "upsert_session") as mock_upsert, \
                _deployed_app() as app:
            client = app.test_client()
            resp = client.post("/_noop_workflow", json={"x": 2})

            self.assertEqual(resp.status_code, 202)
            mock_upsert.assert_not_called()

    def test_does_not_write_dead_workflow_and_created_at_keys(self):
        with _deployed_app() as app:
            client = app.test_client()
            resp = client.post("/_noop_workflow", json={"x": 2})
            request_id = resp.get_json()["request_id"]

            self.assertNotIn(f"request:{request_id}:workflow", app.fake_redis.store)
            self.assertNotIn(f"request:{request_id}:created_at", app.fake_redis.store)


class DeployRequestKeyExpiryTests(unittest.TestCase):
    """Finished requests must leave their Redis keys with a TTL -- without one they
    accumulate for the lifetime of the Redis instance and eventually OOM it."""

    def setUp(self):
        os.environ.pop("VENTIS_DATABASE_URL", None)
        os.environ.pop("VENTIS_PROJECT_ID", None)

    def test_expires_status_and_result_on_success(self):
        with _deployed_app() as app:
            client = app.test_client()
            resp = client.post("/_noop_workflow", json={"x": 2})
            request_id = resp.get_json()["request_id"]

            self.assertEqual(
                app.fake_redis.ttls,
                {
                    f"request:{request_id}:status": deploy_module.COMPLETED_TTL_SECONDS,
                    f"request:{request_id}:result": deploy_module.COMPLETED_TTL_SECONDS,
                },
            )

    def test_expires_status_and_error_on_failure(self):
        with _deployed_app(workflow_fn=_failing_workflow) as app:
            client = app.test_client()
            resp = client.post("/_failing_workflow", json={"x": 2})
            request_id = resp.get_json()["request_id"]

            self.assertEqual(
                app.fake_redis.ttls,
                {
                    f"request:{request_id}:status": deploy_module.COMPLETED_TTL_SECONDS,
                    f"request:{request_id}:error": deploy_module.COMPLETED_TTL_SECONDS,
                },
            )

    def test_expires_context_when_one_was_supplied(self):
        with _deployed_app() as app:
            client = app.test_client()
            resp = client.post(
                "/_noop_workflow", json={"x": 2, "_context": {"role": "admin"}}
            )
            request_id = resp.get_json()["request_id"]

            self.assertEqual(
                app.fake_redis.ttls.get(f"request:{request_id}:context"),
                deploy_module.COMPLETED_TTL_SECONDS,
            )


class DeployStatusFallbackTests(unittest.TestCase):
    """Once the Redis keys expire, /status must keep its contract by reading the
    session row instead of 404-ing."""

    def setUp(self):
        os.environ["VENTIS_DATABASE_URL"] = "postgresql://example/db"
        os.environ["VENTIS_PROJECT_ID"] = "11111111-1111-1111-1111-111111111111"

    def tearDown(self):
        os.environ.pop("VENTIS_DATABASE_URL", None)
        os.environ.pop("VENTIS_PROJECT_ID", None)

    def test_maps_completed_session_to_done_with_result(self):
        row = {"status": "completed", "output": {"x": 2}}
        with patch.object(deploy_module, "get_session", return_value=row), \
                _deployed_app() as app:
            resp = app.test_client().get("/status/expired-id")

            self.assertEqual(resp.status_code, 200)
            self.assertEqual(
                resp.get_json(),
                {"request_id": "expired-id", "status": "done", "result": {"x": 2}},
            )

    def test_maps_failed_session_to_error_and_decodes_text_output(self):
        # Postgres hands back decoded JSONB; sqlite (and any driver without a JSON
        # type) hands back text.
        row = {"status": "failed", "output": '{"error": "workflow blew up"}'}
        with patch.object(deploy_module, "get_session", return_value=row), \
                _deployed_app() as app:
            resp = app.test_client().get("/status/expired-id")

            self.assertEqual(resp.status_code, 200)
            self.assertEqual(
                resp.get_json(),
                {
                    "request_id": "expired-id",
                    "status": "error",
                    "error": "workflow blew up",
                },
            )

    def test_maps_running_session_without_payload(self):
        row = {"status": "running", "output": None}
        with patch.object(deploy_module, "get_session", return_value=row), \
                _deployed_app() as app:
            resp = app.test_client().get("/status/expired-id")

            self.assertEqual(resp.status_code, 200)
            self.assertEqual(
                resp.get_json(), {"request_id": "expired-id", "status": "running"}
            )

    def test_404s_when_there_is_no_session_row(self):
        with patch.object(deploy_module, "get_session", return_value=None), \
                _deployed_app() as app:
            resp = app.test_client().get("/status/unknown-id")

            self.assertEqual(resp.status_code, 404)
            self.assertEqual(resp.get_json(), {"error": "Request not found"})

    def test_404s_instead_of_500_when_the_lookup_fails(self):
        with patch.object(
            deploy_module, "get_session", side_effect=RuntimeError("db down")
        ), _deployed_app() as app:
            resp = app.test_client().get("/status/expired-id")

            self.assertEqual(resp.status_code, 404)

    def test_does_not_touch_postgres_while_redis_still_has_the_request(self):
        with patch.object(deploy_module, "upsert_session"), \
                patch.object(deploy_module, "get_session") as mock_get, \
                _deployed_app() as app:
            client = app.test_client()
            request_id = client.post("/_noop_workflow", json={"x": 2}).get_json()[
                "request_id"
            ]
            resp = client.get(f"/status/{request_id}")

            self.assertEqual(resp.status_code, 200)
            self.assertEqual(
                resp.get_json(),
                {"request_id": request_id, "status": "done", "result": {"x": 2}},
            )
            mock_get.assert_not_called()

    def test_skips_the_fallback_when_the_database_is_not_configured(self):
        os.environ.pop("VENTIS_DATABASE_URL", None)
        os.environ.pop("VENTIS_PROJECT_ID", None)

        with patch.object(deploy_module, "get_session") as mock_get, \
                _deployed_app() as app:
            resp = app.test_client().get("/status/expired-id")

            self.assertEqual(resp.status_code, 404)
            mock_get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
