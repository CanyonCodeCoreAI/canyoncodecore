"""V030-V031 -- capability-gated rules about credentials and import roots."""

import os

from validation.core import line_of
from validation.python_source import (
    parse_python,
    resolves_flat,
    resolves_nested,
    toplevel_import_names,
)
from validation.runtime import RUNTIME_FLAT_NAMES


def check_env_file(report, config, config_path, artifact_dir):
    """V030 -- gated on detected env-file injection support."""
    declared = config.get("env_file")
    supported = report.capabilities.get("env_file")

    if not supported:
        if declared:
            report.error(
                "V030",
                config_path,
                line_of(config, "env_file"),
                f"`env_file: {declared}` is set, but this CanyonOS Core never reads it",
                "No resolve_env_file in the importable ventis package, so the "
                "key is silently dropped and the container answers a provider "
                "credential error on the first request. This port requires the "
                "`env_file` runtime capability.",
            )
        else:
            report.unavailable(
                "V030",
                "env_file is not supported by the importable `ventis` runtime. "
                "Credentials have no declared path into a container on this tree.",
            )
        return

    if not declared:
        report.warn(
            "V030",
            config_path,
            line_of(config),
            "no `env_file:` in the config",
            "Only runtime-managed VENTIS_* variables are guaranteed without it. "
            "If the source reads credentials from the environment, the first "
            "request fails on a provider error.",
        )
        return


def check_import_root(report, source_dir, entrypoint_paths):
    """V031 -- gated on detected editable-install support."""
    supported = report.capabilities.get("editable_install")
    has_metadata = any(
        os.path.isfile(os.path.join(source_dir, name))
        for name in ("pyproject.toml", "setup.py", "setup.cfg")
    )

    non_flat = []
    for path in entrypoint_paths:
        tree, _ = parse_python(path)
        if tree is None:
            continue
        for name, lineno in toplevel_import_names(tree).items():
            if f"{name}.py" in RUNTIME_FLAT_NAMES:
                continue
            if resolves_flat(source_dir, name):
                continue
            location = resolves_nested(source_dir, name)
            if location:
                non_flat.append((path, lineno, name, location))

    if not supported:
        report.unavailable(
            "V031",
            "the editable install (`-e .`) is not supported by the importable "
            "`ventis` runtime. Only names rooted at /app import inside a container.",
        )
        for path, lineno, name, location in non_flat:
            report.error(
                "V031",
                path,
                lineno,
                f"`import {name}` resolves to {location}, which is not at the "
                "root of the source copy",
                "sys.path[0] is /app and this CanyonOS Core runs no editable install, "
                "so only modules swept to the root import. The adapter raises "
                "ModuleNotFoundError inside _load_agent and the first request "
                "answers 'No agent loaded'.",
            )
        return

    if non_flat and not has_metadata:
        for path, lineno, name, location in non_flat:
            report.error(
                "V031",
                path,
                lineno,
                f"`import {name}` resolves to {location}, and the source copy's "
                "root has no packaging metadata",
                "A pyproject.toml, setup.py or setup.cfg at the root of the "
                "source copy is what adds `-e .`; metadata nested deeper in the "
                "tree is ignored. Add minimal root metadata pointing at the "
                "existing package directory. Without it the install is skipped "
                "silently.",
            )
