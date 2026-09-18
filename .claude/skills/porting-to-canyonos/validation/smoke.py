"""V040, V041 -- the declared requirements install, and what runs imports.

Every other check here reads the port. These two run it: they build the image's
declared dependency set in a throwaway environment and load the module the
container loads. That is the only way to see two failures a static walk cannot,
and both of them reach the user as a container that builds, starts, reports
healthy, and fails on its first request:

* a set pip resolves happily whose *generated* code refuses to load -- protobuf
  gencode 6.33.5 against a 5.29.6 runtime, where the constraint lives inside the
  generated module and in no metadata pip can read;
* a pin whose sibling floated to an incompatible release --
  `llama-index-llms-openai==0.1.15` beside a newer `llama-index-core`, which
  imports and then fails at `Can't instantiate abstract class OpenAI`.
"""

import os
import shutil
import subprocess
import tempfile

from validation.runtime import RUNTIME_FLAT_NAMES

INSTALL_TIMEOUT_SECONDS = 900
IMPORT_TIMEOUT_SECONDS = 180

# The images are built `FROM python:3.11-slim`. Resolving against whatever the
# host runs reports build failures for wheels that exist for 3.11 -- numpy
# 1.26.4 has none for 3.13, and reporting that would blame the port for the
# host.
IMAGE_PYTHON = "3.11"

# Mirrors LocalController._load_agent: the dotted name comes from the path so
# package-relative imports inside a nested entrypoint resolve, and the class is
# instantiated, because that is where an ABI mismatch actually raises.
_LOADER = """
import importlib.util, os, sys, traceback
sys.path.insert(0, os.getcwd())
sys.path.insert(0, {overlay!r})
rel = {rel!r}
name = rel[:-3].replace(os.sep, ".").replace("/", ".")
spec = importlib.util.spec_from_file_location(name, os.path.abspath(rel))
module = importlib.util.module_from_spec(spec)
sys.modules[name] = module
spec.loader.exec_module(module)
cls = {cls!r}
if cls:
    getattr(module, cls)()
print("ok")
"""


def _scratch_root():
    """Where to build the throwaway environments.

    Not the system temp directory: it is a tmpfs of a few GB on these hosts, and
    one dependency set with pandas in it fills the rest, which surfaces as
    "Disk quota exceeded" and reads exactly like a broken port. Beside uv's own
    cache instead, which has room and lets uv hardlink into the venv rather than
    copying every file.
    """
    root = os.path.join(os.path.expanduser("~"), ".cache", "canyonos-smoke")
    try:
        os.makedirs(root, exist_ok=True)
        return root
    except OSError:
        return None


def _env_file_values(source_dir):
    """The env_file the container is handed, so construction sees what it sees.

    An SDK client that raises "Did not find openai_api_key" during `__init__`
    does so here and not in the container, which has the file. Reading it back
    is the only way this check tells a broken dependency set apart from a
    credential the image would have had.
    """
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(source_dir)))
    path = os.path.join(project_root, ".env")
    values = {}
    if not os.path.isfile(path):
        return values
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _tail(text, lines=6, width=600):
    kept = [line for line in (text or "").splitlines() if line.strip()][-lines:]
    return (" / ".join(kept))[:width] or "(no output)"


