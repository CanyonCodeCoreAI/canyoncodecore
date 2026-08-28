"""Orchestration.

Stages 1-5 run concurrently across repos. Stages 6-8 take a global lock: they
build images tagged `ventis-<agent name>`, bind the workflow's api_port, and
`ventis deploy` starts its own Redis container, so two repos cannot be in them at
once regardless of how wide the pool is.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import db, shim, stages
from .stages import Config, Ctx, Result

log = logging.getLogger("runner")

# (stage name, function, gating). Stage 5 is the one non-gating stage: a failed
# validate.py is recorded and the pipeline continues, which is the only way to
# observe a validation that was wrong to block. See DESIGN.md section 1.
PIPELINE = [
    ("fetched", stages.fetch, True),
    ("screened", stages.screen, True),
    ("wired", stages.wire, True),
    ("ported", stages.port, True),
    ("validated", stages.validate, False),
    ("built", stages.build, True),
    ("deployed", stages.deploy, True),
    ("served", stages.serve, True),
]

DOCKER_STAGES = {"built", "deployed", "served"}

_SLUG = re.compile(r"[^a-z0-9]+")


def slug_for(repo: str) -> str:
    return _SLUG.sub("-", repo.rstrip("/").split("/")[-1].removesuffix(".git").lower()).strip("-")


def _tree_sha(root: Path, path: str) -> str:
    """The git tree hash of a subdirectory — it changes when that subtree changes
    and not when anything else in the repo does, which is exactly what pinning
    the skill and the core each require."""
    try:
        out = subprocess.run(["git", "rev-parse", f"HEAD:{path}"], cwd=root,
                             capture_output=True, text=True, timeout=30)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _classify(stage: str, result: Result, ctx: Ctx) -> str:
    if stage == "screened":
        return "blocked"          # out of scope for this run, not a skill failure
    if stage == "wired":
        return "blocked"          # missing credential, nothing was tested
    if stage == "served" and ctx.missing_credential:
        # The port served a real request far enough to run the source, which
        # then asked for a credential nobody gave it. Nothing about the skill or
        # about Ventis failed here.
        return "blocked"
    if stage == "ported":
        if ctx.reported_and_stopped:
            # The skill told the agent to report rather than fix, and it did.
            # Scoring this as a failure would count the skill working as the
            # skill failing, and would bury the Ventis gap that caused it.
            return "blocked"
        trace = ctx.log_path("4-port.log")
        text = trace.read_text(encoding="utf-8", errors="replace") if trace.is_file() else ""
        if "budget" in text.lower() and "exceed" in text.lower():
            return "budget_exhausted"
        if "timed out" in result.detail or "timed out" in text:
            return "timeout"
    return "failed"


def run_repo(repo: str, cfg: Config, conn, docker_lock: threading.Lock) -> dict:
    slug = slug_for(repo)
    artifacts = cfg.work_root / slug / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    ctx = Ctx(repo=repo, slug=slug, root=cfg.work_root / slug / "src",
              artifacts=artifacts, cfg=cfg)

    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    farthest, status = "none", "passed"
    began = time.time()

    try:
        for stage, fn, gating in PIPELINE:
            if stage in DOCKER_STAGES:
                docker_lock.acquire()
            try:
                result = fn(ctx)
            except Exception as e:            # a harness bug, not a port failure
                log.exception("%s: %s crashed", slug, stage)
                result = Result(False, f"harness error: {type(e).__name__}: {e}")
            finally:
                if stage in DOCKER_STAGES:
                    docker_lock.release()

            mark = "ok " if result.ok else "FAIL"
            log.info("%-24s %-10s %s  %s", slug, stage, mark, result.detail)

            if result.ok:
                if gating:
                    farthest = stage
            elif gating:
                status = _classify(stage, result, ctx)
                break
    finally:
        stages.teardown(ctx)

    usage = shim.usage_for(slug)
    (artifacts / "usage.json").write_text(str(usage), encoding="utf-8")

    if ctx.screen:
        db.upsert_repo(conn, repo, framework=ctx.screen.framework,
                       is_multiagent=int(ctx.screen.is_multiagent),
                       description=ctx.screen.description)
    else:
        db.upsert_repo(conn, repo)

    record = dict(
        repo=repo,
        repo_sha=ctx.repo_sha or "unknown",
        skill_sha=cfg.skill_sha,
        ventis_sha=cfg.ventis_sha,
        farthest_step=farthest,
        status=status,
        validate_ok=None if ctx.validate_ok is None else int(ctx.validate_ok),
        core_issue=ctx.core_issue or None,
        skill_issue=ctx.skill_issue or None,
        analysis=None,
        cost_usd=None,
        artifacts=str(artifacts),
        started_at=started,
        ended_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    db.record_test(conn, **record)
    log.info("%-24s => %s at %s (%.0fs)", slug, status, farthest, time.time() - began)
    return record


def run_all(repos: list[str], cfg: Config, conn, concurrency: int = 2) -> list[dict]:
    docker_lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(run_repo, r, cfg, conn, docker_lock) for r in repos]
        return [f.result() for f in futures]
