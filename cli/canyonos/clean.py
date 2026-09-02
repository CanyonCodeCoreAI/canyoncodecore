"""
Remove generated stubs, gRPC files, and Docker build contexts.

Ported directly over from canyonos, moving the logic into here.
"""

import os
import shutil


def run_clean():
    project_dir = os.getcwd()

    paths_to_clean = [
        os.path.join(project_dir, "stubs"),
        os.path.join(project_dir, "grpc_stubs"),
        os.path.join(project_dir, "docker_container"),
    ]

    for path in paths_to_clean:
        if os.path.exists(path):
            print(f"Cleaning {path}...")
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)

    print("Clean complete.")
