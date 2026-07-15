"""Opt-in live EC2 test for the Ventis template.

This module intentionally has no pytest dependency beyond what the repository
already uses.  It is normally run directly through ``run_ec2_e2e.sh``.
"""

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TIMEOUT = int(os.environ.get("VENTIS_E2E_TIMEOUT", "900"))


def settings():
    names = (
        "host",
        "user",
        "key",
        "worker_key",
        "region",
        "ami_id",
        "subnet_id",
        "security_group_ids",
        "ssh_user",
    )
    env_names = {
        "host": "VENTIS_CONTROLLER_HOST",
        "user": "VENTIS_CONTROLLER_USER",
        "key": "VENTIS_CONTROLLER_PRIVATE_KEY",
        "worker_key": "VENTIS_WORKER_PRIVATE_KEY",
        "region": "VENTIS_AWS_REGION",
        "ami_id": "VENTIS_AMI_ID",
        "subnet_id": "VENTIS_SUBNET_ID",
        "security_group_ids": "VENTIS_SECURITY_GROUP_IDS",
        "ssh_user": "VENTIS_WORKER_SSH_USER",
    }
    result = {key: os.environ.get(env_names[key]) for key in names}
    missing = [key for key, value in result.items() if not value]
    if missing:
        raise RuntimeError("Missing launcher settings: " + ", ".join(missing))
    result["security_group_ids"] = [x.strip() for x in result["security_group_ids"].split(",") if x.strip()]
    if not result["security_group_ids"]:
        raise RuntimeError("VENTIS_SECURITY_GROUP_IDS must not be empty")
    result["key"] = os.path.expanduser(result["key"])
    if not Path(result["key"]).is_file():
        raise RuntimeError(f"Controller private key does not exist: {result['key']}")
    return result


class Remote:
    def __init__(self, cfg):
        self.base = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
                     "-i", cfg["key"], f"{cfg['user']}@{cfg['host']}"]

    def run(self, command, check=True, timeout=120):
        try:
            return subprocess.run(
                self.base + [command],
                text=True,
                capture_output=True,
                check=check,
                timeout=timeout,
            )
        except subprocess.CalledProcessError as exc:
            details = (exc.stderr or exc.stdout or "").strip()
            raise RuntimeError(
                f"Remote command failed with exit status {exc.returncode}: {command}\n"
                f"{details}"
            ) from exc

    def output(self, command):
        return self.run(command).stdout.strip()


