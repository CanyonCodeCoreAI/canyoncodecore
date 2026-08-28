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
from .stages import Config, install_signal_handlers

HARNESS_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WORK = HARNESS_ROOT / ".harness"
DEFAULT_DB = DEFAULT_WORK / "results.sqlite"


def _load_repos(path: Path) -> list[str]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [r["repo"] if isinstance(r, dict) else r for r in doc.get("repos", [])]


def _dotenv(path: Path) -> dict[str, str]:
    """Keys may live in the harness repo's own .env rather than the environment."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip("\"'")
    return out


def _providers(repos_file: Path) -> dict[str, shim.Provider]:
    doc = yaml.safe_load(repos_file.read_text(encoding="utf-8")) or {}
    config = doc.get("providers") or {}
    ambient = {**_dotenv(HARNESS_ROOT / ".env"), **os.environ}
    keys = {
        name: ambient.get((entry or {}).get("key_env", ""), "")
        for name, entry in config.items()
    }
    return shim.build_providers(config, keys)


def cmd_run(args: argparse.Namespace) -> int:
    work = Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    repos_file = Path(args.repos).resolve()
    repos = _load_repos(repos_file)
    if not repos:
        print(f"no repos listed in {repos_file}", file=sys.stderr)
        return 2

    install_signal_handlers()
    providers = _providers(repos_file)
    surfaces = frozenset(providers)
    if not surfaces:
        print("no provider has a key; nothing can be tested. See providers: in "
              f"{repos_file}", file=sys.stderr)
        return 2
    logging.info("open surfaces: %s", ", ".join(sorted(surfaces)))
    shim.start(providers, port=args.shim_port)

    cfg = Config(
        harness_root=HARNESS_ROOT,
        work_root=work,
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


def cmd_screen(args: argparse.Namespace) -> int:
    """Clone and screen candidates without porting anything.

    Stage 2 is a static read, so answering "is this repo in scope" costs a
    shallow clone and no agent budget. This is how the repo list gets built.
    """
    import shutil
    import subprocess
    import tempfile

    from .screen import editable_install_available, screen as do_screen

    repos = _load_repos(Path(args.repos).resolve()) if args.repos else []
    repos += args.repo
    surfaces = frozenset(_providers(Path(args.repos).resolve())) if args.repos \
        else frozenset({"openai"})
    editable = editable_install_available()
    print(f"surfaces={sorted(surfaces)}  editable_install={editable}\n")

    tmp = Path(tempfile.mkdtemp(prefix="screen-"))
    try:
        for repo in repos:
            dest = tmp / runner.slug_for(repo)
            r = subprocess.run(["git", "clone", "-q", "--depth", "1", repo, str(dest)],
                               capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                print(f"{repo:<62} CLONE FAILED {r.stderr.strip()[-80:]}")
                continue
            s = do_screen(dest, surfaces=surfaces, editable_install=editable)
            verdict = "IN SCOPE" if not s.reject else s.reject
            print(f"{repo:<62} root_py={s.root_py_files:<3} py={s.py_files:<4} "
                  f"loc={s.loc:<6} {s.framework}/{s.llm_sdk:<9} {verdict}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
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

    scr_p = sub.add_parser("screen", help="clone and screen candidates, port nothing")
    scr_p.add_argument("--repos", default=None, help="yaml list to screen")
    scr_p.add_argument("repo", nargs="*", help="extra repo urls")
    scr_p.set_defaults(func=cmd_screen)

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
