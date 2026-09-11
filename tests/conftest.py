"""Compiles the gRPC stubs canyonos_core imports at module load time.

`local_controler_pb2` / `local_controler_pb2_grpc` are never checked in --
`canyonos build` generates them per deployment into `.car/grpc_stubs` (see
`canyonos_core/cli.py`). Importing `global_controller` or `local_controller`
from a plain source checkout needs the same modules importable, so compile
them once per test session into a scratch directory and put it on `sys.path`
before any test module imports canyonos_core's controller package.
"""

import os
import subprocess
import sys
import tempfile

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROTO_DIR = os.path.join(_REPO_ROOT, "canyonos_core", "controller", "proto")
_STUBS_DIR = tempfile.mkdtemp(prefix="canyonos-test-grpc-stubs-")

for _proto_file in sorted(os.listdir(_PROTO_DIR)):
    if not _proto_file.endswith(".proto"):
        continue
    subprocess.run(
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            f"-I{_PROTO_DIR}",
            f"--python_out={_STUBS_DIR}",
            f"--grpc_python_out={_STUBS_DIR}",
            os.path.join(_PROTO_DIR, _proto_file),
        ],
        check=True,
    )

sys.path.insert(0, _STUBS_DIR)
