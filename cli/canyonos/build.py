"""
Logic for `canyonos build`: install the CanyonOS skill on a coding agent,
then launch that agent with a prompt to apply it to the current project.
"""

import os
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

from canyonos import ui
from utils.tui import select_menu

SKILL_OWNER = "CanyonCodeCoreAI"
SKILL_REPO = "canyoncodecore"
# The .car-aware skill lives only on this branch; the copies on main and every
# other branch are the older flat-layout `porting-to-canyonos-core`. Repoint at
# main once this merges -- and rename SKILL_NAME with it, since the two
# variants declare different `name:` frontmatter.
SKILL_REF = "nickhuo/porting-skill-car-layout"
SKILL_NAME = "porting-to-canyonos"
SKILL_PATH = f".claude/skills/{SKILL_NAME}"

REPO_URL = f"https://github.com/{SKILL_OWNER}/{SKILL_REPO}"
TREE_URL = f"{REPO_URL}/tree/{SKILL_REF}/{SKILL_PATH}"
TARBALL_URL = f"https://codeload.github.com/{SKILL_OWNER}/{SKILL_REPO}/tar.gz/refs/heads/{SKILL_REF}"

# The porting skill emits no otel config; without this the dashboard stays empty.
OTEL_BLOCK = """otel:
  destinations:
    - name: local
      protocol: http
      endpoint: http://host.docker.internal:3000/v1/traces
      headers: {}"""

BUILD_PROMPT = (
    f"Use the CanyonOS {SKILL_NAME} skill to convert the codebase in this directory to a canyonos-compatable format. No changes should be made to the current files, but all modifications should be put into a new .car folder."
    "\n\nFinally, add the following block verbatim to the generated config/global_controller.yaml,"
    " at the top level as a sibling of `agents:`. Copy it exactly -- `protocol` must be http, and"
    " the endpoint must keep the /v1/traces path:\n\n" + OTEL_BLOCK
)

# The leaf name of every install path must match the skill's own `name:`
# frontmatter or the agent won't resolve it.
AGENTS = {
    "claude": {
        "label": "Claude Code",
        "cli": "claude",
        "skill_dirs": {
            "local": SKILL_PATH,
            "global": os.path.expanduser(f"~/.claude/skills/{SKILL_NAME}"),
        },
    },
    "codex": {
        "label": "Codex",
        "cli": "codex",
        "skill_dirs": {
            "local": f".codex/skills/{SKILL_NAME}",
            "global": os.path.expanduser(f"~/.codex/skills/{SKILL_NAME}"),
        },
    },
}


def prompt_agent():
    options = [(key, spec["label"]) for key, spec in AGENTS.items()]
    return select_menu(options, title="Which coding agent do you want to build on?")


def prompt_scope(agent):
    dirs = AGENTS[agent]["skill_dirs"]
    options = [
        ("local", f"This project only ({dirs['local']})"),
        ("global", f"Globally ({dirs['global']})"),
    ]
    return select_menu(options, title="Where should the CanyonOS skill be installed?")


def _replace_dir(source, dest):
    """Move `source` onto `dest`, replacing whatever was there."""
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    shutil.move(source, dest)


def _fetch_with_git(dest):
    """Sparse-checkout just the skill path -- no full-repo download, no Node."""
    if not shutil.which("git"):
        return False

    with tempfile.TemporaryDirectory() as tmp:
        clone = os.path.join(tmp, "repo")
        cloned = subprocess.run(
            ["git", "clone", "--depth", "1", "--filter=blob:none", "--sparse",
             "--branch", SKILL_REF, REPO_URL, clone],
            capture_output=True,
        )
        if cloned.returncode != 0:
            return False

        sparse = subprocess.run(
            ["git", "-C", clone, "sparse-checkout", "set", SKILL_PATH],
            capture_output=True,
        )
        skill = os.path.join(clone, SKILL_PATH)
        if sparse.returncode != 0 or not os.path.isdir(skill):
            return False

        _replace_dir(skill, dest)
    return True


def _fetch_with_tarball(dest):
    """Stdlib-only fallback: pull the ref's tarball and keep the skill members.

    Needs no external tool at all, at the cost of downloading the whole repo.
    """
    prefix = f"{SKILL_PATH}/"
    with tempfile.TemporaryDirectory() as tmp:
        archive = os.path.join(tmp, "repo.tar.gz")
        try:
            with urllib.request.urlopen(TARBALL_URL, timeout=60) as response:
                with open(archive, "wb") as out:
                    shutil.copyfileobj(response, out)
        except OSError:
            return False

        staged = os.path.join(tmp, "skill")
        found = False
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                # Drop the archive's own top-level directory, whose name
                # depends on how GitHub mangles the ref.
                _, _, path = member.name.partition("/")
                if not path.startswith(prefix) or not member.isfile():
                    continue
                relative = os.path.relpath(path, SKILL_PATH)
                target = os.path.join(staged, relative)
                # Never let an archive entry write outside the staging dir.
                if not os.path.abspath(target).startswith(os.path.abspath(staged) + os.sep):
                    continue
                extracted = tar.extractfile(member)
                if extracted is None:
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as out:
                    shutil.copyfileobj(extracted, out)
                found = True

        if not found:
            return False
        _replace_dir(staged, dest)
    return True


FETCH_STRATEGIES = (
    ("git", _fetch_with_git),
    ("tarball", _fetch_with_tarball),
)


def install_skill(dest):
    """Fetch the skill into `dest`. Returns True on success."""
    for name, fetch in FETCH_STRATEGIES:
        try:
            if fetch(dest):
                ui.ok(f"Fetched the CanyonOS skill via {name}.")
                return True
        except OSError:
            pass
        ui.hint(f"{name} fetch unavailable, trying the next option...")

    ui.fail(f"Could not fetch the CanyonOS skill from {TREE_URL}.")
    ui.hint("Install git, or check network access, then run `canyonos doctor`.")
    return False


def launch_agent(agent, prompt):
    spec = AGENTS[agent]
    if not shutil.which(spec["cli"]):
        ui.fail(f"`{spec['cli']}` not found on PATH; install {spec['label']} first.")
        return
    # No check=True: the agent exiting non-zero (including the user quitting it)
    # is an ordinary outcome, not something to raise a traceback over.
    subprocess.run([spec["cli"], prompt])


def run_build():
    agent = prompt_agent()
    if agent is None:
        ui.say("Cancelled.")
        return

    scope = prompt_scope(agent)
    if scope is None:
        ui.say("Cancelled.")
        return

    dest = AGENTS[agent]["skill_dirs"][scope]
    ui.say(f"Installing CanyonOS skill for {AGENTS[agent]['label']} into {dest}...")
    if not install_skill(dest):
        return

    ui.say(f"Launching {AGENTS[agent]['label']}...")
    launch_agent(agent, BUILD_PROMPT)
