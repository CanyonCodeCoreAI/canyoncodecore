#!/usr/bin/env python3
"""Preflight a CanyonOS port before an approved deployment.

This checks the public `.car` artifact contract first, then parses Python without
importing it to catch failures that would otherwise stay hidden until a
container loads an agent, starts a workflow, or serves its first request. It
fails closed when the required inputs cannot be checked. A replica is not
evidence: the controller writes `healthy` to Redis before `_load_agent` runs.

    python3 validate.py [artifact_root] [-c config/global_controller.yaml]
                       [--json] [--strict]

`artifact_root` is the `.car` directory: `config/` beside `app/`, the copy of
the application source that becomes /app inside every container.

Exit 1 if any ERROR was reported, 0 otherwise. --strict also fails on warnings.

Runtime capabilities vary across CanyonOS Core installations. This script probes
the importable `ventis` package directly. A capability-gated check reports
UNAVAILABLE when its behavior cannot be proven.
"""

import argparse
import json
import os
import sys

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

from validation.adapter import check_adapter
from validation.core import ERROR, INFO, WARN, Report, load_yaml
from validation.dependencies import (
    check_requirements_coverage,
    check_secrets,
)
from validation.entrypoint import (
    check_entrypoint_module,
    check_flat_collisions,
)
from validation.manifest import (
    check_declaration_bindings,
    check_manifest_structure,
    check_policy,
    check_self_contained_tree,
    discover_agent_declarations,
)
from validation.packaging import check_env_file, check_import_root
from validation.python_source import module_path
from validation.runtime import (
    BASE_AGENT_REQUIREMENTS,
    BASE_WORKFLOW_REQUIREMENTS,
    CAPABILITY_SOURCE,
    probe_capabilities,
)
from validation.workflow import check_workflow

DEFAULT_CONFIG_PATH = "config/global_controller.yaml"
# ventis/cli.py SOURCE_DIR_NAME -- the duplicated application source.
SOURCE_DIR_NAME = "app"


# Path existence and readability are deploy-preflight checks. Do not
# duplicate them here.


# ------------------------------------------------------------------ #
#  Driver                                                             #
# ------------------------------------------------------------------ #


def validate(artifact_dir, config_path, capabilities):
    """Check the public artifact contract and deeper runtime failure modes."""
    report = Report(artifact_dir, capabilities)

    config, error = load_yaml(config_path)
    if error is not None or not isinstance(config, dict):
        report.error(
            "V001",
            config_path,
            0,
            f"the global manifest cannot be read: {error or 'expected a YAML mapping'}",
            "Validation finishes before an approved `canyonos deploy`, so an "
            "unreadable manifest cannot be deferred to deploy.",
        )
        return report

    source_dir = os.path.join(artifact_dir, SOURCE_DIR_NAME)
    if not os.path.isdir(source_dir):
        report.error(
            "V032",
            artifact_dir,
            0,
            f"no `{SOURCE_DIR_NAME}/` beside `config/`",
            "The artifact root holds the application source it deploys: "
            f"`{SOURCE_DIR_NAME}/` is the copy that becomes /app, and every "
            "entrypoint is relative to it. Without it the port has nothing to "
            "build and nothing to keep it decoupled from the developer's tree.",
        )
        return report

    check_self_contained_tree(report, artifact_dir)

    config_dir = os.path.dirname(config_path)
    entries = check_manifest_structure(report, config, config_path, source_dir)
    if entries is None:
        return report

    agents_by_name = discover_agent_declarations(report, config_dir, config_path)
    check_declaration_bindings(report, entries, agents_by_name, config_path)
    check_policy(report, config_dir)

    entrypoints = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type", "agent") == "workflow":
            continue
        name = entry.get("name")
        entrypoint = entry.get("entrypoint")
        if isinstance(entrypoint, str):
            entrypoints.append((name, entrypoint))
        if name in agents_by_name:
            yaml_path, agent_block = agents_by_name[name]
            check_adapter(report, yaml_path, agent_block, entry, source_dir)
        entrypoint_path = os.path.join(source_dir, entrypoint or "")
        if isinstance(entrypoint, str) and os.path.isfile(entrypoint_path):
            check_entrypoint_module(report, source_dir, name, entrypoint)
            check_requirements_coverage(
                report,
                source_dir,
                entry,
                entrypoint_path,
                config_path,
                BASE_AGENT_REQUIREMENTS,
            )

    # Where each agent's stub is written, and therefore the only import that
    # reaches it over gRPC.
    stub_modules = {
        name: module_path(entrypoint)
        for name, entrypoint in entrypoints
        if name in agents_by_name
    }
    stubbed_entrypoint_paths = [
        os.path.join(source_dir, entrypoint)
        for name, entrypoint in entrypoints
        if name in agents_by_name
    ]

    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type", "agent") != "workflow":
            continue
        workflow_file = entry.get("workflow_file")
        if not isinstance(workflow_file, str):
            continue
        workflow_path = os.path.join(source_dir, workflow_file)
        if os.path.isfile(workflow_path):
            check_workflow(report, workflow_path, stub_modules)
            # The workflow image installs its own list. A module it imports for
            # a helper drags that module's dependencies in even though the
            # workflow makes no model call of its own.
            check_requirements_coverage(
                report,
                source_dir,
                entry,
                workflow_path,
                config_path,
                BASE_WORKFLOW_REQUIREMENTS,
                shadowed_paths=stubbed_entrypoint_paths,
            )

    # These survive a green build and otherwise surface only in a container or
    # on its first request.
    check_flat_collisions(report, source_dir, entrypoints)
    check_env_file(report, config, config_path, artifact_dir)

    entrypoint_paths = [
        os.path.join(source_dir, e)
        for _, e in entrypoints
        if os.path.isfile(os.path.join(source_dir, e))
    ]
    check_import_root(report, source_dir, entrypoint_paths)

    port_paths = list(entrypoint_paths)
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("workflow_file"), str):
            candidate = os.path.join(source_dir, entry["workflow_file"])
            if os.path.isfile(candidate):
                port_paths.append(candidate)

    # Secret detection remains because a green image build would permanently
    # bake the credential into every image.
    check_secrets(report, port_paths)
    return report


