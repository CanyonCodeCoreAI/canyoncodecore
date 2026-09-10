import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(
    0,
    os.path.abspath(
        os.path.join(
            os.path.dirname(__file__), "..", "canyonos_core", "templates", "grpc_stubs"
        )
    ),
)

import canyonos_core.controller.future as future_module
import canyonos_core.controller.canyonos_context as canyonos_context
from canyonos_core.controller.local_controller import LocalController


class _FakeRedis:
    def __init__(self):
        self.hashes = {}
        self.sets = {}

    def hset_multiple(self, name, mapping):
        self.hashes.setdefault(name, {}).update(mapping)

    def hset(self, name, field, value):
        self.hashes.setdefault(name, {})[field] = value

    def hget(self, name, field):
        return self.hashes.get(name, {}).get(field)

    def hgetall(self, name):
        return dict(self.hashes.get(name, {}))

    def sadd(self, name, *values):
        self.sets.setdefault(name, set()).update(values)


class FutureParentIdTests(unittest.TestCase):
    def setUp(self):
        self.fake_redis = _FakeRedis()
        self._orig_redis = future_module.Future.redis
        self._orig_stub = future_module.Future._stub
        future_module.Future.redis = self.fake_redis
        future_module.Future._stub = MagicMock()
        canyonos_context.set_current_future_id("")

    def tearDown(self):
        future_module.Future.redis = self._orig_redis
        future_module.Future._stub = self._orig_stub
        canyonos_context.set_current_future_id("")

    def test_parent_defaults_to_empty_when_no_future_executing(self):
        f = future_module.Future(
            parent="some/file.py", service="Svc", method="do_thing"
        )
        self.assertEqual(f.parent, "")
        self.assertEqual(self.fake_redis.hashes[f"future:{f.id}"]["parent"], "")

    def test_parent_is_the_currently_executing_future_id(self):
        canyonos_context.set_current_future_id("caller-future-id")

        f = future_module.Future(
            parent="ignored/file.py", service="Svc", method="do_thing"
        )

        self.assertEqual(f.parent, "caller-future-id")
        self.assertEqual(
            self.fake_redis.hashes[f"future:{f.id}"]["parent"], "caller-future-id"
        )

    def test_submission_failure_is_raised_by_value_not_constructor(self):
        future_module.Future._stub.Execute.side_effect = RuntimeError("submit failed")

        future = future_module.Future(
            parent="ignored/file.py", service="Svc", method="do_thing"
        )

        with self.assertRaisesRegex(RuntimeError, "submit failed"):
            future.value()


class FutureIdArgDetectionTests(unittest.TestCase):
    """A Future handed to another stub call travels as its bare id string --
    stub_generator emits `x.id if isinstance(x, Future) else x` -- so the width
    Future generates has to be the width the controller's detection accepts.
    When the two drifted apart the id was forwarded to the agent as a literal
    string instead of being awaited, with no error raised anywhere.
    """

    def setUp(self):
        self.fake_redis = _FakeRedis()
        self._orig_redis = future_module.Future.redis
        self._orig_stub = future_module.Future._stub
        future_module.Future.redis = self.fake_redis
        future_module.Future._stub = MagicMock()
        canyonos_context.set_current_future_id("")

    def tearDown(self):
        future_module.Future.redis = self._orig_redis
        future_module.Future._stub = self._orig_stub
        canyonos_context.set_current_future_id("")

    def _make_future(self, method):
        return future_module.Future(
            parent="some/file.py", service="Svc", method=method
        )

    def test_generated_id_is_accepted_by_the_controller_detection(self):
        future = self._make_future("produce")

        self.assertTrue(canyonos_context.looks_like_future_id(future.id))

    def test_future_passed_as_arg_is_awaited_instead_of_forwarded_raw(self):
        producer = self._make_future("produce")
        self.fake_redis.hset(f"future:{producer.id}", "result", "42")
        args = {
            "ticker": producer.id if isinstance(producer, future_module.Future) else producer,
            "currency": "USD",
        }
        controller = SimpleNamespace(redis=self.fake_redis)

        resolved = LocalController._resolve_future_args(controller, args)

        self.assertEqual(resolved, {"ticker": "42", "currency": "USD"})


if __name__ == "__main__":
    unittest.main()
