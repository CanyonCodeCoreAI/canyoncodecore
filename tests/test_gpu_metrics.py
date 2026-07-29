import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ventis.controller.utils.gpu_metrics import read_gpu_percent


class ReadGpuPercentTests(unittest.TestCase):
    def test_falls_back_to_zero_when_nvidia_smi_missing(self):
        with patch(
            "ventis.controller.utils.gpu_metrics.subprocess.run",
            side_effect=FileNotFoundError(),
        ):
            self.assertEqual(read_gpu_percent(), 0.0)

    def test_parses_nvidia_smi_output(self):
        fake_result = SimpleNamespace(returncode=0, stdout="42\n")
        with patch(
            "ventis.controller.utils.gpu_metrics.subprocess.run",
            return_value=fake_result,
        ):
            self.assertEqual(read_gpu_percent(), 42.0)

    def test_falls_back_on_nonzero_returncode(self):
        fake_result = SimpleNamespace(returncode=1, stdout="")
        with patch(
            "ventis.controller.utils.gpu_metrics.subprocess.run",
            return_value=fake_result,
        ):
            self.assertEqual(read_gpu_percent(), 0.0)


if __name__ == "__main__":
    unittest.main()
