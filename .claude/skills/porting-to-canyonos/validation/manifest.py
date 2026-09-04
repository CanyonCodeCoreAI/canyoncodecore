"""Fail-closed checks for the public CanyonOS artifact contract."""

import glob
import os

from validation.core import line_of, load_yaml


def _safe_relative_python_path(value):
    if not isinstance(value, str) or not value.strip():
        return False
    normalized = value.replace("\\", "/")
    return (
        not normalized.startswith("/")
        and ".." not in normalized.split("/")
        and normalized.endswith(".py")
    )


def check_manifest_structure(report, config, config_path, source_dir):
    """Return entries only when deeper checks can traverse them safely."""
    entries = config.get("agents")
    if not isinstance(entries, list):
        report.error(
            "V001",
            config_path,
            line_of(config, "agents"),
            "`agents:` must be a list",
            "The CanyonOS manifest cannot be traversed or built without an agents list.",
        )
        return None

    valid = True
    agent_count = sum(
        1
        for entry in entries
        if isinstance(entry, dict) and entry.get("type", "agent") == "agent"
    )
    workflow_count = sum(
        1
        for entry in entries
        if isinstance(entry, dict) and entry.get("type", "agent") == "workflow"
    )
    if agent_count < 1:
        report.error(
            "V002",
            config_path,
            line_of(config, "agents"),
            "the manifest must contain at least one agent service",
            "A CanyonOS port needs a callable service behind its workflow.",
        )
        valid = False
    if workflow_count != 1:
        report.error(
            "V002",
            config_path,
            line_of(config, "agents"),
            f"the manifest must contain exactly one workflow service; found {workflow_count}",
            "The deployment exposes one `/main` workflow and builds one workflow image.",
        )
        valid = False
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            report.error(
                "V002",
                config_path,
                0,
                f"agents[{index}] must be a mapping",
                "CanyonOS reads each agents item as a service declaration.",
            )
            valid = False
            continue

        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            report.error(
                "V002",
                config_path,
                line_of(entry, "name"),
                f"agents[{index}] has no non-empty string `name`",
                "Names bind manifest entries, declarations, generated stubs, and images.",
            )
            valid = False
        else:
            earlier = [
                item.get("name")
                for item in entries[:index]
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            ]
            collision = next(
                (other for other in earlier if other.lower() == name.lower()), None
            )
            if collision is not None:
                report.error(
                    "V002",
                    config_path,
                    line_of(entry, "name"),
                    f"`{name}` collides with `{collision}` after lowercase normalization",
                    "CanyonOS uses lowercase image and target names, so one "
                    "service overwrites the other.",
                )
                valid = False

        service_type = entry.get("type", "agent")
        if service_type not in ("agent", "workflow"):
            report.error(
                "V002",
                config_path,
                line_of(entry, "type"),
                f"`type: {service_type}` is neither `agent` nor `workflow`",
                "Only those two service shapes have a CanyonOS build contract.",
            )
            valid = False

        provider = entry.get("provider", "local")
        if provider not in ("local", "EC2"):
            report.error(
                "V002",
                config_path,
                line_of(entry, "provider"),
                f"unsupported provider spelling `{provider}`",
                "Use lowercase `local` or uppercase `EC2`; runtime provider "
                "handling is case-sensitive.",
            )
            valid = False

        replicas = entry.get("replicas", 1)
        if isinstance(replicas, bool) or not isinstance(replicas, int) or replicas < 1:
            report.error(
                "V002",
                config_path,
                line_of(entry, "replicas"),
                "`replicas` must be an integer greater than zero",
                "CanyonOS creates one placement per replica and cannot deploy "
                "an empty or fractional set.",
            )
            valid = False

        requirements = entry.get("requirements", [])
        if not isinstance(requirements, list) or not all(
            isinstance(item, str) and item.strip() for item in requirements
        ):
            report.error(
                "V002",
                config_path,
                line_of(entry, "requirements"),
                "`requirements` must be a list of non-empty strings",
                "CanyonOS writes this list into the image requirements file.",
            )
            valid = False

        path_key = "workflow_file" if service_type == "workflow" else "entrypoint"
        relative = entry.get(path_key)
        if not _safe_relative_python_path(relative):
            report.error(
                "V002",
                config_path,
                line_of(entry, path_key),
                f"`{path_key}` must be a relative .py path contained by `.car/app`",
                "Absolute and parent-relative paths escape the self-contained CanyonOS artifact.",
            )
            valid = False
        elif not os.path.isfile(os.path.join(source_dir, relative)):
            report.error(
                "V002",
                config_path,
                line_of(entry, path_key),
                f"`{path_key}: {relative}` does not exist in `.car/app`",
                "The deploy build cannot create this service without its Python entry file.",
            )
            valid = False

    return entries if valid else None


