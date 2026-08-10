import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from grpc_tools import protoc

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROTO_DIR = PROJECT_ROOT / "ventis" / "controller" / "proto"
_GENERATED_STUBS = tempfile.TemporaryDirectory()

for proto_path in PROTO_DIR.glob("*.proto"):
    result = protoc.main(
        [
            "grpc_tools.protoc",
            f"-I{PROTO_DIR}",
            f"--python_out={_GENERATED_STUBS.name}",
            f"--grpc_python_out={_GENERATED_STUBS.name}",
            str(proto_path),
        ]
    )
    if result != 0:
        raise RuntimeError(f"Failed to compile gRPC stubs from {proto_path}")

sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, _GENERATED_STUBS.name)

from ventis.controller.local_controller import LocalController
from ventis.utils.redis_client import RedisClient


class _FakeRedis:
    def __init__(self):
        self.hashes = {}
        self.strings = {}

    def hset(self, name, field, value):
        self.hashes.setdefault(name, {})[field] = value

    def hincrby(self, name, field, amount=1):
        bucket = self.hashes.setdefault(name, {})
        bucket[field] = int(bucket.get(field, 0)) + amount
        return bucket[field]

    def hset_multiple(self, name, mapping):
        self.hashes.setdefault(name, {}).update(mapping)

    def hget(self, name, field):
        return self.hashes.get(name, {}).get(field)

    def set(self, name, value):
        self.strings[name] = value

    def get(self, name):
        return self.strings.get(name)


