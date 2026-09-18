"""
Logic for `canyonos build`: install the CanyonOS skill on a coding agent,
then launch that agent with a prompt to apply it to the current project.

--agent/--scope/-y replace the two menus, so the command also runs where there
is no tty, and -y is the unattended form whether or not one is attached. The
command's exit status is the port's: the agent can end its session having asked
a question nobody answered, so what the port produced is checked here with the
skill's own validator rather than taken on trust.
"""

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

from canyonos import env, ui
from utils.tui import select_menu

SKILL_OWNER = "CanyonCodeCoreAI"
SKILL_REPO = "canyoncodecore"
SKILL_REF = env.PROD_SKILL_REF
SKILL_NAME = "porting-to-canyonos"
SKILL_PATH = f".claude/skills/{SKILL_NAME}"

# Where the skill comes from: the ref above for a released CLI, or a directory
# in a developer's checkout (see cli/DEVELOPMENT.md).
SKILL_SOURCE = env.skill_source

REPO_URL = f"https://github.com/{SKILL_OWNER}/{SKILL_REPO}"


def _tree_url(ref):
    return f"{REPO_URL}/tree/{ref}/{SKILL_PATH}"


def _tarball_url(ref):
    # Bare ref, not refs/heads/<ref>: codeload resolves a branch, a tag or a
    # commit from this form, where the refs/heads/ one 404s for a tag.
    return f"https://codeload.github.com/{SKILL_OWNER}/{SKILL_REPO}/tar.gz/{ref}"


SCOPES = ("local", "global")
DEFAULT_AGENT = "claude"
DEFAULT_SCOPE = "local"

BUILD_PROMPT = (
    f"Use the CanyonOS {SKILL_NAME} skill to convert the codebase in this directory to a "
    "canyonos-compatible format. Put every conversion result in a new .car folder and "
    "leave the application's own source untouched. Three files outside .car are yours to "
    "update, because the port needs them and the skill's own preparation step already "
    "writes one of them: .gitignore, .env and .env.example. Read each before writing so "
    "an existing key is edited rather than appended a second time."
)

UNATTENDED_NOTE = (
    " This build is unattended: there is no terminal and nobody can answer you. "
    "Do not ask questions or request approval. Use the skill's documented unattended "
    "defaults and report the choices you made. Complete the whole porting checklist: "
    "the port is done only when validation of .car exits 0. Stop after reporting the "
    "validation result; canyonos build never deploys or asks whether to deploy. If a "
    "required decision has no safe documented default, report it as a blocker and stop "
    "without a question."
)

CAR_DIR = ".car"
VALIDATOR = "validate.py"

# The leaf name of every install path must match the skill's own `name:`
# frontmatter or the agent won't resolve it.

