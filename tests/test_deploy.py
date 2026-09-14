import contextlib
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import canyonos_core.controller.deploy as deploy_module


class _FakeRedis:
    def __init__(self):
        self.store = {}
        self.ttls = {}
        self.hashes = {}

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

    def hset_multiple(self, name, mapping):
        self.hashes.setdefault(name, {}).update(mapping)

    def hgetall(self, name):
        return dict(self.hashes.get(name, {}))


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
    def test_accepts_the_request_and_returns_a_request_id(self):
        with _deployed_app() as app:
            resp = app.test_client().post("/_noop_workflow", json={"x": 2})

            self.assertEqual(resp.status_code, 202)
            self.assertIn("request_id", resp.get_json())

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


class DeployStatusTests(unittest.TestCase):
    """/status is served entirely from Redis now that the Postgres `session` table is
    gone -- durable post-TTL history lives in the external API, not in core."""

    def test_serves_a_finished_request_from_redis(self):
        with _deployed_app() as app:
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

    def test_404s_when_redis_has_nothing(self):
        # After COMPLETED_TTL_SECONDS the request's Redis keys are gone and there is
        # no session fallback -- /status must 404 rather than error.
        with _deployed_app() as app:
            resp = app.test_client().get("/status/unknown-id")

            self.assertEqual(resp.status_code, 404)
            self.assertEqual(resp.get_json(), {"error": "Request not found"})


if __name__ == "__main__":
    unittest.main()