class HookTests(unittest.TestCase):
    def _controller(self, agent=None, redis=None):
        controller = SimpleNamespace(
            redis=redis or _FakeRedis(),
            agent=agent or SimpleNamespace(greet=lambda name: f"hello {name}"),
            agent_name="Greeter",
            agent_id="agent-1",
            _my_endpoint="localhost:50051",
            _metrics_key="controller:localhost:50051:metrics",
            _resolve_future_args=lambda args: args,
            _hooks={"before_call": [], "after_call": []},
        )
        controller._mark_future_failed = (
            lambda future_id, error, origin=None: LocalController._mark_future_failed(
                controller, future_id, error, origin
            )
        )
        return controller

    def _execute(self, controller, args, future_id="future-hooks"):
        with patch(
            "ventis.controller.local_controller.read_gpu_percent", return_value=0.0
        ):
            LocalController._execute_locally(
                controller, "Greeter", "greet", args, future_id
            )

    def _write_config(self, directory, definitions):
        with open(os.path.join(directory, "ventis_hooks.json"), "w") as f:
            json.dump(definitions, f)

    def test_no_config_returns_empty_hook_lists(self):
        controller = self._controller()
        original_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                hooks = LocalController._load_hooks(controller)
            finally:
                os.chdir(original_cwd)

        self.assertEqual(hooks, {"before_call": [], "after_call": []})

    def test_malformed_config_raises_json_error(self):
        controller = self._controller()
        original_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "ventis_hooks.json").write_text("{not-json")
            os.chdir(tmpdir)
            try:
                with self.assertRaises(json.JSONDecodeError):
                    LocalController._load_hooks(controller)
            finally:
                os.chdir(original_cwd)

    def test_missing_hook_file_raises_file_not_found(self):
        controller = self._controller()
        original_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_config(tmpdir, [{"entrypoint": "missing.py"}])
            os.chdir(tmpdir)
            try:
                with self.assertRaises(FileNotFoundError):
                    LocalController._load_hooks(controller)
            finally:
                os.chdir(original_cwd)

    def test_missing_hook_function_raises_value_error(self):
        controller = self._controller()
        original_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "hooks.py").write_text(
                "def available(payload):\n    return payload\n"
            )
            self._write_config(
                tmpdir,
                [{"entrypoint": "hooks.py", "before_call": "missing"}],
            )
            os.chdir(tmpdir)
            try:
                with self.assertRaisesRegex(ValueError, "Hook function not found"):
                    LocalController._load_hooks(controller)
            finally:
                os.chdir(original_cwd)

    def test_hook_with_wrong_parameter_count_raises_value_error(self):
        controller = self._controller()
        original_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "hooks.py").write_text(
                "def invalid(first, second):\n    return first\n"
            )
            self._write_config(
                tmpdir,
                [{"entrypoint": "hooks.py", "before_call": "invalid"}],
            )
            os.chdir(tmpdir)
            try:
                with self.assertRaisesRegex(
                    ValueError, "Hook must accept exactly one argument"
                ):
                    LocalController._load_hooks(controller)
            finally:
                os.chdir(original_cwd)

    def test_multiple_hooks_run_in_configuration_order(self):
        controller = self._controller()
        original_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "first.py").write_text(
                "def before(args):\n"
                "    return {**args, 'name': args['name'] + '-first'}\n\n"
                "def after(result):\n"
                "    return result + '-first'\n"
            )
            Path(tmpdir, "second.py").write_text(
                "def before(args):\n"
                "    return {**args, 'name': args['name'] + '-second'}\n\n"
                "def after(result):\n"
                "    return result + '-second'\n"
            )
            self._write_config(
                tmpdir,
                [
                    {
                        "entrypoint": "first.py",
                        "before_call": "before",
                        "after_call": "after",
                    },
                    {
                        "entrypoint": "second.py",
                        "before_call": "before",
                        "after_call": "after",
                    },
                ],
            )
            os.chdir(tmpdir)
            try:
                controller._hooks = LocalController._load_hooks(controller)
                self._execute(controller, {"name": "start"})
            finally:
                os.chdir(original_cwd)

        self.assertEqual(
            controller.redis.hget("future:future-hooks", "result"),
            "hello start-first-second-first-second",
        )

    def test_none_return_keeps_the_current_payload(self):
        controller = self._controller()
        seen = []

        def observe_and_return_none(payload):
            seen.append(payload)
            return None

        controller._hooks = {
            "before_call": [observe_and_return_none],
            "after_call": [observe_and_return_none],
        }
        self._execute(controller, {"name": "world"})

        self.assertEqual(seen, [{"name": "world"}, "hello world"])
        self.assertEqual(
            controller.redis.hget("future:future-hooks", "result"), "hello world"
        )

    def test_hook_exception_marks_execution_failed(self):
        controller = self._controller()

        def boom(payload):
            raise RuntimeError("hook exploded")

        controller._hooks["before_call"] = [boom]
        self._execute(controller, {"name": "world"})

        self.assertEqual(
            controller.redis.hget("future:future-hooks", "error"), "hook exploded"
        )
        self.assertEqual(
            controller.redis.hget("future:future-hooks:metrics", "failed"), 1
        )

    def test_invalid_before_call_arguments_mark_execution_failed(self):
        calls = []

        def greet(name):
            calls.append(name)
            return "should not run"

        controller = self._controller(agent=SimpleNamespace(greet=greet))
        controller._hooks["before_call"] = [lambda args: {"unknown": "value"}]

        self._execute(controller, {"name": "world"})

        self.assertEqual(calls, [])
        self.assertEqual(
            controller.redis.hget("future:future-hooks:metrics", "failed"), 1
        )
        self.assertIn(
            "missing a required argument: 'name'",
            controller.redis.hget("future:future-hooks", "error"),
        )

    def test_execute_locally_loads_and_runs_configured_hooks(self):
        controller = self._controller()
        original_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "request_hooks.py").write_text(
                "def before_call(args):\n"
                "    return {**args, 'name': args['name'].upper()}\n\n"
                "def after_call(result):\n"
                "    return {'message': result, 'hooked': True}\n"
            )
            self._write_config(
                tmpdir,
                [
                    {
                        "entrypoint": "request_hooks.py",
                        "before_call": "before_call",
                        "after_call": "after_call",
                    }
                ],
            )
            os.chdir(tmpdir)
            try:
                controller._hooks = LocalController._load_hooks(controller)
                self._execute(controller, {"name": "world"})
            finally:
                os.chdir(original_cwd)

        self.assertEqual(
            json.loads(controller.redis.hget("future:future-hooks", "result")),
            {"message": "hello WORLD", "hooked": True},
        )
        self.assertEqual(
            controller.redis.hget("future:future-hooks:metrics", "failed"), 0
        )

    def test_real_local_controller_initialization_loads_hooks(self):
        redis = _FakeRedis()
        server = MagicMock()
        servicer = SimpleNamespace(request_queue=MagicMock())
        original_cwd = os.getcwd()

        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "agent.py").write_text(
                "class Greeter:\n"
                "    def greet(self, name):\n"
                "        return f'hello {name}'\n"
            )
            Path(tmpdir, "hooks.py").write_text(
                "def before(args):\n"
                "    return {**args, 'name': args['name'].upper()}\n"
            )
            self._write_config(
                tmpdir,
                [{"entrypoint": "hooks.py", "before_call": "before"}],
            )

            os.chdir(tmpdir)
            try:
                with (
                    patch.dict(
                        os.environ,
                        {
                            "VENTIS_AGENT_NAME": "Greeter",
                            "VENTIS_AGENT_FILE": "agent.py",
                            "VENTIS_AGENT_HOST": "test-host",
                            "VENTIS_AGENT_PORT": "50123",
                        },
                        clear=False,
                    ),
                    patch(
                        "ventis.controller.local_controller.start_server",
                        return_value=(server, servicer),
                    ),
                    patch(
                        "ventis.controller.local_controller.RedisClient",
                        return_value=redis,
                    ),
                    patch("ventis.controller.local_controller.threading.Thread.start"),
                ):
                    controller = LocalController(port=50123)
            finally:
                os.chdir(original_cwd)

        try:
            self.assertEqual(len(controller._hooks["before_call"]), 1)
            self.assertEqual(
                LocalController._run_hooks(
                    controller, "before_call", {"name": "world"}
                ),
                {"name": "WORLD"},
            )
            self.assertEqual(controller.agent.greet("WORLD"), "hello WORLD")
            self.assertEqual(redis.get("controller:test-host:50123:status"), "healthy")
        finally:
            controller._executor.shutdown(wait=False)


