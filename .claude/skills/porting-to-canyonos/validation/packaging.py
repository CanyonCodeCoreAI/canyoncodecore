"""V030-V031 -- rules about credentials and import roots.

Both used to be gated on a probed capability. Neither actually varies:
`global_controller.py` calls `resolve_env_file` unconditionally on every
deployment, so env-file injection is not optional; and `canyonos_core` has no
`_install_step` or any other editable-install mechanism, in this codebase or
its history, so an editable install is never available. Treat both as fixed
facts about the current runtime instead of probing for them.
"""

from validation.core import line_of
from validation.python_source import (
    parse_python,
    resolves_flat,
    resolves_nested,
    toplevel_import_names,
)
from validation.runtime import RUNTIME_FLAT_NAMES


def check_env_file(report, config, config_path):
    """V030 -- env-file injection is mandatory; warn when the config omits it."""
    if config.get("env_file"):
        return

    report.warn(
        "V030",
        config_path,
        line_of(config),
        "no `env_file:` in the config",
        "Only runtime-managed CANYONOS_* variables are guaranteed without it. "
        "If the source reads credentials from the environment, the first "
        "request fails on a provider error.",
    )


def check_import_root(report, source_dir, entrypoint_paths):
    """V031 -- canyonos_core runs no editable install; only /app-rooted names import."""
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
            if not location:
                continue
            report.error(
                "V031",
                path,
                lineno,
                f"`import {name}` resolves to {location}, which is not at the "
                "root of the source copy",
                "sys.path[0] is /app and canyonos_core runs no editable "
                "install, so only modules swept to the root import. The "
                "adapter raises ModuleNotFoundError inside _load_agent and "
                "the first request answers 'No agent loaded'.",
            )
