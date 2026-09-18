"""Dependency and layout facts about the runtime, used by validation checks."""

import os
import sys


RUNTIME_FLAT_NAMES = frozenset(
    {
        "future.py",
        "canyonos_context.py",
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
    "a2a": ("a2a-sdk",),
    "attr": ("attrs",),
    "autogen": ("pyautogen", "ag2", "autogen", "autogen-agentchat"),
    "bs4": ("beautifulsoup4",),
    "cv2": ("opencv-python",),
    "dateutil": ("python-dateutil",),
    "dotenv": ("python-dotenv",),
    "faiss": ("faiss-cpu", "faiss-gpu"),
    "git": ("gitpython",),
    # Not derivable: the dotted path bears no relation to the distribution.
    "googleapiclient": ("google-api-python-client",),
    "grpc": ("grpcio",),
    "grpc_tools": ("grpcio-tools",),
    "jwt": ("pyjwt",),
    "PIL": ("pillow",),
    "psycopg": ("psycopg",),
    "psycopg2": ("psycopg2-binary",),
    "pydantic_settings": ("pydantic-settings",),
    "sklearn": ("scikit-learn",),
    # `speech_recognition` normalizes to speech-recognition; PyPI has no dash.
    "speech_recognition": ("speechrecognition",),
    "typing_extensions": ("typing-extensions",),
    "yaml": ("pyyaml",),
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
        from canyonos_core import stub_generator
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
    # Compiled stdlib extensions (math, _json, array, ...) live in
    # lib-dynload, a sibling of the .py-file library directory, not inside
    # it. Missing this directory misclassifies every such module as a
    # missing third-party dependency on any pre-3.10 interpreter, where
    # sys.stdlib_module_names does not exist.
    dynload = os.path.join(library, "lib-dynload")
    for directory in (library, dynload):
        try:
            entries = os.listdir(directory)
        except OSError:
            continue
        for entry in entries:
            if entry.endswith(".py"):
                found.add(entry[:-3])
            elif entry.endswith((".so", ".pyd")):
                # Strip from the first dot: multi-part suffixes like
                # `math.cpython-39-darwin.so` are not a literal `.so`.
                found.add(entry.split(".", 1)[0])
            elif "." not in entry and "-" not in entry:
                found.add(entry)
    return frozenset(found)


BASE_AGENT_REQUIREMENTS, BASE_WORKFLOW_REQUIREMENTS = _base_requirements()
STDLIB_MODULE_NAMES = _stdlib_names()
