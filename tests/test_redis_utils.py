import unittest
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ventis.controller.utils.redis_utils import _wait_for_redis


class WaitForRedisTests(unittest.TestCase):
    @patch("ventis.controller.utils.redis_utils.time.sleep")
    @patch("ventis.controller.utils.redis_utils.time.time", return_value=0)
    def test_timeout_message_stays_the_same(self, mock_time, mock_sleep):
        redis_client = MagicMock()

        with self.assertRaisesRegex(
            TimeoutError,
            r"Timed out connecting to Redis at 10\.0\.0\.1:6379\. "
            r"For EC2 runtimes, ensure the instance security group allows inbound "
            r"TCP 6379 from the global controller host\.",
        ):
            _wait_for_redis(redis_client, "10.0.0.1", 6379, timeout=0, interval=1)

        redis_client.set.assert_not_called()
        mock_sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
