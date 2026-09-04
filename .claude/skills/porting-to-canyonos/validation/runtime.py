"""Runtime capabilities and dependency facts used by validation checks."""

import importlib
import os
import sys


RUNTIME_FLAT_NAMES = frozenset(
    {
        "future.py",
        "ventis_context.py",
        "local_controller.py",
        "local_controller_frontend.py",
        "redis_client.py",
        "grpc_options.py",
        "gpu_metrics.py",
        "bedrock.py",
        "deploy.py",
        "session_logging.py",
        "workflow_launcher.py",
    }
)

IMPORT_TO_DISTRIBUTION = {
    "attr": ("attrs",),
    "autogen": ("pyautogen", "ag2", "autogen", "autogen-agentchat"),
    "bs4": ("beautifulsoup4",),
    "cv2": ("opencv-python",),
    "dateutil": ("python-dateutil",),
    "dotenv": ("python-dotenv",),
    "grpc": ("grpcio",),
    "grpc_tools": ("grpcio-tools",),
    "jwt": ("pyjwt",),
    "PIL": ("pillow",),
    "psycopg": ("psycopg",),
    "psycopg2": ("psycopg2-binary",),
    "pydantic_settings": ("pydantic-settings",),
    "sklearn": ("scikit-learn",),
    "typing_extensions": ("typing-extensions",),
    "yaml": ("pyyaml",),
}

NAMESPACE_DISTRIBUTIONS = {"llama_index": "llama-index"}

CAPABILITY_SOURCE = {
    "env_file": "runtime env-file injection",
    "editable_install": "editable project installation",
    "sweeps_all_files": "full project-file sweep",
}


def _base_requirements():
    agent = [
        "grpcio",
        "grpcio-tools",
        "redis",
        "pyyaml",
        "psutil",
        "ipdb",
        "ipython",
        "boto3",
    ]
    workflow = [*agent, "flask", "sqlalchemy", "psycopg[binary]"]
    try:
        from ventis import stub_generator
    except Exception:  # noqa: BLE001 - a broken install must not crash validation
        return agent, workflow
    return (
        list(getattr(stub_generator, "BASE_AGENT_REQUIREMENTS", agent)),
        list(getattr(stub_generator, "BASE_WORKFLOW_REQUIREMENTS", workflow)),
    )


def _stdlib_names():
    names = getattr(sys, "stdlib_module_names", None)
    if names:
        return frozenset(names)
    found = set(sys.builtin_module_names)
    library = os.path.dirname(os.__file__)
    try:
        entries = os.listdir(library)
    except OSError:
        return frozenset(found)
    for entry in entries:
        if entry.endswith(".py"):
            found.add(entry[:-3])
        elif "." not in entry and "-" not in entry:
            found.add(entry)
    return frozenset(found)


BASE_AGENT_REQUIREMENTS, BASE_WORKFLOW_REQUIREMENTS = _base_requirements()
STDLIB_MODULE_NAMES = _stdlib_names()


def probe_capabilities():
    """Probe the installed compatibility runtime behind the CanyonOS CLI."""
    capabilities = dict.fromkeys(CAPABILITY_SOURCE, False)
    capabilities["ventis"] = False
    try:
        from ventis import stub_generator
    except Exception:  # noqa: BLE001 - unavailable runtime is reported, not fatal
        return capabilities

    capabilities["ventis"] = True
    capabilities["editable_install"] = hasattr(stub_generator, "_install_step")
    capabilities["sweeps_all_files"] = hasattr(stub_generator, "_sweep_project_files")

    for module_name in (
        "ventis.controller.utils.env_file",
        "ventis.utils.env_file",
    ):
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001 - try the other supported location
            continue
        if hasattr(module, "resolve_env_file"):
            capabilities["env_file"] = True
            break
    return capabilities