class RealRedisHookIntegrationTests(unittest.TestCase):
    """Opt-in integration test against a real Redis server.

    Run with VENTIS_TEST_REDIS=1. Host and port default to localhost:6379 and
    can be overridden with VENTIS_REDIS_HOST and VENTIS_REDIS_PORT.
    """

    @unittest.skipUnless(
        os.environ.get("VENTIS_TEST_REDIS") == "1",
        "set VENTIS_TEST_REDIS=1 to run the real Redis hook integration test",
    )
    def test_hook_execution_persists_to_real_redis(self):
        host = os.environ.get("VENTIS_REDIS_HOST", "localhost")
        port = int(os.environ.get("VENTIS_REDIS_PORT", "6379"))
        redis = RedisClient(host=host, port=port)
        redis.client.ping()

        suffix = uuid.uuid4().hex
        future_id = f"hook-integration-{suffix}"
        metrics_key = f"controller:hook-integration:{suffix}:metrics"
        controller = SimpleNamespace(
            redis=redis,
            agent=SimpleNamespace(greet=lambda name: f"hello {name}"),
            agent_name="Greeter",
            agent_id="real-redis-agent",
            _my_endpoint="localhost:50051",
            _metrics_key=metrics_key,
            _resolve_future_args=lambda args: args,
            _hooks={
                "before_call": [lambda args: {**args, "name": args["name"].upper()}],
                "after_call": [lambda result: {"message": result, "hooked": True}],
            },
        )
        controller._mark_future_failed = lambda current_future_id, error, origin=None: LocalController._mark_future_failed(
            controller, current_future_id, error, origin
        )

        keys = [f"future:{future_id}", f"future:{future_id}:metrics", metrics_key]
        try:
            with patch(
                "ventis.controller.local_controller.read_gpu_percent", return_value=0.0
            ):
                LocalController._execute_locally(
                    controller,
                    "Greeter",
                    "greet",
                    {"name": "redis"},
                    future_id,
                )

            self.assertEqual(
                json.loads(redis.hget(f"future:{future_id}", "result")),
                {"message": "hello REDIS", "hooked": True},
            )
            self.assertEqual(redis.hget(f"future:{future_id}:metrics", "failed"), "0")
        finally:
            redis.delete(*keys)


if __name__ == "__main__":
    unittest.main()