def _stub_overlay(directory, stub_entrypoints):
    """Write what the image provides but this checkout does not.

    Two kinds. The runtime's own flat modules -- `deploy`, `local_controller`
    and the rest -- live at /app in every image and nowhere here, so importing
    without them fails on the runtime rather than on the port. And each agent's
    stub, which `canyonos build` puts over its entrypoint path *and* flat at the
    context root: importing the real entrypoint instead would drag in that
    agent's dependencies, which the workflow image neither installs nor needs.
    """
    for runtime_module in RUNTIME_FLAT_NAMES:
        with open(
            os.path.join(directory, runtime_module), "w", encoding="utf-8"
        ) as handle:
            handle.write("def __getattr__(name):\n    return lambda *a, **k: None\n")
    for entrypoint, name in (stub_entrypoints or {}).items():
        body = f"class {name}:\n    def __getattr__(self, item):\n        return None\n"
        for rel in {entrypoint, os.path.basename(entrypoint)}:
            path = os.path.join(directory, rel)
            os.makedirs(os.path.dirname(path) or directory, exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(body)
            package = os.path.dirname(path)
            while package and os.path.realpath(package) != os.path.realpath(directory):
                init = os.path.join(package, "__init__.py")
                if not os.path.exists(init):
                    open(init, "w", encoding="utf-8").close()
                package = os.path.dirname(package)


def check_installs_and_imports(
    report,
    source_dir,
    entry,
    module_rel,
    base_requirements,
    config_path,
    agent_class=None,
    stub_entrypoints=None,
):
    """V040/V041 -- install this image's requirements, then load its module."""
    if not shutil.which("uv"):
        report.warn(
            "V040",
            config_path,
            0,
            "uv is not on PATH, so the declared requirements were never installed",
            "Without this the port ships on a static import walk alone, which "
            "cannot see a set that resolves but does not import.",
        )
        return

    declared = [
        item for item in (entry.get("requirements") or []) if isinstance(item, str)
    ]
    name = entry.get("name") or module_rel

    workdir = tempfile.mkdtemp(prefix="canyonos-smoke-", dir=_scratch_root())
    try:
        venv = os.path.join(workdir, "venv")
        reqs = os.path.join(workdir, "requirements.txt")
        with open(reqs, "w", encoding="utf-8") as handle:
            handle.write("\n".join(list(base_requirements) + declared) + "\n")

        env = dict(os.environ, VIRTUAL_ENV=venv)
        try:
            subprocess.run(
                ["uv", "venv", venv, "-q", "--python", IMAGE_PYTHON],
                capture_output=True,
                timeout=INSTALL_TIMEOUT_SECONDS,
                check=True,
            )
            install = subprocess.run(
                ["uv", "pip", "install", "-q", "-r", reqs],
                capture_output=True,
                text=True,
                env=env,
                timeout=INSTALL_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            report.warn(
                "V040",
                config_path,
                0,
                f"installing {name}'s requirements timed out",
                "Too slow to verify here; deploy will be the first "
                "attempt at this dependency set.",
            )
            return
        except subprocess.CalledProcessError as error:
            report.warn(
                "V040",
                config_path,
                0,
                f"could not create a virtualenv to check {name}: {error}",
                "The requirements were not verified.",
            )
            return

        if install.returncode != 0:
            report.error(
                "V040",
                config_path,
                0,
                f"{name}'s declared requirements do not install: "
                f"{_tail(install.stderr or install.stdout)}",
                "`canyonos deploy` runs this same resolution inside the image "
                "build. A set that cannot install here cannot deploy, and the "
                "failure arrives after the port is reported finished.",
            )
            return

        overlay = os.path.join(workdir, "stubs")
        os.makedirs(overlay, exist_ok=True)
        _stub_overlay(overlay, stub_entrypoints)

        python = os.path.join(venv, "bin", "python")
        run_env = dict(env, **_env_file_values(source_dir), PYTHONPATH=overlay)
        try:
            loaded = subprocess.run(
                [
                    python,
                    "-c",
                    _LOADER.format(rel=module_rel, cls=agent_class, overlay=overlay),
                ],
                capture_output=True,
                text=True,
                cwd=source_dir,
                env=run_env,
                timeout=IMPORT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            report.warn(
                "V041",
                os.path.join(source_dir, module_rel),
                0,
                f"importing {name} timed out",
                "Module-level work that slow will also delay every container start.",
            )
            return

        if loaded.returncode != 0:
            report.error(
                "V041",
                os.path.join(source_dir, module_rel),
                0,
                f"{name} installs but does not load: {_tail(loaded.stderr)}",
                "This is what the container does at startup, so the same "
                "traceback is what `canyonos deploy` produces -- after the "
                "build has passed and the port has been reported finished.",
            )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
