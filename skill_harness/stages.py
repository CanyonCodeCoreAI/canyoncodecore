"""The eight stages.

Only stage 4 runs an agent. Everything else is a deterministic subprocess with a
timeout, which is what makes a stage 6 failure a fact about the port rather than
about how the agent happened to behave that day. See DESIGN.md section 1.

Every stage writes its own log into the test's artifacts directory. Those logs,
plus the agent trace and the four written files, are what every later analysis
reads — the database deliberately stores none of it.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import screen as screen_mod

log = logging.getLogger("stages")

SKILL_REL = ".claude/skills/porting-to-ventis"

PORT_PROMPT = """\
Port the project in this directory onto Ventis.

Use the porting-to-ventis skill. Follow it; it is the thing under test.

Do not ask for confirmation — there is nobody to answer. When the skill tells you
to report something and stop rather than fix it, write the report to
PORT_REPORT.md in this directory and stop, which counts as following it.
"""


@dataclass
class Result:
    ok: bool
    detail: str = ""


@dataclass
class Ctx:
    repo: str
    slug: str
    root: Path
    artifacts: Path
    cfg: "Config"
    repo_sha: str = ""
    screen: screen_mod.Screen | None = None
    validate_ok: bool | None = None
    core_issue: list = field(default_factory=list)
    skill_issue: list = field(default_factory=list)
    # The skill's "report rather than fix" paths -- the credential wall, the
    # import root, a dependency mismatch -- are all Ventis limitations. An agent
    # that takes one has followed the skill, so this is not a port failure and
    # must not be scored as one.
    reported_and_stopped: bool = False
    # An env var the repo needed and the harness never supplied.
    missing_credential: str | None = None
    # The port served a result and the source's own logic failed inside it.
    # Not a port defect, but it means the run proved less than "served" suggests.
    app_error: str | None = None
    _procs: list = field(default_factory=list)

    def log_path(self, name: str) -> Path:
        return self.artifacts / name


@dataclass
class Config:
    harness_root: Path
    work_root: Path
    shim_base: str          # what a *container* uses to reach the shim
    model: str
    effort: str
    budget_usd: float
    port_timeout: int
    stage_timeout: int
    # The two pins that cannot be reconstructed after a run: which skill and
    # which core produced the result. Git tree hashes, so each moves only when
    # its own subtree does.
    skill_sha: str = "unknown"
    ventis_sha: str = "unknown"
    disallowed_tools: str = ""
    # Which Bedrock wire formats this credential can actually reach. An account
    # property, measured rather than assumed — see README.
    surfaces: frozenset = frozenset({"openai"})


# --------------------------------------------------------------------------- #
#  helpers
# --------------------------------------------------------------------------- #

# The harness runs under its own virtualenv, but launching it does not put that
# venv's bin on PATH. Without this the `ventis` CLI is simply not found -- stage 6
# exits 127 and the run records a harness setup fault as a defect in the port --
# and the agent in stage 4 has no interpreter that can import ventis, so it goes
# looking for one outside the tree under test.
_BIN = str(Path(sys.executable).parent)


def subprocess_env(extra: dict | None = None) -> dict:
    env = {**os.environ, **(extra or {})}
    path = env.get("PATH", "")
    if _BIN not in path.split(os.pathsep):
        env["PATH"] = _BIN + os.pathsep + path
    env.setdefault("VIRTUAL_ENV", str(Path(_BIN).parent))
    return env


# Every child the harness has started, so none of them outlives it. A killed
# harness used to orphan its agents: they kept running, kept spending their
# budget, and kept calling the shim of whatever run started next.
_LIVE: set[subprocess.Popen] = set()
_LIVE_LOCK = threading.Lock()


def kill_children(sig=signal.SIGTERM) -> int:
    with _LIVE_LOCK:
        procs = [p for p in _LIVE if p.poll() is None]
    for proc in procs:
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except Exception:  # noqa: BLE001 - already gone, or not ours any more
            pass
    return len(procs)


def install_signal_handlers() -> None:
    """Take the children down with us, however we are asked to stop."""
    def _handler(signum, _frame):
        n = kill_children(signal.SIGTERM)
        log.warning("signal %s: terminated %d child process(es)", signum, n)
        raise SystemExit(128 + signum)

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _handler)
        except ValueError:  # not on the main thread
            pass
    atexit.register(kill_children)


def run(ctx: Ctx, name: str, cmd: list[str], *, cwd: Path | None = None,
        timeout: int | None = None, env: dict | None = None) -> tuple[int, str]:
    """Run a subprocess, tee its output into the artifacts directory, return it."""
    timeout = timeout or ctx.cfg.stage_timeout
    full_env = subprocess_env(env)
    log.debug("%s: %s", name, " ".join(cmd))
    proc = None
    try:
        # Its own process group, so a timeout or a signal reaches the whole tree
        # rather than just the process the harness happens to hold.
        proc = subprocess.Popen(
            cmd, cwd=cwd or ctx.root, env=full_env, start_new_session=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        with _LIVE_LOCK:
            _LIVE.add(proc)
        out, _ = proc.communicate(timeout=timeout)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        out, _ = proc.communicate()
        rc = 124
        out = (out or "") + f"\n[harness] timed out after {timeout}s\n"
    except FileNotFoundError as e:
        rc, out = 127, f"[harness] {e}\n"
    finally:
        if proc is not None:
            with _LIVE_LOCK:
                _LIVE.discard(proc)
    ctx.log_path(f"{name}.log").write_text(out or "", encoding="utf-8")
    return rc, out or ""


def _config_path(root: Path) -> Path:
    return root / "config" / "global_controller.yaml"


def _load_yaml(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _agent_entries(root: Path) -> list[dict]:
    return _load_yaml(_config_path(root)).get("agents") or []


def _api_port(root: Path, default: int = 8080) -> int:
    for entry in _agent_entries(root):
        if entry.get("type") == "workflow":
            return int(entry.get("api_port", default))
    return default


# --------------------------------------------------------------------------- #
#  1. fetched
# --------------------------------------------------------------------------- #

def fetch(ctx: Ctx) -> Result:
    if ctx.root.exists():
        shutil.rmtree(ctx.root)
    ctx.root.parent.mkdir(parents=True, exist_ok=True)
    rc, out = run(ctx, "1-fetch", ["git", "clone", "--depth", "1", ctx.repo, str(ctx.root)],
                  cwd=ctx.root.parent)
    if rc != 0:
        return Result(False, f"clone failed: {out.strip()[-300:]}")
    rc, sha = run(ctx, "1-sha", ["git", "rev-parse", "HEAD"])
    ctx.repo_sha = sha.strip() if rc == 0 else "unknown"
    return Result(True, ctx.repo_sha[:12])


# --------------------------------------------------------------------------- #
#  2. screened
# --------------------------------------------------------------------------- #

def screen(ctx: Ctx) -> Result:
    ctx.screen = screen_mod.screen(ctx.root, surfaces=ctx.cfg.surfaces)
    ctx.log_path("2-screen.json").write_text(
        json.dumps(ctx.screen.__dict__, indent=2, default=str), encoding="utf-8"
    )
    if ctx.screen.reject:
        return Result(False, ctx.screen.reject)
    return Result(True, f"{ctx.screen.framework}/{ctx.screen.llm_sdk}, {ctx.screen.loc} loc")


# --------------------------------------------------------------------------- #
#  3. wired
# --------------------------------------------------------------------------- #

# A repo's SDK refuses to send without *a* key, so it gets a placeholder; the
# shim replaces it with the real one on the way out. No real credential is
# written into the repo, and none enters the image.
_PLACEHOLDER = "supplied-by-the-shim"


def wire(ctx: Ctx) -> Result:
    """Write the .env the port will point `env_file:` at.

    The source tree is never edited; this adds a file beside it, which is what
    the skill's own credential path expects (M18, M23). Only the surfaces this
    harness can actually serve are written -- pointing a repo at a base URL that
    answers 503 would be worse than leaving it unset, because the failure would
    read as the repo's rather than the harness's.
    """
    if not ctx.cfg.surfaces:
        return Result(False, "no provider surface is open")

    base = f"{ctx.cfg.shim_base}/r/{ctx.slug}"
    lines = [
        "# Written by skill_harness. The source tree is untouched; this file is",
        "# what `env_file:` in the port's config points at. The keys are",
        "# placeholders: the shim swaps the real ones in as requests pass.",
    ]
    if "openai" in ctx.cfg.surfaces:
        lines += [f"OPENAI_BASE_URL={base}/openai", f"OPENAI_API_KEY={_PLACEHOLDER}"]
    if "anthropic" in ctx.cfg.surfaces:
        lines += [f"ANTHROPIC_BASE_URL={base}/anthropic",
                  f"ANTHROPIC_API_KEY={_PLACEHOLDER}"]
    lines.append("")

    env = "\n".join(lines)
    (ctx.root / ".env").write_text(env, encoding="utf-8")
    ctx.log_path("3-wire.log").write_text(env, encoding="utf-8")
    return Result(True, f"{'+'.join(sorted(ctx.cfg.surfaces))} via {base}")


# --------------------------------------------------------------------------- #
#  4. ported  — the only stage that runs an agent
# --------------------------------------------------------------------------- #

def port(ctx: Ctx) -> Result:
    """Copy the skill in, then run `claude -p` against the repo.

    The skill is delivered explicitly rather than relied on globally, so the
    version under test is the version recorded in tests.skill_sha.
    """
    src = ctx.cfg.harness_root / SKILL_REL
    dst = ctx.root / SKILL_REL
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, dirs_exist_ok=True)

    cmd = [
        "claude", "-p", PORT_PROMPT,
        "--bare",
        "--setting-sources", "",
        "--permission-mode", "bypassPermissions",
        "--output-format", "stream-json", "--verbose",
        "--model", ctx.cfg.model,
        "--effort", ctx.cfg.effort,
        "--max-budget-usd", str(ctx.cfg.budget_usd),
        "--no-session-persistence",
    ]
    if ctx.cfg.disallowed_tools:
        cmd += ["--disallowedTools", ctx.cfg.disallowed_tools]

    rc, out = run(ctx, "4-port", cmd, timeout=ctx.cfg.port_timeout)
    # The stream-json trace is the only record of *how* the agent got there.
    ctx.log_path("4-port.trace.jsonl").write_text(out, encoding="utf-8")

    report = ctx.root / "PORT_REPORT.md"
    if report.is_file():
        # Filed as a core issue: every "report and stop" the skill defines is
        # triggered by something Ventis cannot do, so it is a finding with a
        # Ventis owner. The full text stays in artifacts; this is the grouping key.
        ctx.reported_and_stopped = True
        ctx.core_issue.append({"kind": "reported_and_stopped",
                               "text": report.read_text(encoding="utf-8")[:4000]})

    if rc != 0:
        return Result(False, f"claude exited {rc}")

    if not _config_path(ctx.root).is_file():
        if ctx.reported_and_stopped:
            return Result(False, "reported and stopped: " + _report_headline(report))
        return Result(False, "no port written, and no report explaining why")
    return Result(True, "port written")


def _report_headline(report: Path) -> str:
    """The report's first non-empty, non-heading line, for the run log."""
    for line in report.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip().lstrip("#").strip()
        if line and not line.startswith(("**Date", "**Skill", "---")):
            return line[:160]
    return "(no summary line)"


