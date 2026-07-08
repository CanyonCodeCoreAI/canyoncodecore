import time


def _wait_for_redis(redis_client, host, port, timeout=30, interval=1):
    """Wait until Redis accepts commands, surfacing network issues clearly."""
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            redis_client.set("__ventis_redis_healthcheck__", "ok")
            return
        except Exception as exc:
            last_error = exc
            time.sleep(interval)

    raise TimeoutError(
        f"Timed out connecting to Redis at {host}:{port}. "
        "For EC2 runtimes, ensure the instance security group allows inbound "
        f"TCP {port} from the global controller host."
    ) from last_error