AGENTS = {
    "claude": {
        "label": "Claude Code",
        "cli": "claude",
        "unattended": [
            "-p",
            "--verbose",
            "--output-format",
            "stream-json",
            "--permission-mode",
            "bypassPermissions",
        ],
        "skill_dirs": {
            "local": SKILL_PATH,
            "global": os.path.expanduser(f"~/.claude/skills/{SKILL_NAME}"),
        },
    },
    "codex": {
        "label": "Codex",
        "cli": "codex",
        "unattended": ["exec", "--sandbox", "workspace-write"],
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


def _copy_from_directory(source, dest):
    """Install the skill straight from a checkout. Copies rather than moves, so
    the source survives -- including when it *is* `dest`, e.g. building inside
    this repo."""
    with tempfile.TemporaryDirectory() as tmp:
        staged = os.path.join(tmp, SKILL_NAME)
        shutil.copytree(source, staged)
        _replace_dir(staged, dest)


def _fetch_with_git(dest, ref):
    """Sparse-checkout just the skill path -- no full-repo download, no Node."""
    if not shutil.which("git"):
        return False

    with tempfile.TemporaryDirectory() as tmp:
        clone = os.path.join(tmp, "repo")
        cloned = subprocess.run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--filter=blob:none",
                "--sparse",
                "--branch",
                ref,
                REPO_URL,
                clone,
            ],
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


def _fetch_with_tarball(dest, ref):
    """Stdlib-only fallback: pull the ref's tarball and keep the skill members.

    Needs no external tool at all, at the cost of downloading the whole repo.
    """
    prefix = f"{SKILL_PATH}/"
    with tempfile.TemporaryDirectory() as tmp:
        archive = os.path.join(tmp, "repo.tar.gz")
        try:
            with urllib.request.urlopen(_tarball_url(ref), timeout=60) as response:
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
                if not os.path.abspath(target).startswith(
                    os.path.abspath(staged) + os.sep
                ):
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


def install_skill(dest, source=SKILL_SOURCE):
    """Install the skill into `dest` from `source` -- an existing directory to
    copy, otherwise a git ref to fetch. Returns True on success."""
    # `isdir` alone would read the production ref as a path in any project that
    # happens to have a directory of that name next to it.
    if source != SKILL_REF and os.path.isdir(source):
        try:
            _copy_from_directory(source, dest)
        except OSError as error:
            # An explicit local source is not something to quietly fall back
            # from: the fetch strategies would install a different skill.
            ui.fail(f"Could not copy the CanyonOS skill from {source} to {dest}.")
            ui.hint(str(error))
            return False
        ui.ok(f"Copied the CanyonOS skill from {source}.")
        return True

    for name, fetch in FETCH_STRATEGIES:
        try:
            if fetch(dest, source):
                ui.ok(f"Fetched the CanyonOS skill via {name}.")
                return True
        except OSError:
            pass
        ui.hint(f"{name} fetch unavailable, trying the next option...")

    ui.fail(f"Could not fetch the CanyonOS skill from {_tree_url(source)}.")
    ui.hint("Install git, or check network access, then run `canyonos doctor`.")
    return False


def launch_agent(agent: str, prompt: str, unattended: bool) -> int | None:
    """Run the agent over `prompt`. Returns its exit status, or None if
    there was no agent to run.

    Attended, the agent owns the screen with its own TUI. Unattended it is asked
    for a transcript instead, since nobody is watching one, and told as much in
    the prompt so it stops posing questions into an empty room.
    """
    spec = AGENTS[agent]
    if not shutil.which(spec["cli"]):
        ui.fail(f"`{spec['cli']}` not found on PATH; install {spec['label']} first.")
        return None

    # The agent's TUI wants stdin and stdout; anything less -- or a caller who
    # already said not to ask -- and it gets the unattended flags instead.
    attended = not unattended and sys.stdin.isatty() and sys.stdout.isatty()
    if not attended:
        prompt += UNATTENDED_NOTE
    argv = [spec["cli"], *([] if attended else spec["unattended"]), prompt]
    # No check=True: the agent exiting non-zero (including the user quitting it)
    # is an ordinary outcome, not something to raise a traceback over.
    return subprocess.run(argv, check=False).returncode


def report_port(skill_dir: str) -> bool:
    """Say whether the port landed, and answer True only when it did.

    The verdict is the skill's own step 4 -- its validator exiting 0 over the
    `.car` in this directory -- so a session that stopped early fails here
    instead of passing for having exited cleanly. Unverifiable is a failure:
    build cannot call a port complete on evidence it never saw.
    """
    validator = os.path.join(skill_dir, VALIDATOR)
    if not os.path.isdir(CAR_DIR):
        ui.fail(f"Port incomplete: no {CAR_DIR}/ was produced.")
        return False
    if not os.path.isfile(validator):
        ui.fail(f"Port unverified: no {VALIDATOR} in {skill_dir}.")
        return False

    check = subprocess.run(
        [sys.executable, validator, CAR_DIR],
        capture_output=True,
        text=True,
        check=False,
    )
    output = "\n".join(
        text.strip() for text in (check.stdout, check.stderr) if text.strip()
    )
    if output:
        ui.say(output)
    if check.returncode == 0:
        ui.ok(f"Port complete: {CAR_DIR}/ passed validation.")
        return True

    ui.fail(f"Port incomplete: {CAR_DIR}/ did not pass validation.")
    return False


def run_build(
    agent: str | None = None, scope: str | None = None, yes: bool = False
) -> bool:
    """Install the skill, hand the port to a coding agent, then check its work.

    True only if the agent ran to completion and the `.car` it left validates.
    A session that died says nothing about a `.car` an earlier run may have left
    in the directory, so it fails without consulting it.
    """
    # The menus read keys off stdin and draw on stderr; without both, flags are
    # the only way in.
    can_prompt = sys.stdin.isatty() and sys.stderr.isatty()
    if not yes and (agent is None or scope is None) and not can_prompt:
        ui.fail("`canyonos build` needs a terminal for the agent and scope menus.")
        ui.hint(
            f"Re-run with --agent {DEFAULT_AGENT} --scope {DEFAULT_SCOPE}, "
            "or with -y to take those defaults."
        )
        return False

    if agent is None:
        agent = DEFAULT_AGENT if yes else prompt_agent()
        if agent is None:
            ui.say("Cancelled.")
            return False

    if scope is None:
        scope = DEFAULT_SCOPE if yes else prompt_scope(agent)
        if scope is None:
            ui.say("Cancelled.")
            return False

    spec = AGENTS[agent]
    dest = spec["skill_dirs"][scope]
    ui.say(f"Installing CanyonOS skill for {spec['label']} into {dest}...")
    if not install_skill(dest):
        return False

    ui.say(f"Launching {spec['label']}...")
    status = launch_agent(agent, BUILD_PROMPT, unattended=yes)
    if status is None:
        return False
    if status != 0:
        ui.fail(f"Port incomplete: {spec['label']} exited with status {status}.")
        return False
    return report_port(dest)
