"""CLI.

    python -m skill_harness run   --repos skill_harness/repos.yaml
    python -m skill_harness report
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import yaml

from . import db, runner, shim
from .stages import Config

HARNESS_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WORK = HARNESS_ROOT / ".harness"
DEFAULT_DB = DEFAULT_WORK / "results.sqlite"


def _load_repos(path: Path) -> list[str]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [r["repo"] if isinstance(r, dict) else r for r in doc.get("repos", [])]


def _model_map(path: Path) -> shim.ModelMap:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    m = doc.get("models", {})
    defaults = {k: v for k, v in (m.get("defaults") or {}).items() if v}
    return shim.ModelMap(
        exact=m.get("exact", {}),
        prefixes=[(p["prefix"], p["to"]) for p in m.get("prefixes", [])],
        defaults=defaults,
    )


def cmd_run(args: argparse.Namespace) -> int:
    work = Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    repos_file = Path(args.repos).resolve()
    repos = _load_repos(repos_file)
    if not repos:
        print(f"no repos listed in {repos_file}", file=sys.stderr)
        return 2

    key = os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "")
    if not key:
        # Stage 3 would report this per repo, but failing here says it once and
        # avoids cloning a hundred repos to learn it.
        print("AWS_BEARER_TOKEN_BEDROCK is not set; stage 3 cannot run.", file=sys.stderr)
        return 2

    model_map = _model_map(repos_file)
    surfaces = frozenset(model_map.defaults)
    if not surfaces:
        print("no usable surface: every models.defaults entry is empty", file=sys.stderr)
        return 2
    logging.info("reachable surfaces: %s", ", ".join(sorted(surfaces)))
    shim.start(region=args.region, key=key, model_map=model_map, port=args.shim_port)

    cfg = Config(
        harness_root=HARNESS_ROOT,
        work_root=work,
        region=args.region,
        shim_base=f"{args.shim_host}:{args.shim_port}",
        model=args.model,
        effort=args.effort,
        budget_usd=args.budget,
        port_timeout=args.port_timeout,
        stage_timeout=args.stage_timeout,
        skill_sha=runner._tree_sha(HARNESS_ROOT, ".claude/skills/porting-to-ventis"),
        ventis_sha=runner._tree_sha(HARNESS_ROOT, "ventis"),
        disallowed_tools=args.disallowed_tools,
        surfaces=surfaces,
    )
    logging.info("skill %s | core %s | model %s/%s",
                 cfg.skill_sha[:12], cfg.ventis_sha[:12], cfg.model, cfg.effort)

    conn = db.connect(args.db)
    records = runner.run_all(repos, cfg, conn, concurrency=args.concurrency)

    failed = sum(1 for r in records if r["status"] != "passed")
    print(f"\n{len(records)} repos, {len(records) - failed} served, {failed} did not")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    rows = db.summary(conn)
    if not rows:
        print("no results yet")
        return 0
    width = max(len(r["repo"]) for r in rows)
    for r in rows:
        v = {None: "-", 1: "pass", 0: "FAIL"}[r["validate_ok"]]
        print(f"{r['repo']:<{width}}  {r['farthest_step']:<10} {r['status']:<18} validate={v}")
    print("\nvalidate.py against the eventual outcome:")
    print(json.dumps(db.confusion(conn), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="skill_harness")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="run the pipeline over a repo list")
    run_p.add_argument("--repos", default=str(HARNESS_ROOT / "skill_harness" / "repos.yaml"))
    run_p.add_argument("--work", default=str(DEFAULT_WORK))
    run_p.add_argument("--concurrency", type=int, default=2)
    run_p.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    run_p.add_argument("--shim-port", type=int, default=8300)
    # Containers reach the host by a different name than the harness does.
    run_p.add_argument("--shim-host", default="http://host.docker.internal")
    run_p.add_argument("--model", default="opus")
    run_p.add_argument("--effort", default="high")
    run_p.add_argument("--budget", type=float, default=8.0)
    run_p.add_argument("--port-timeout", type=int, default=3600)
    run_p.add_argument("--stage-timeout", type=int, default=900)
    run_p.add_argument("--disallowed-tools", default="")
    run_p.set_defaults(func=cmd_run)

    rep_p = sub.add_parser("report", help="print the results table")
    rep_p.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)-8s %(message)s",
        datefmt="%H:%M:%S",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
