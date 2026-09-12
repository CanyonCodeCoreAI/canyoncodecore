"""GPU utilization reporting via nvidia-smi."""
# Disclaimer, only works for NVIDIA (obviously but wanted to make note)
# NVIDIA-smi is inaccurate, will need to change it in the future
# Currently, read_gpu_percent() is meant for LC to read the GPU that it is taking up, while read_gpu_metrics() is meant for the instance metrics collector for the GPU util by the whole machine.

import subprocess

_MIB_TO_BYTES = 1024 * 1024


def read_gpu_metrics():
    """Return (utilization_percent, memory_used_bytes, memory_free_bytes) for GPU 0.

    All zero if nvidia-smi is unavailable or errors. Only the first GPU is read -- this
    is a deliberately basic single-GPU reporter.
    """
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,memory.used,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode != 0:
            return 0.0, 0, 0
        first_line = result.stdout.strip().splitlines()[0]
        util, mem_used_mib, mem_free_mib = (
            part.strip() for part in first_line.split(",")
        )
        return (
            float(util),
            int(float(mem_used_mib)) * _MIB_TO_BYTES,
            int(float(mem_free_mib)) * _MIB_TO_BYTES,
        )
    except (
        FileNotFoundError,
        subprocess.SubprocessError,
        ValueError,
        IndexError,
    ):
        return 0.0, 0, 0


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