# --------------------------------------------------------------------------- #
#  5. validated  — records a verdict, does not gate
# --------------------------------------------------------------------------- #

def validate(ctx: Ctx) -> Result:
    script = ctx.cfg.harness_root / SKILL_REL / "validate.py"
    rc, out = run(ctx, "5-validate", [sys.executable, str(script), ".", "--json"])
    ctx.validate_ok = rc == 0
    if rc == 127:
        ctx.validate_ok = None
        return Result(False, "validate.py could not run")
    try:
        findings = json.loads(out)
        ctx.log_path("5-validate.json").write_text(json.dumps(findings, indent=2),
                                                   encoding="utf-8")
    except json.JSONDecodeError:
        pass
    return Result(True, "pass" if ctx.validate_ok else "fail (not gating)")


# --------------------------------------------------------------------------- #
#  6. built  — build, then both probes from SKILL.md Step 4
# --------------------------------------------------------------------------- #

def build(ctx: Ctx) -> Result:
    rc, out = run(ctx, "6-build", ["ventis", "build", "-c", "config/global_controller.yaml"])
    if rc != 0:
        return Result(False, f"ventis build exited {rc}")

    # `ventis build` prints "Build complete." and exits 0 for a project whose
    # container dies on startup, so the build result is not evidence on its own.
    for entry in _agent_entries(ctx.root):
        if entry.get("type") == "workflow":
            continue
        name = entry.get("name", "")
        image = f"ventis-{name.lower()}"

        rc, out = run(ctx, f"6-probe1-{name}",
                      ["docker", "run", "--rm", image, "python", "-c",
                       "import local_controller"])
        if rc != 0:
            # The gRPC/protobuf stack is unpinned and resolved on the host; this
            # failure belongs to Ventis, not to the port.
            ctx.core_issue.append({"kind": "runtime_import", "agent": name,
                                   "detail": out.strip()[-500:]})
            return Result(False, f"{image}: import local_controller failed")

        entrypoint = Path(entry.get("entrypoint", "")).name
        probe = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('m', '{entrypoint}')\n"
            "m = importlib.util.module_from_spec(spec); sys.modules['m'] = m\n"
            f"spec.loader.exec_module(m); m.{name}(); print('ok')"
        )
        rc, out = run(ctx, f"6-probe2-{name}",
                      ["docker", "run", "--rm", image, "python", "-c", probe])
        if rc != 0:
            # _load_agent swallows every exception, so without this probe the
            # symptom would only appear as "No agent loaded" at stage 8.
            ctx.skill_issue.append({"kind": "agent_unloadable", "agent": name,
                                    "detail": out.strip()[-500:]})
            return Result(False, f"{image}: agent would not load")

    return Result(True, "built, both probes pass")


