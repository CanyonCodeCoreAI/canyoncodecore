import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(
    0,
    os.path.abspath(
        os.path.join(
            os.path.dirname(__file__), "..", "ventis", "templates", "grpc_stubs"
        )
    ),
)

import ventis.future as future_module
import ventis.ventis_context as ventis_context


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

    def sadd(self, name, *values):
        self.sets.setdefault(name, set()).update(values)


class FutureParentIdTests(unittest.TestCase):
    def setUp(self):
        self.fake_redis = _FakeRedis()
        self._orig_redis = future_module.Future.redis
        self._orig_stub = future_module.Future._stub
        future_module.Future.redis = self.fake_redis
        future_module.Future._stub = MagicMock()
        ventis_context.set_current_future_id("")

    def tearDown(self):
        future_module.Future.redis = self._orig_redis
        future_module.Future._stub = self._orig_stub
        ventis_context.set_current_future_id("")

    def test_parent_defaults_to_empty_when_no_future_executing(self):
        f = future_module.Future(
            parent="some/file.py", service="Svc", method="do_thing"
        )
        self.assertEqual(f.parent, "")
        self.assertEqual(self.fake_redis.hashes[f"future:{f.id}"]["parent"], "")

    def test_parent_is_the_currently_executing_future_id(self):
        ventis_context.set_current_future_id("caller-future-id")

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


if __name__ == "__main__":
    unittest.main()