def install_worker_key(remote, cfg):
    """Install the worker key where the EC2 runtime expects it."""
    worker_key = Path(os.path.expanduser(cfg["worker_key"]))
    if not worker_key.is_file():
        raise RuntimeError(f"Worker private key does not exist: {worker_key}")

    remote.run("mkdir -p ~/.ssh && chmod 700 ~/.ssh")
    target = f"{cfg['user']}@{cfg['host']}:~/.ssh/ventis_ec2"
    result = subprocess.run(
        [
            "scp",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=no",
            "-i",
            cfg["key"],
            str(worker_key),
            target,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"Could not install worker key: {details}")
    remote.run("chmod 600 ~/.ssh/ventis_ec2")


def wait_until(
    predicate,
    description,
    timeout=TIMEOUT,
    interval=5,
    fatal_exceptions=(),
):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            value = predicate()
            if value:
                return value
            last = value
        except Exception as exc:  # transient AWS/SSH availability is expected
            if fatal_exceptions and isinstance(exc, fatal_exceptions):
                raise
            last = exc
        time.sleep(interval)
    raise AssertionError(f"Timed out waiting for {description}: {last}")


def records(remote, project):
    code = ("import json,redis; r=redis.Redis(host='127.0.0.1',port=6379,decode_responses=True); "
            "print(json.dumps([r.hgetall(k) for k in r.scan_iter('agent_instance:*')]))")
    command = (
        f"cd {shlex.quote(project)} && source ../.venv/bin/activate && "
        f"python -c {shlex.quote(code)}"
    )
    return json.loads(remote.output(command))


def _tail_log(remote, log_path, lines=200):
    result = remote.run(f"test -f {shlex.quote(log_path)} && tail -n {lines} {shlex.quote(log_path)} || true", check=False)
    return (result.stdout or result.stderr or "").strip()


def _deploy_exited(remote, deploy_pid):
    return remote.run(f"kill -0 {deploy_pid}", check=False).returncode != 0


def main():
    if os.environ.get("VENTIS_E2E_ENABLED") != "1":
        print("EC2 E2E skipped (set VENTIS_E2E_ENABLED=1 to enable).")
        return 0
    cfg = settings()
    remote = Remote(cfg)
    temp = f"/tmp/ventis-ec2-e2e-{os.getpid()}-{int(time.time())}"
    deploy_pid = None
    captured = set()
    try:
        print("[1/8] Logging into Global Controller and checking prerequisites...", flush=True)
        remote.run("command -v python3 && command -v docker && docker info >/dev/null && python3 -c 'import boto3; print(boto3.client(\"sts\").get_caller_identity()[\"Arn\"])'")
        print("  Global Controller login and prerequisites succeeded.", flush=True)

        print("[2/8] Staging current source on Global Controller...", flush=True)
        remote.run(f"rm -rf {shlex.quote(temp)} && mkdir -p {shlex.quote(temp)}")
        # A virtualenv is machine-specific.  In particular, macOS/uv virtualenvs
        # contain symlinks to the local interpreter, which are invalid on the
        # Linux controller and can make ``.venv/bin/python3`` disappear.
        archive = subprocess.Popen(
            ["tar", "-czf", "-", "--exclude=.git", "--exclude=.venv", "."],
            cwd=ROOT,
            stdout=subprocess.PIPE,
        )
        ssh = subprocess.Popen(remote.base + [f"tar -xzf - -C {shlex.quote(temp)}"], stdin=archive.stdout,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False)
        archive.stdout.close(); _, err = ssh.communicate(timeout=120); archive.wait()
        if ssh.returncode or archive.returncode:
            raise RuntimeError(f"Could not stage source: {err.decode(errors='replace')}")
        print("  Source staging succeeded.", flush=True)

        print("[3/8] Creating virtual environment and installing Ventis...", flush=True)
        project = f"{temp}/project"
        remote.run(
            f"cd {shlex.quote(temp)} && rm -rf .venv && python3 -m venv .venv && "
            ". .venv/bin/activate && pip install -q -e . && "
            "mkdir -p project && cp -R ventis/templates/. project/"
        )
        print("  Virtual environment and editable install succeeded.", flush=True)
        config_code = (
            "from pathlib import Path\n"
            "import yaml\n"
            f"path = Path({project + '/config/global_controller.yaml'!r})\n"
            "config = yaml.safe_load(path.read_text())\n"
            "config['ec2'].update({\n"
            f"    'region': {cfg['region']!r},\n"
            f"    'ami_id': {cfg['ami_id']!r},\n"
            f"    'subnet_id': {cfg['subnet_id']!r},\n"
            f"    'security_group_ids': {cfg['security_group_ids']!r},\n"
            f"    'ssh_user': {cfg['ssh_user']!r},\n"
            "})\n"
            "path.write_text(yaml.safe_dump(config, sort_keys=False))\n"
        )
        remote.run(
            f"cd {shlex.quote(project)} && ../.venv/bin/python -c "
            + shlex.quote(config_code)
        )
        print("[4/8] Running ventis build...", flush=True)
        remote.run(f"cd {shlex.quote(project)} && ../.venv/bin/python -m ventis.cli build", timeout=TIMEOUT)
        remote.run(f"cd {shlex.quote(project)} && docker image inspect ventis-exampleagent ventis-workflow >/dev/null")
        print("  ventis build succeeded and both images exist.", flush=True)

        print("[5/8] Starting ventis deploy on Global Controller...", flush=True)
        install_worker_key(remote, cfg)
        print("  Worker private key installed as ~/.ssh/ventis_ec2.", flush=True)
        log = f"{temp}/deploy.log"
        worker_key = cfg["worker_key"]
        remote_worker_key = (
            worker_key.replace("~/", "$HOME/", 1)
            if worker_key.startswith("~/")
            else shlex.quote(worker_key)
        )
        proc = remote.run(
            f"cd {shlex.quote(project)} && export VENTIS_EC2_SSH_KEY="
            f"{remote_worker_key} && "
            "nohup ../.venv/bin/python -m ventis.cli deploy "
            f"</dev/null >{shlex.quote(log)} 2>&1 & deploy_pid=$!; "
            "printf '%s\\n' \"$deploy_pid\"",
            timeout=300,
        )
        deploy_pid = int(proc.stdout.strip().splitlines()[-1])
        print(f"  ventis deploy started (PID {deploy_pid}).", flush=True)

        def ready():
            if _deploy_exited(remote, deploy_pid):
                raise RuntimeError(
                    "ventis deploy exited before the EC2 instances became ready:\n"
                    + _tail_log(remote, log)
                )
            found = records(remote, project)
            selected = [x for x in found if x.get("agent_name") in {"ExampleAgent", "Workflow"}]
            if len(selected) == 2:
                captured.update(x["ec2_instance_id"] for x in selected); return selected
            return False
        selected = wait_until(
            ready,
            "both EC2 service records",
            fatal_exceptions=(RuntimeError,),
        )
        print("  ExampleAgent and Workflow EC2 instances are ready.", flush=True)
        agent = next(x for x in selected if x["agent_name"] == "ExampleAgent")
        workflow = next(x for x in selected if x["agent_name"] == "Workflow")
        remote.run(
            f"ssh -o StrictHostKeyChecking=no -i {remote_worker_key} "
            f"{shlex.quote(cfg['ssh_user'])}@{shlex.quote(agent['host'])} "
            "'sudo docker ps --format {{.Names}}' | grep -q ventis-ec2-exampleagent"
        )
        print("[6/8] Verified ExampleAgent container on an EC2 worker.", flush=True)
        workflow_url = f"http://{workflow['host']}:8080"
        response = remote.output(f"curl -fsS -X POST {shlex.quote(workflow_url + '/main')} -H 'Content-Type: application/json' -d '{{\"name\":\"World\"}}'")
        request_id = json.loads(response)["request_id"]
        def done():
            data = json.loads(remote.output(f"curl -fsS {shlex.quote(workflow_url + '/status/' + request_id)}"))
            return data if data.get("status") in {"done", "error"} else False
        result = wait_until(done, "workflow completion", timeout=180)
        assert result.get("status") == "done" and result.get("result") == {"greeting": "Hello, World! I'm the ExampleAgent."}, result
        print("[7/8] Workflow completed with the expected greeting.", flush=True)
    finally:
        if deploy_pid:
            print("[8/8] Stopping deploy and cleaning up Ventis containers...", flush=True)
            remote.run(f"kill -TERM {deploy_pid}", check=False)
            wait_until(lambda: remote.run(f"kill -0 {deploy_pid}", check=False).returncode != 0, "deploy exit", timeout=120)
            wait_until(lambda: "ventis-redis" not in remote.output("docker ps --format '{{.Names}}'")
                       and "ventis-ec2" not in remote.output("docker ps --format '{{.Names}}'"),
                       "Ventis container cleanup", timeout=120)
            print("  Ventis containers and Redis cleaned up successfully.", flush=True)
        if captured:
            ids = repr(sorted(captured))
            cleanup_code = (
                "import boto3; c=boto3.client('ec2', region_name="
                + repr(cfg["region"])
                + "); ids=" + ids
                + "; c.terminate_instances(InstanceIds=ids)"
            )
            remote.run("python3 -c " + shlex.quote(cleanup_code), check=False)
            def terminated():
                check_code = (
                    "import boto3,json; c=boto3.client('ec2', region_name="
                    + repr(cfg["region"])
                    + "); r=c.describe_instances(InstanceIds=" + ids + "); "
                    + "print(json.dumps(not any(i.get('State', {}).get('Name') not in "
                    + "{'terminated', 'shutting-down'} for x in r['Reservations'] "
                    + "for i in x['Instances'])))"
                )
                return json.loads(remote.output("python3 -c " + shlex.quote(check_code)))
            wait_until(terminated, "captured workers termination", timeout=300)
            print("  Captured EC2 workers terminated successfully.", flush=True)
        elif deploy_pid:
            log_tail = _tail_log(remote, log)
            if log_tail:
                print("  Deploy log tail:", flush=True)
                print(log_tail, flush=True)
        remote.run(f"rm -rf {shlex.quote(temp)}", check=False)
        print("  Remote temporary test directory removed.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
