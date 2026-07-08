import requests
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch


def run_integration_test():
    base_url = "http://localhost:8080"
    print(f"Submitting query to {base_url}/main...")

    response = requests.post(f"{base_url}/main", json={"ticker": "MSFT"})

    if response.status_code != 202:
        print(f"Error submitting request: HTTP {response.status_code}")
        print(response.text)
        sys.exit(1)

    data = response.json()
    req_id = data.get("request_id")
    print(f"Got Request ID: {req_id}")

    max_wait = 30
    elapsed = 0

    while elapsed < max_wait:
        status_res = requests.get(f"{base_url}/status/{req_id}").json()
        status = status_res.get("status")

        if status == "done":
            result = status_res.get("result", {})
            print(f"\nWorkflow Completed! Result: {result}")

            # Validation assertions
            assert "MSFT" in result.get("company_name", ""), "Missing expected company name."
            assert "This is an LLM generated response to" in result.get("competitors", ""), "VllmAgent response formatting missing."
            assert result.get("stock_price") == 100.0, "FinanceAgent did not return 100.0"

            print("\nIntegration test passed. All validations successful.")
            sys.exit(0)

        if status == "error":
            print(f"Workflow hit an error: {status_res.get('error')}")
            sys.exit(1)

        print(f"Status: {status} ... waiting")
        time.sleep(1)
        elapsed += 1

    print(f"Timed out after {max_wait}s waiting for workflow completion.")
    sys.exit(1)


if __name__ == "__main__":
    run_integration_test()


class IntegrationScriptTests(unittest.TestCase):
    def test_run_integration_test_exits_zero_on_expected_result(self):
        submit_response = SimpleNamespace(
            status_code=202,
            json=lambda: {"request_id": "req-1"},
        )
        status_response = SimpleNamespace(
            json=lambda: {
                "status": "done",
                "result": {
                    "company_name": "MSFT Corp",
                    "competitors": "This is an LLM generated response to competitors",
                    "stock_price": 100.0,
                },
            }
        )

        with patch(f"{__name__}.requests.post", return_value=submit_response):
            with patch(f"{__name__}.requests.get", return_value=status_response):
                with self.assertRaises(SystemExit) as raised:
                    run_integration_test()

        self.assertEqual(raised.exception.code, 0)

    def test_run_integration_test_exits_one_on_submit_failure(self):
        submit_response = SimpleNamespace(status_code=500, text="boom")

        with patch(f"{__name__}.requests.post", return_value=submit_response):
            with self.assertRaises(SystemExit) as raised:
                run_integration_test()

        self.assertEqual(raised.exception.code, 1)

    def test_run_integration_test_exits_one_on_workflow_error(self):
        submit_response = SimpleNamespace(
            status_code=202,
            json=lambda: {"request_id": "req-1"},
        )
        status_response = SimpleNamespace(json=lambda: {"status": "error", "error": "boom"})

        with patch(f"{__name__}.requests.post", return_value=submit_response):
            with patch(f"{__name__}.requests.get", return_value=status_response):
                with self.assertRaises(SystemExit) as raised:
                    run_integration_test()

        self.assertEqual(raised.exception.code, 1)

    def test_run_integration_test_times_out(self):
        submit_response = SimpleNamespace(
            status_code=202,
            json=lambda: {"request_id": "req-1"},
        )
        status_response = SimpleNamespace(json=lambda: {"status": "running"})

        with patch(f"{__name__}.requests.post", return_value=submit_response):
            with patch(f"{__name__}.requests.get", return_value=status_response):
                with patch(f"{__name__}.time.sleep", return_value=None):
                    with self.assertRaises(SystemExit) as raised:
                        run_integration_test()

        self.assertEqual(raised.exception.code, 1)
