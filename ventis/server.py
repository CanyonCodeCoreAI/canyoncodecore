import os
import signal
import subprocess
import sys
import tempfile

from flask import Flask, jsonify, request

app = Flask("ventis-server")

_gc_process = None


def _gc_running():
    return _gc_process is not None and _gc_process.poll() is None


@app.route("/new-project", methods=["POST"])
def new_project():
    return jsonify({"error": "new-project runs locally via the CLI"}), 400


@app.route("/build", methods=["POST"])
def build():
    return jsonify({"error": "build runs locally via the CLI"}), 400


@app.route("/deploy", methods=["POST"])
def deploy():
    global _gc_process

    if _gc_running():
        return jsonify({"error": "already running"}), 409

    data = request.get_json(force=True, silent=True) or {}
    config = data.get("config")
    if not config:
        return jsonify({"error": "config is required"}), 400

    tmp = tempfile.mkdtemp()
    config_path = os.path.join(tmp, "global_controller.yaml")
    with open(config_path, "w") as f:
        f.write(config)

    if data.get("policy"):
        os.makedirs(os.path.join(tmp, "config"), exist_ok=True)
        with open(os.path.join(tmp, "policy.yaml"), "w") as f:
            f.write(data["policy"])

    if data.get("env"):
        env_path = os.path.join(tmp, ".env")
        with open(env_path, "w") as f:
            f.write(data["env"])

    _gc_process = subprocess.Popen(
        [sys.executable, "-m", "ventis.controller.global_controller", "-c", config_path]
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
