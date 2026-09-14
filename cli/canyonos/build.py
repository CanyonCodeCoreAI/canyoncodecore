"""
Logic for `canyonos build`: install the CanyonOS skill on a coding agent,
then launch that agent with a prompt to apply it to the current project.

--agent/--scope/-y replace the two menus, so the command also runs where there
is no tty. How the agent itself is launched is read off the terminal rather
than off a flag -- see `launch_agent`. Nothing here reports through an exit
code: what happened is in the output, which for an unattended run is the
agent's own transcript.
"""

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

from canyonos import ui
from utils.tui import select_menu

SKILL_OWNER = "CanyonCodeCoreAI"
SKILL_REPO = "canyoncodecore"
SKILL_REF = "main"
SKILL_NAME = "porting-to-canyonos"
SKILL_PATH = f".claude/skills/{SKILL_NAME}"

REPO_URL = f"https://github.com/{SKILL_OWNER}/{SKILL_REPO}"
TREE_URL = f"{REPO_URL}/tree/{SKILL_REF}/{SKILL_PATH}"
TARBALL_URL = f"https://codeload.github.com/{SKILL_OWNER}/{SKILL_REPO}/tar.gz/refs/heads/{SKILL_REF}"

DEFAULT_AGENT = "claude"
DEFAULT_SCOPE = "local"

BUILD_PROMPT = f"Use the CanyonOS {SKILL_NAME} skill to convert the codebase in this directory to a canyonos-compatable format. No changes should be made to the current files, but all modifications should be put into a new .car folder."

# The leaf name of every install path must match the skill's own `name:`
# frontmatter or the agent won't resolve it.

AGENTS = {
    "claude": {
        "label": "Claude Code",
        "cli": "claude",
        "unattended": ["-p", "--verbose", "--output-format", "stream-json"],
        "skill_dirs": {
            "local": SKILL_PATH,
            "global": os.path.expanduser(f"~/.claude/skills/{SKILL_NAME}"),
        },
    },
    "codex": {
        "label": "Codex",
        "cli": "codex",
        "unattended": ["exec"],
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


def _attended():
    """True if a person can both see this run and type at it."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def launch_agent(agent, prompt):
    """Run the coding agent over `prompt`.
    """
    spec = AGENTS[agent]
    if not shutil.which(spec["cli"]):
        ui.fail(f"`{spec['cli']}` not found on PATH; install {spec['label']} first.")
        return

    attended = _attended()
    argv = [spec["cli"], *([] if attended else spec["unattended"]), prompt]
    # No check=True: the agent exiting non-zero (including the user quitting it)
    # is an ordinary outcome, not something to raise a traceback over.
    subprocess.run(argv)


def _needs_menu(agent, scope, yes):
    """True if resolving the options would have to open a menu."""
    return not yes and (agent is None or scope is None)


def run_build(agent=None, scope=None, yes=False):
    """Install the skill and hand the port to a coding agent."""
    if _needs_menu(agent, scope, yes) and not sys.stdin.isatty():
        ui.fail("`canyonos build` needs a terminal for the agent and scope menus.")
        ui.hint(
            f"Re-run with --agent {DEFAULT_AGENT} --scope {DEFAULT_SCOPE}, "
            "or with -y to take those defaults."
        )
        return

    if agent is None:
        agent = DEFAULT_AGENT if yes else prompt_agent()
    if agent is None:
        ui.say("Cancelled.")
        return

    if scope is None:
        scope = DEFAULT_SCOPE if yes else prompt_scope(agent)
    if scope is None:
        ui.say("Cancelled.")
        return

    dest = AGENTS[agent]["skill_dirs"][scope]
    ui.say(f"Installing CanyonOS skill for {AGENTS[agent]['label']} into {dest}...")
    if not install_skill(dest):
        return

    ui.say(f"Launching {AGENTS[agent]['label']}...")
    launch_agent(agent, BUILD_PROMPT)