def check_self_contained_tree(report, artifact_dir):
    """Reject symlinks because `.car` must not depend on outside state."""
    for root, directories, files in os.walk(artifact_dir, followlinks=False):
        for name in [*directories, *files]:
            path = os.path.join(root, name)
            if not os.path.islink(path):
                continue
            report.error(
                "V036",
                path,
                0,
                "`.car` contains a symbolic link",
                "A symlink can escape the artifact or be skipped by the "
                "Python-file sweep. Copy the intended file or directory into "
                "the artifact explicitly.",
            )


def discover_agent_declarations(report, config_dir, config_path):
    """Load declarations without silently discarding malformed or duplicate YAML."""
    declarations = {}
    for path in sorted(glob.glob(os.path.join(config_dir, "*.yaml"))):
        data, error = load_yaml(path)
        if error is not None:
            report.error(
                "V003",
                path,
                0,
                f"YAML cannot be parsed: {error}",
                "CanyonOS reads every YAML file in the config directory during deploy.",
            )
            continue
        if not isinstance(data, dict):
            if os.path.realpath(path) == os.path.realpath(config_path):
                report.error(
                    "V003",
                    path,
                    0,
                    "the global manifest must be a mapping",
                    "A scalar or empty manifest has no CanyonOS configuration contract.",
                )
            continue
        agent = data.get("agent")
        if agent is None:
            continue
        if (
            not isinstance(agent, dict)
            or not isinstance(agent.get("name"), str)
            or not agent["name"]
        ):
            report.error(
                "V003",
                path,
                line_of(data, "agent"),
                "`agent` must contain a non-empty string `name`",
                "The declaration name is the binding used to generate its stub.",
            )
            continue
        name = agent["name"]
        if name in declarations:
            report.error(
                "V003",
                path,
                line_of(agent, "name"),
                f"duplicate declaration for `{name}`",
                "Filename ordering would otherwise choose one declaration silently.",
            )
            continue
        declarations[name] = (path, agent)
    return declarations


def check_declaration_bindings(report, entries, declarations, config_path):
    """Require a one-to-one binding for every agent service."""
    configured = {
        entry["name"] for entry in entries if entry.get("type", "agent") != "workflow"
    }
    for name in sorted(configured - declarations.keys()):
        report.error(
            "V004",
            config_path,
            0,
            f"agent `{name}` has no matching declaration in `.car/config`",
            f"Without `agent.name: {name}`, CanyonOS cannot generate the service stub.",
        )
    for name in sorted(declarations.keys() - configured):
        path, _ = declarations[name]
        report.warn(
            "V004",
            path,
            0,
            f"declaration `{name}` has no agent service in the manifest",
            "It is stale or unused and will not produce a deployable service.",
        )


def check_policy(report, config_dir):
    path = os.path.join(config_dir, "policy.yaml")
    if not os.path.exists(path):
        return
    data, error = load_yaml(path)
    if error is not None or not isinstance(data, dict):
        report.error(
            "V005",
            path,
            0,
            "policy.yaml must be a YAML mapping",
            error or "CanyonOS reads `rules` from this mapping during deploy.",
        )
        return
    rules = data.get("rules")
    if not isinstance(rules, list) or not rules:
        report.error(
            "V005",
            path,
            line_of(data, "rules"),
            "policy.yaml must contain a non-empty `rules` list",
            "Remove the file for unrestricted access; an empty policy is not a valid restriction.",
        )
