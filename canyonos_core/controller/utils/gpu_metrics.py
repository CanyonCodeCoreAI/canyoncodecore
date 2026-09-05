"""GPU utilization reporting via nvidia-smi."""
# Disclaimer, only works for NVIDIA (obviously but wanted to make note)

import subprocess


def read_gpu_percent():
    """Return current GPU utilization percent; 0.0 if unavailable."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode != 0:
            return 0.0
        first_line = result.stdout.strip().splitlines()[0]
        return float(first_line)
    except (
        FileNotFoundError,
        subprocess.SubprocessError,
        ValueError,
        IndexError,
    ):
        return 0.0
