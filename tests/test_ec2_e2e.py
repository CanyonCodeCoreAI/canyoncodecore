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
        "host", "user", "key", "worker_key", "region", "ami_id", "subnet_id",
        "security_group_ids", "ssh_user",
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
    result["instance_type"] = os.environ.get("VENTIS_INSTANCE_TYPE", "t2.nano")
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


def wait_until(predicate, description, timeout=TIMEOUT, interval=5):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            value = predicate()
            if value:
                return value
            last = value
        except Exception as exc:  # transient AWS/SSH availability is expected
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
        archive = subprocess.Popen(["tar", "-czf", "-", "--exclude=.git", "."], cwd=ROOT, stdout=subprocess.PIPE)
        ssh = subprocess.Popen(remote.base + [f"tar -xzf - -C {shlex.quote(temp)}"], stdin=archive.stdout,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False)
        archive.stdout.close(); _, err = ssh.communicate(timeout=120); archive.wait()
        if ssh.returncode or archive.returncode:
            raise RuntimeError(f"Could not stage source: {err.decode(errors='replace')}")
        print("  Source staging succeeded.", flush=True)

        print("[3/8] Creating virtual environment and installing Ventis...", flush=True)
        project = f"{temp}/project"
        remote.run(
            f"cd {shlex.quote(temp)} && python3 -m venv .venv && "
            ". .venv/bin/activate && pip install -q -e . && "
            ".venv/bin/python -m ventis.cli new-project project"
        )
        print("  Virtual environment and editable install succeeded.", flush=True)
        config = {"agents": [
            {"name": "ExampleAgent", "replicas": 1, "redis_port": 6379, "instance_type": cfg["instance_type"], "entrypoint": "agents/example_agent.py", "provider": "EC2"},
            {"name": "Workflow", "replicas": 1, "type": "workflow", "redis_port": 6379, "workflow_file": "workflows/example_workflow.py", "provider": "EC2", "instance_type": cfg["instance_type"]}],
            "poll_interval": 5, "redis": {"host": "localhost", "port": 6379, "db": 0},
            "ec2": {"region": cfg["region"], "ami_id": cfg["ami_id"], "subnet_id": cfg["subnet_id"], "security_group_ids": cfg["security_group_ids"], "ssh_user": cfg["ssh_user"]}}
        encoded = json.dumps(config)
        config_code = (
            "import json,yaml; yaml.safe_dump(json.loads(" + repr(encoded) + "), "
            "open(" + repr(project + "/config/global_controller.yaml") + ", 'w'))"
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
            f"</dev/null >{shlex.quote(log)} 2>&1 & echo $!",
            timeout=30,
        )
        deploy_pid = int(proc.stdout.strip().splitlines()[-1])
        print(f"  ventis deploy started (PID {deploy_pid}).", flush=True)
        def ready():
            found = records(remote, project)
            selected = [x for x in found if x.get("agent_name") in {"ExampleAgent", "Workflow"}]
            if len(selected) == 2:
                captured.update(x["ec2_instance_id"] for x in selected); return selected
            return False
        selected = wait_until(ready, "both EC2 service records")
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
        remote.run(f"rm -rf {shlex.quote(temp)}", check=False)
        print("  Remote temporary test directory removed.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
