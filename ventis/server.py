import os
import signal
import subprocess
import sys

import yaml
from flask import Flask, jsonify, request

from ventis.cli import _artifact_prefix
from ventis.controller.utils.redis_client import RedisClient

app = Flask("ventis-server")

# The project files are copied here (into a named volume) by `canyonos sync` /
# `canyonos deploy`. Deploy builds and launches against this path.
WORKSPACE_DIR = "/workspace"

DEFAULT_API_PORT = 8080

_gc_process = None
_config_path = None


def _gc_running():
    return _gc_process is not None and _gc_process.poll() is None


@app.route("/new-project", methods=["POST"])
def new_project():
    return jsonify({"error": "new-project runs locally via the CLI"}), 400


@app.route("/deploy", methods=["POST"])
def deploy():
    global _gc_process, _config_path

    if _gc_running():
        return jsonify({"error": "already running"}), 409

    data = request.get_json(force=True, silent=True) or {}
    # Resolved with ventis' own artifact-layout rule rather than a second copy
    # of it, so a `.car` project works when the client sends no config_path.
    config_path = data.get("config_path") or os.path.join(
        _artifact_prefix(WORKSPACE_DIR), "config", "global_controller.yaml"
    )
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
    _config_path = full_path
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


def _primary_redis(config):
    """The Redis the controller writes instance records to: the local node's.

    Mirrors GlobalController._launch_redis_containers(), where a localhost node
    is reached through VENTIS_REDIS_HOST when the controller is containerized.
    """
    redis_cfg = config.get("redis", {})
    host = redis_cfg.get("host", "localhost")
    port = redis_cfg.get("port", 6379)
    for agent in config.get("agents") or []:
        if str(agent.get("provider", "local")).lower() == "local":
            port = agent.get("redis_port", port)
            break
    if host in ("localhost", "127.0.0.1"):
        host = os.environ.get("VENTIS_REDIS_HOST", host)
    return RedisClient(host=host, port=int(port))


def _workflow_endpoints(config):
    """Address of every running workflow replica, as the caller should reach it."""
    ports = {
        agent["name"]: agent.get("api_port", DEFAULT_API_PORT)
        for agent in config.get("agents") or []
        if agent.get("type") == "workflow" and agent.get("name")
    }
    if not ports:
        return []

    redis_client = _primary_redis(config)
    endpoints = []
    for key in sorted(redis_client.scan_keys("agent_instance:*")):
        record = redis_client.hgetall(key)
        name = record.get("agent_name")
        if name not in ports:
            continue
        # public_host wins: `host` is the address the controller routes over,
        # which for a workflow on another machine is private to that network.
        host = record.get("public_host") or record.get("host")
        if not host:
            continue
        endpoints.append(
            {
                "name": name,
                "host": host,
                "port": int(record.get("api_port") or ports[name]),
            }
        )
    return endpoints


@app.route("/endpoints", methods=["GET"])
def endpoints():
    """Where the deployed workflows answer, so the CLI can print real addresses."""
    if _config_path is None or not os.path.isfile(_config_path):
        return jsonify({"workflows": []}), 200

    try:
        with open(_config_path) as f:
            config = yaml.safe_load(f) or {}
        return jsonify({"workflows": _workflow_endpoints(config)}), 200
    except Exception as e:
        return jsonify({"workflows": [], "error": str(e)}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
