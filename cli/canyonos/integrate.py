"""
Logic for `canyonos integrate`: install the CanyonOS skill on a coding agent,
then launch that agent with a prompt to apply it to the current project.
"""

import os
import shutil
import subprocess

from rich.console import Console

from utils.tui import select_menu

# Points at the skill's folder, so SKILL.md and references/ both come along.
SKILL_SOURCE_URL = "https://github.com/CanyonCodeCoreAI/canyoncodecore/tree/nickhuo/porting-skill-car-layout/.claude/skills/porting-to-canyonos-core"

INTEGRATE_PROMPT = (
    "Use the CanyonOS porting-to-canyonos-core skill to convert the codebase in this directory to a canyonos-compatable format. No changes should be made to the current files, but all modifications should be put into a new .car folder."
)

AGENTS = {
    "claude": {
        "label": "Claude Code",
        "cli": "claude",
        # Claude Code auto-loads project-local skills from here.
        "skill_dir": ".claude/skills/porting-to-canyonos-core",
    },
    "codex": {
        "label": "Codex",
        "cli": "codex",
        # Codex only auto-loads skills from the user's home directory, not per-project.
        "skill_dir": os.path.expanduser("~/.codex/skills/porting-to-canyonos-core"),
    },
}


def prompt_agent():
    options = [(key, spec["label"]) for key, spec in AGENTS.items()]
    return select_menu(options, title="Which coding agent do you want to integrate with?")


def install_skill(agent):
    spec = AGENTS[agent]
    # -f overwrites an existing skill dir; without it gitpick exits 1 when the
    # target already exists and is non-empty (e.g. re-running `integrate`).
    subprocess.run(
        ["npx", "-y", "gitpick", "-f", SKILL_SOURCE_URL, spec["skill_dir"]],
        check=True,
    )


def launch_agent(agent, prompt):
    spec = AGENTS[agent]
    if not shutil.which(spec["cli"]):
        print(f"`{spec['cli']}` not found on PATH; install {spec['label']} first.")
        return
    subprocess.run([spec["cli"], prompt], check=True)


def run_integrate():
    console = Console()
    agent = prompt_agent()
    if agent is None:
        console.print("Cancelled.")
        return

    console.print(f"Installing CanyonOS skill for {AGENTS[agent]['label']}...")
    install_skill(agent)

    console.print(f"Launching {AGENTS[agent]['label']}...")
    launch_agent(agent, INTEGRATE_PROMPT)
