import json
import unittest

from tests.support.runtime_fakes import FakeController
from tests.support.runtime_fakes import FakeRedis
from tests.support.runtime_fakes import make_instance
from ventis.controller.runtime_manager import RuntimeManager


class RoutingPublicationTests(unittest.TestCase):
    def setUp(self):
        self.controller = FakeController()
        self.manager = RuntimeManager(self.controller, self.controller.redis)

    def write_instance(self, instance):
        self.manager._write_instance(instance)
        self.controller.redis.sadd(
            f"agent:{instance['agent_name']}:instances",
            self.manager._instance_id_from_record(instance),
        )

    def test_publish_routing_snapshot_orders_endpoints_by_replica_index(self):
        self.write_instance(make_instance("Alpha", 1, host_port=8001))
        self.write_instance(make_instance("Alpha", 0, host_port=8000))

        self.manager.publish_routing_snapshot([{"name": "Alpha", "stateful": True}])

        self.assertEqual(
            json.loads(self.controller.redis.hget("routing_table:endpoints", "Alpha")),
            ["host.docker.internal:8000", "host.docker.internal:8001"],
        )
        self.assertEqual(self.controller.redis.hget("routing_table:stateful", "Alpha"), "true")
        self.assertEqual(self.controller.redis.smembers("routing_table:services"), {"Alpha"})

    def test_publish_routing_snapshot_removes_endpoint_when_no_instances_exist(self):
        redis = self.controller.redis
        redis.sadd("routing_table:services", "Alpha")
        redis.hset("routing_table:endpoints", "Alpha", json.dumps(["localhost:8000"]))

        self.manager.publish_routing_snapshot([{"name": "Alpha", "stateful": False}])

        self.assertEqual(redis.smembers("routing_table:services"), {"Alpha"})
        self.assertIsNone(redis.hget("routing_table:endpoints", "Alpha"))

    def test_publish_routing_snapshot_clears_stale_service_metadata(self):
        redis = self.controller.redis
        redis.sadd("routing_table:services", "Old", "Keep")
        redis.hset("routing_table:endpoints", "Old", json.dumps(["localhost:9000"]))
        redis.hset("routing_table:stateful", "Old", "true")
        redis.hset("routing_table:stateful", "Keep", "true")

        self.manager.publish_routing_snapshot([{"name": "Keep", "stateful": False}])

        self.assertEqual(redis.smembers("routing_table:services"), {"Keep"})
        self.assertIsNone(redis.hget("routing_table:endpoints", "Old"))
        self.assertIsNone(redis.hget("routing_table:stateful", "Old"))
        self.assertIsNone(redis.hget("routing_table:stateful", "Keep"))

    def test_publish_routing_snapshot_targets_each_node_redis(self):
        node_a = FakeRedis()
        node_b = FakeRedis()
        self.controller.node_redis = {"localhost": node_a, "127.0.0.1": node_b}
        self.write_instance(make_instance("Alpha", 0, host="localhost", host_port=8000))
        self.write_instance(make_instance("Beta", 0, host="127.0.0.1", host_port=8001))

        self.manager.publish_routing_snapshot(
            [
                {"name": "Alpha", "stateful": True},
                {"name": "Beta", "stateful": False},
            ]
        )

        for redis in (node_a, node_b):
            self.assertEqual(redis.smembers("routing_table:services"), {"Alpha", "Beta"})
            self.assertEqual(redis.hget("routing_table:stateful", "Alpha"), "true")
            self.assertEqual(
                json.loads(redis.hget("routing_table:endpoints", "Alpha")),
                ["host.docker.internal:8000"],
            )
            self.assertEqual(
                json.loads(redis.hget("routing_table:endpoints", "Beta")),
                ["host.docker.internal:8001"],
            )

        self.assertIsNone(self.controller.redis.hget("routing_table:endpoints", "Alpha"))

    def test_routing_targets_fall_back_to_central_redis(self):
        self.assertEqual(self.manager._routing_redis_targets(), [self.controller.redis])

    def test_routing_targets_use_node_redis_when_present(self):
        node = FakeRedis()
        self.controller.node_redis = {"localhost": node}

        self.assertEqual(self.manager._routing_redis_targets(), [node])

    def test_publish_policy_rules_writes_to_all_targets(self):
        self.controller.node_redis = {"localhost": FakeRedis(), "other": FakeRedis()}
        rules = [{"match": {"role": "admin"}, "access": "all"}]

        count = self.manager.publish_policy_rules(rules)

        self.assertEqual(count, 2)
        for redis in self.controller.node_redis.values():
            self.assertEqual(json.loads(redis.get("policy:rules")), rules)

    def test_publish_policy_rules_writes_empty_rule_list(self):
        count = self.manager.publish_policy_rules([])

        self.assertEqual(count, 1)
        self.assertEqual(json.loads(self.controller.redis.get("policy:rules")), [])


if __name__ == "__main__":
    unittest.main()
