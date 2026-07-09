import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests.support.runtime_fakes import FakeRedis
from tests.support.runtime_fakes import install_grpc_stubs
from tests.support.runtime_fakes import make_global_controller

install_grpc_stubs()

import ventis.ventis_context as ventis_context
from ventis.controller.global_controller import GlobalController
from ventis.controller.local_controller import LocalController
from ventis.controller.local_controller_frontend import LocalControllerServicer
from ventis.deploy import deploy
from ventis.future import Future


class InlineThread:
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}
        self.daemon = daemon

    def start(self):
        self.target(*self.args, **self.kwargs)


class AgentIdStickinessTests(unittest.TestCase):
    def setUp(self):
        ventis_context.set_request_id(None)
        ventis_context.set_agent_id(None)
        ventis_context.set_session_priority(None)
        ventis_context.set_agent_priority(None)

    def test_stateful_workflow_post_generates_agent_id(self):
        redis = FakeRedis()
        redis.hset("routing_table:stateful", "Sticky", "true")
        captured = {}

        def workflow(name="World"):
            return {"name": name, "agent_id": ventis_context.get_agent_id()}

        def fake_run(app, host=None, port=None):
            captured["app"] = app

        with patch("ventis.deploy.RedisClient", return_value=redis), patch(
            "ventis.deploy.threading.Thread", InlineThread
        ), patch("ventis.deploy.Flask.run", new=fake_run):
            deploy(workflow, port=0, host="127.0.0.1")

        response = captured["app"].test_client().post("/workflow", json={"name": "Ada"})
        payload = response.get_json()

        self.assertEqual(response.status_code, 202)
        self.assertIn("request_id", payload)
        self.assertIn("agent_id", payload)
        stored_context = json.loads(redis.get(f"request:{payload['request_id']}:context"))
        self.assertEqual(stored_context["agent_id"], payload["agent_id"])
        self.assertEqual(stored_context["session_priority"], 0)

    def test_workflow_post_stores_session_priority(self):
        redis = FakeRedis()
        captured = {}

        def workflow(name="World"):
            return {
                "name": name,
                "session_priority": ventis_context.get_session_priority(),
                "agent_priority": ventis_context.get_agent_priority(),
            }

        def fake_run(app, host=None, port=None):
            captured["app"] = app

        with patch.dict("os.environ", {"VENTIS_AGENT_PRIORITY": "7"}, clear=False), patch(
            "ventis.deploy.RedisClient", return_value=redis
        ), patch("ventis.deploy.threading.Thread", InlineThread), patch(
            "ventis.deploy.Flask.run", new=fake_run
        ):
            deploy(workflow, port=0, host="127.0.0.1")

        response = captured["app"].test_client().post(
            "/workflow",
            json={"name": "Ada", "_context": {"session_priority": -3}},
        )
        payload = response.get_json()
        stored_context = json.loads(redis.get(f"request:{payload['request_id']}:context"))

        self.assertEqual(response.status_code, 202)
        self.assertEqual(stored_context["session_priority"], -3)
        self.assertEqual(stored_context["agent_priority"], 7)
        result = json.loads(redis.get(f"request:{payload['request_id']}:result"))
        self.assertEqual(result["session_priority"], -3)
        self.assertEqual(result["agent_priority"], 7)

    def test_stateless_workflow_post_skips_agent_id(self):
        redis = FakeRedis()
        captured = {}

        def workflow(name="World"):
            return {"name": name}

        def fake_run(app, host=None, port=None):
            captured["app"] = app

        with patch("ventis.deploy.RedisClient", return_value=redis), patch(
            "ventis.deploy.threading.Thread", InlineThread
        ), patch("ventis.deploy.Flask.run", new=fake_run):
            deploy(workflow, port=0, host="127.0.0.1")

        response = captured["app"].test_client().post("/workflow", json={"name": "Ada"})
        payload = response.get_json()

        self.assertEqual(response.status_code, 202)
        self.assertIn("request_id", payload)
        self.assertNotIn("agent_id", payload)

    def test_stateful_routing_reuses_agent_id_across_requests(self):
        controller = LocalController.__new__(LocalController)
        controller.redis = FakeRedis()
        controller.redis.hset("routing_table:stateful", "Sticky", "true")
        controller.redis.hset(
            "routing_table:endpoints",
            "Sticky",
            json.dumps(["host:5001", "host:5002"]),
        )

        with patch("ventis.controller.local_controller.random.choice", side_effect=["host:5001", "host:5002"]):
            first = LocalController._resolve_endpoint(controller, "Sticky", "req-1", agent_id="agent-a")
            second = LocalController._resolve_endpoint(controller, "Sticky", "req-2", agent_id="agent-a")
            third = LocalController._resolve_endpoint(controller, "Sticky", "req-3", agent_id="agent-b")

        self.assertEqual(first, "host:5001")
        self.assertEqual(second, "host:5001")
        self.assertEqual(third, "host:5002")
        self.assertEqual(controller.redis.hget("affinity:Sticky:agent-a", "endpoint"), "host:5001")
        self.assertEqual(controller.redis.hget("affinity:req-1", "Sticky"), "host:5001")
        self.assertEqual(controller.redis.hget("affinity:req-2", "Sticky"), "host:5001")

    def test_future_submissions_include_agent_id(self):
        Future.redis = FakeRedis()
        ventis_context.set_request_id("req-1")
        ventis_context.set_agent_id("agent-1")
        ventis_context.set_session_priority(-2)
        ventis_context.set_agent_priority(4)
        seen = []

        class Stub:
            def Execute(self, request):
                seen.append(json.loads(request.resonse))
                return object()

        with patch.object(Future, "_get_stub", return_value=Stub()):
            Future(parent="root", service="Sticky", method="call", args={"x": 1})

        self.assertEqual(seen[0]["request_id"], "req-1")
        self.assertEqual(seen[0]["agent_id"], "agent-1")
        self.assertEqual(seen[0]["session_priority"], -2)
        self.assertEqual(seen[0]["agent_priority"], 4)

    def test_nested_future_submissions_use_parent_session_and_local_agent_priority(self):
        controller = LocalController.__new__(LocalController)
        controller.agent = None
        controller.agent_name = "Sticky"
        controller.agent_priority = 9
        controller.redis = FakeRedis()
        controller._my_endpoint = "host:5000"
        controller._active_requests = 0
        controller._active_requests_lock = None
        controller._publish_active_work = lambda: None
        controller._publish_queue_depth = lambda: None

        seen = []

        class Stub:
            def Execute(self, request):
                seen.append(json.loads(request.resonse))
                return object()

        class Agent:
            def call(self):
                Future(parent="root", service="Child", method="work", args={})
                return "done"

        controller.agent = Agent()
        controller.redis.set(
            "request:req-1:context",
            json.dumps({"session_priority": -5}),
        )

        with patch.object(Future, "_get_stub", return_value=Stub()):
            LocalController._execute_locally(
                controller,
                "Sticky",
                "call",
                {},
                "future-1",
                request_id="req-1",
                agent_id="agent-1",
            )

        self.assertEqual(seen[0]["session_priority"], -5)
        self.assertEqual(seen[0]["agent_priority"], 9)

    def test_cleanup_keeps_persistent_agent_affinity(self):
        redis = FakeRedis()
        servicer = LocalControllerServicer(my_endpoint="host:5000")
        servicer.redis = redis
        redis.sadd("request:req-1:futures", "future-1")
        redis.hset("future:future-1", "id", "future-1")
        redis.hset("affinity:req-1", "Sticky", "host:5001")
        redis.hset("affinity:Sticky:agent-1", "endpoint", "host:5001")

        servicer._cleanup_request("req-1")

        self.assertIsNone(redis.hget("affinity:req-1", "Sticky"))
        self.assertEqual(redis.hget("affinity:Sticky:agent-1", "endpoint"), "host:5001")

    def test_global_controller_ignores_persistent_agent_affinity_for_inflight_checks(self):
        controller = make_global_controller([])
        controller.redis.hset("affinity:Sticky:agent-1", "endpoint", "host.docker.internal:8001")

        self.assertFalse(
            GlobalController._has_affine_inflight_requests(
                controller,
                "Sticky",
                "host.docker.internal:8001",
            )
        )

    def test_forwarded_requests_preserve_priorities(self):
        controller = LocalController.__new__(LocalController)
        controller.redis = FakeRedis()
        controller._my_endpoint = "host:5000"
        controller.agent_name = "Sticky"
        controller.agent_priority = 6
        captured = {}

        controller._check_policy = lambda service, context: True
        controller._resolve_endpoint = lambda service, request_id, agent_id=None: "remote:5001"
        controller._forward_request = lambda endpoint, data: captured.update(
            {"endpoint": endpoint, "data": json.loads(json.dumps(data))}
        )

        LocalController._process_request(
            controller,
            {
                "service": "Sticky",
                "function": "call",
                "args": {},
                "future_id": "f1",
                "request_id": "req-1",
                "session_priority": -4,
                "agent_priority": 2,
                "baggage": {"context": {"agent_id": "agent-1"}},
            },
        )

        self.assertEqual(captured["endpoint"], "remote:5001")
        self.assertEqual(captured["data"]["session_priority"], -4)
        self.assertEqual(captured["data"]["agent_priority"], 2)
        self.assertEqual(captured["data"]["baggage"]["context"]["session_priority"], -4)
        self.assertEqual(captured["data"]["baggage"]["context"]["agent_priority"], 2)

    def test_frontend_priority_queue_orders_session_then_agent_then_arrival(self):
        servicer = LocalControllerServicer(my_endpoint="host:5000")
        payloads = [
            {"future_id": "low", "service": "A", "function": "x", "session_priority": 2, "agent_priority": 0},
            {"future_id": "high-agent", "service": "A", "function": "x", "session_priority": 1, "agent_priority": 5},
            {"future_id": "high-agent-first", "service": "A", "function": "x", "session_priority": 1, "agent_priority": 3},
            {"future_id": "high-agent-first-later", "service": "A", "function": "x", "session_priority": 1, "agent_priority": 3},
        ]

        for payload in payloads:
            servicer.Execute(SimpleNamespace(resonse=json.dumps(payload)), None)

        ordered = [json.loads(servicer.request_queue.get()[3])["future_id"] for _ in payloads]
        self.assertEqual(
            ordered,
            ["high-agent-first", "high-agent-first-later", "high-agent", "low"],
        )


if __name__ == "__main__":
    unittest.main()
