import argparse
import concurrent.futures
import time
import sys
import requests
import statistics
import unittest
from types import SimpleNamespace
from unittest.mock import patch


def dispatch_request(session, base_url, payload):
    start_time = time.time()
    try:
        response = session.post(f"{base_url}/main", json=payload, timeout=5)
        response.raise_for_status()
        req_id = response.json().get("request_id")
        return {"status": "dispatched", "req_id": req_id, "latency": time.time() - start_time}
    except Exception as e:
        return {"status": "error", "error": str(e), "latency": time.time() - start_time}


def poll_request(session, base_url, req_id):
    start_time = time.time()
    while True:
        try:
            res = session.get(f"{base_url}/status/{req_id}", timeout=5).json()
            if res.get("status") in ["done", "error"]:
                return {"status": res["status"], "latency": time.time() - start_time}
        except Exception as e:
            return {"status": "error", "error": str(e), "latency": time.time() - start_time}
        time.sleep(0.5)


def run_performance_test(concurrent_users, total_requests):
    base_url = "http://localhost:8080"
    print(f"Starting Performance Test: {total_requests} total requests across {concurrent_users} workers.")

    session = requests.Session()

    dispatch_results = []

    # Phase 1: Dispatch barrage
    print("\n[Phase 1] Queuing requests...")
    global_start = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrent_users) as executor:
        futures = [executor.submit(dispatch_request, session, base_url, {"ticker": f"TICK{i}"}) for i in range(total_requests)]
        for future in concurrent.futures.as_completed(futures):
            dispatch_results.append(future.result())

    dispatch_success = [r for r in dispatch_results if r["status"] == "dispatched"]
    print(f"Successfully queued {len(dispatch_success)} / {total_requests} requests.")

    # Phase 2: Poll till completion
    print("\n[Phase 2] Waiting for resolution...")
    poll_results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrent_users) as executor:
        futures = [executor.submit(poll_request, session, base_url, r["req_id"]) for r in dispatch_success]
        for future in concurrent.futures.as_completed(futures):
            poll_results.append(future.result())

    global_time = time.time() - global_start

    # Analysis
    completed = [r for r in poll_results if r["status"] == "done"]
    failed = [r for r in poll_results if r["status"] == "error"]

    if completed:
        latencies = [r["latency"] for r in completed]
        avg_lat = statistics.mean(latencies)
        p95_lat = statistics.quantiles(latencies, n=20)[18] if len(latencies) >= 20 else max(latencies)

        print("\n--- Analytics Report ---")
        print(f"Total Time Elapsed:     {global_time:.2f} s")
        print(f"Completed Successfully: {len(completed)}")
        print(f"Failed / Errors:        {len(failed) + (total_requests - len(dispatch_success))}")
        print(f"Throughput (RPS):       {len(completed) / global_time:.2f} req/s")
        print(f"Avg End-to-End Latency: {avg_lat:.2f} s")
        print(f"P95 End-to-End Latency: {p95_lat:.2f} s")
        print("------------------------\n")

        if len(failed) > 0:
            sys.exit(1)
        sys.exit(0)
    else:
        print("\nAll requests failed. Performance cannot be calculated.")
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrent", type=int, default=10, help="Number of parallel workers")
    parser.add_argument("--total", type=int, default=50, help="Total number of requests to generate")
    args = parser.parse_args()

    run_performance_test(args.concurrent, args.total)


class PerformanceScriptTests(unittest.TestCase):
    def test_dispatch_request_returns_request_id_on_success(self):
        response = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"request_id": "req-1"},
        )
        session = SimpleNamespace(post=lambda *args, **kwargs: response)

        result = dispatch_request(session, "http://test", {"ticker": "MSFT"})

        self.assertEqual(result["status"], "dispatched")
        self.assertEqual(result["req_id"], "req-1")

    def test_dispatch_request_returns_error_on_exception(self):
        session = SimpleNamespace(post=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

        result = dispatch_request(session, "http://test", {"ticker": "MSFT"})

        self.assertEqual(result["status"], "error")
        self.assertIn("boom", result["error"])

    def test_poll_request_returns_done_status(self):
        response = SimpleNamespace(json=lambda: {"status": "done"})
        session = SimpleNamespace(get=lambda *args, **kwargs: response)

        result = poll_request(session, "http://test", "req-1")

        self.assertEqual(result["status"], "done")

    def test_poll_request_returns_error_on_exception(self):
        session = SimpleNamespace(get=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

        result = poll_request(session, "http://test", "req-1")

        self.assertEqual(result["status"], "error")
        self.assertIn("boom", result["error"])

    def test_run_performance_test_exits_zero_when_all_requests_complete(self):
        class Session:
            def post(self, *args, **kwargs):
                return SimpleNamespace(
                    raise_for_status=lambda: None,
                    json=lambda: {"request_id": "req-1"},
                )

            def get(self, *args, **kwargs):
                return SimpleNamespace(json=lambda: {"status": "done"})

        with patch(f"{__name__}.requests.Session", return_value=Session()):
            with self.assertRaises(SystemExit) as raised:
                run_performance_test(concurrent_users=1, total_requests=1)

        self.assertEqual(raised.exception.code, 0)

    def test_run_performance_test_exits_one_when_dispatch_fails(self):
        class Session:
            def post(self, *args, **kwargs):
                raise RuntimeError("boom")

        with patch(f"{__name__}.requests.Session", return_value=Session()):
            with self.assertRaises(SystemExit) as raised:
                run_performance_test(concurrent_users=1, total_requests=1)

        self.assertEqual(raised.exception.code, 1)
