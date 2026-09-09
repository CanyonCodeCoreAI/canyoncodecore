import os
import signal
import subprocess
import sys

from flask import Flask, jsonify, request

app = Flask("ventis-server")

# The project files are copied here (into a named volume) by `canyonos sync` /
# `canyonos deploy`. Deploy builds and launches against this path.
WORKSPACE_DIR = "/workspace"

_gc_process = None


def _gc_running():
    return _gc_process is not None and _gc_process.poll() is None


@app.route("/new-project", methods=["POST"])
def new_project():
    return jsonify({"error": "new-project runs locally via the CLI"}), 400


@app.route("/deploy", methods=["POST"])
def deploy():
    global _gc_process

    if _gc_running():
        return jsonify({"error": "already running"}), 409

    data = request.get_json(force=True, silent=True) or {}
    config_path = data.get("config_path", "config/global_controller.yaml")
    full_path = os.path.join(WORKSPACE_DIR, config_path)

    if not os.path.isfile(full_path):
        return jsonify({"error": f"config file not found: {full_path}"}), 400

    # `ventis deploy` builds (stubs/protos/images) then launches the Global
    # Controller. cwd is the workspace so build outputs land alongside the
    # project files and the controller finds them. Build+deploy output streams
    # to the container logs, which `canyonos deploy` tails.
    _gc_process = subprocess.Popen(
        [sys.executable, "-m", "ventis.cli", "deploy", "-c", config_path],
        cwd=WORKSPACE_DIR,
    )
    return jsonify({"status": "started", "pid": _gc_process.pid}), 200


@app.route("/clean", methods=["POST"])
def clean():
    global _gc_process

    if not _gc_running():
        return jsonify({"error": "not running"}), 409

    _gc_process.send_signal(signal.SIGTERM)
    _gc_process.wait()
    _gc_process = None
    return jsonify({"status": "stopped"}), 200


@app.route("/status", methods=["GET"])
def status():
    return jsonify({"running": _gc_running()}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