# --------------------------------------------------------------------------- #
#  7. deployed
# --------------------------------------------------------------------------- #

def deploy(ctx: Ctx) -> Result:
    """`ventis deploy` blocks in a health-monitoring loop, so it runs detached
    and the harness waits on the workflow's port instead."""
    logfile = ctx.log_path("7-deploy.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(
        ["ventis", "deploy", "-c", "config/global_controller.yaml"],
        cwd=ctx.root, stdout=logfile, stderr=subprocess.STDOUT, text=True,
        start_new_session=True, env=subprocess_env(),
    )
    ctx._procs.append(proc)
    with _LIVE_LOCK:
        _LIVE.add(proc)

    port_no = _api_port(ctx.root)
    deadline = time.time() + ctx.cfg.stage_timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return Result(False, f"ventis deploy exited early ({proc.returncode})")
        try:
            urllib.request.urlopen(f"http://localhost:{port_no}/", timeout=2)
            return Result(True, f"workflow answering on :{port_no}")
        except urllib.error.HTTPError:
            return Result(True, f"workflow answering on :{port_no}")  # 404 is an answer
        except Exception:
            time.sleep(2)
    return Result(False, f"workflow never answered on :{port_no}")


# --------------------------------------------------------------------------- #
#  8. served
# --------------------------------------------------------------------------- #

