"""Compile the controller protos before collection.

canyonos_core.controller bare-imports local_controler_pb2. The container
images put protoc's output on sys.path; a checkout has to generate it.
"""

import shutil
import subprocess
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROTO_DIR = PACKAGE_ROOT / "canyonos_core" / "controller" / "proto"
STUB_DIR = PACKAGE_ROOT / "build" / "pb"


def pytest_configure(config):
    shutil.rmtree(STUB_DIR, ignore_errors=True)
    STUB_DIR.mkdir(parents=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            f"-I{PROTO_DIR}",
            f"--python_out={STUB_DIR}",
            f"--grpc_python_out={STUB_DIR}",
            *sorted(str(p) for p in PROTO_DIR.glob("*.proto")),
        ],
        check=True,
    )
    sys.path.insert(0, str(STUB_DIR))