# ------------------------------------------------------------------ #
#  Output                                                             #
# ------------------------------------------------------------------ #

LEVEL_ORDER = {ERROR: 0, WARN: 1, INFO: 2}


def _wrap(text, width, indent):
    words = text.split()
    lines = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) + len(indent) > width and current:
            lines.append(indent + current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(indent + current)
    return lines


def print_report(report, artifact_root):
    caps = report.capabilities
    if not caps.get("ventis"):
        print("ventis is not importable here -- capability-gated rules are")
        print("reported UNAVAILABLE rather than checked.\n")
    else:
        print("CanyonOS Core capabilities detected:")
        for key, source in CAPABILITY_SOURCE.items():
            mark = "yes" if caps.get(key) else "no "
            print(f"  {mark}  {key:<22} {source}")
        print()

    findings = sorted(
        report.findings,
        key=lambda f: (LEVEL_ORDER[f["level"]], f["check"], f["path"], f["line"]),
    )
    for finding in findings:
        where = finding["path"]
        if where and finding["line"]:
            where = f"{where}:{finding['line']}"
        header = f"{finding['check']}  {finding['level']:<5}"
        print(f"{header}  {where}" if where else header)
        for line in _wrap(finding["summary"], 78, "    "):
            print(line)
        if finding["mechanism"]:
            for line in _wrap(finding["mechanism"], 78, "      "):
                print(line)
        print()

    errors, warnings = report.counts()
    if not findings:
        print(f"{artifact_root}: clean.")
        return
    print(f"{errors} error(s), {warnings} warning(s).")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Check a CanyonOS Core port against the rules in SKILL.md."
    )
    parser.add_argument(
        "artifact_root",
        nargs="?",
        default=".",
        help="the .car directory holding config/ and app/ (default: the cwd)",
    )
    parser.add_argument(
        "-c",
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"config path relative to artifact_root (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument("--json", action="store_true", help="emit findings as JSON")
    parser.add_argument(
        "--strict", action="store_true", help="fail on warnings as well as errors"
    )
    args = parser.parse_args(argv)

    artifact_root = os.path.abspath(args.artifact_root)
    config_path = (
        args.config
        if os.path.isabs(args.config)
        else os.path.join(artifact_root, args.config)
    )

    capabilities = probe_capabilities()
    report = validate(artifact_root, config_path, capabilities)
    errors, warnings = report.counts()

    if args.json:
        print(
            json.dumps(
                {
                    "artifact_root": artifact_root,
                    "capabilities": capabilities,
                    "errors": errors,
                    "warnings": warnings,
                    "findings": report.findings,
                },
                indent=2,
            )
        )
    else:
        print_report(report, report.rel(artifact_root) or artifact_root)

    if errors or (args.strict and warnings):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