def serve(ctx: Ctx, query: str = "animals") -> Result:
    port_no = _api_port(ctx.root)
    body = json.dumps({"query": query}).encode()
    req = urllib.request.Request(
        f"http://localhost:{port_no}/main", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            accepted = json.loads(resp.read())
    except Exception as e:
        return Result(False, f"POST /main failed: {type(e).__name__}: {e}")

    request_id = accepted.get("request_id")
    if not request_id:
        return Result(False, f"POST /main returned no request_id: {accepted}")

    deadline = time.time() + ctx.cfg.stage_timeout
    last = {}
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://localhost:{port_no}/status/{request_id}", timeout=30
            ) as resp:
                last = json.loads(resp.read())
        except Exception as e:
            last = {"status": "poll_failed", "detail": str(e)}
        ctx.log_path("8-serve.json").write_text(json.dumps(last, indent=2), encoding="utf-8")
        status = last.get("status")
        if status == "done":
            # Ventis served the request. Whether the *project* then did anything
            # useful is a separate question, and conflating the two would let a
            # hundred-repo pass rate mean much less than it appears to: a port
            # can be perfect while the source fails on a query that means
            # nothing to it, or on a host it was never given.
            inner = last.get("result")
            if isinstance(inner, dict):
                app = inner.get("status") or inner.get("error") or inner.get("error_message")
                if inner.get("status") in ("failed", "error") or inner.get("error_message"):
                    ctx.app_error = str(
                        inner.get("error_message") or inner.get("error") or app
                    )[:300]
                    return Result(True, f"served; the project itself errored: "
                                        f"{ctx.app_error[:80]}")
            return Result(True, "served")
        if status == "error":
            detail = str(last.get("error", ""))
            # A bare env var name is what a repo raises when a backing service it
            # needs was never configured. That is a fact about the repo, not a
            # defect in the port, so it must not be scored as one.
            missing = re.fullmatch(r"'([A-Z][A-Z0-9_]{3,})'", detail.strip())
            if missing:
                ctx.missing_credential = missing.group(1)
                return Result(False, f"needs {missing.group(1)}, which was never configured")
            ctx.skill_issue.append({"kind": "request_error", "detail": last})
            return Result(False, f"request errored: {str(last)[:300]}")
        time.sleep(3)
    return Result(False, f"request never completed: {str(last)[:300]}")


# --------------------------------------------------------------------------- #
#  teardown
# --------------------------------------------------------------------------- #

def teardown(ctx: Ctx) -> None:
    """Containers and the controller outlive the pipeline unless killed."""
    for proc in ctx._procs:
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                proc.wait(timeout=30)
            except Exception:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    pass
    ctx._procs.clear()
    if _config_path(ctx.root).is_file():
        run(ctx, "9-clean", ["ventis", "clean"], timeout=120)
