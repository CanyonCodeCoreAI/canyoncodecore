"""Stage 2 — read the repo without running it.

Two jobs. It decides whether the repo is in scope for this run, and it finds the
hardcoded model ids that stage 3 has to teach the shim about, because that is the
one thing an environment variable cannot reach (DESIGN.md section 3).
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", "build", "dist"}

FRAMEWORK_MARKERS = [
    ("langgraph", ("langgraph",)),
    ("langchain", ("langchain", "langchain_core", "langchain_community")),
    ("crewai", ("crewai",)),
    ("autogen", ("autogen", "autogen_agentchat")),
    ("adk", ("google.adk", "google_adk")),
]

# Ordered: the first two decide which base URL stage 3 writes, so they must be
# recognised by more than their own package name — a repo commonly reaches a
# provider through a wrapper, and matching only `openai`/`anthropic` misses it.
SDK_MARKERS = [
    ("openai", ("openai", "langchain_openai", "llama_index.llms.openai")),
    ("anthropic", ("anthropic", "langchain_anthropic", "llama_index.llms.anthropic")),
    ("bedrock", ("boto3", "botocore", "langchain_aws", "ventis.llm", "ventis")),
    # Reaches a model, but not through a provider SDK we can redirect by env var.
    ("other", ("litellm", "instructor", "google.generativeai", "google.genai",
               "cohere", "mistralai", "ollama", "langchain.chat_models")),
]

# Model ids as they appear in source. Deliberately broad: a literal this matches
# is a candidate for the shim's mapping table, and a human reads the list before
# the run. Missing one is a stage 8 provider error; over-matching costs nothing.
MODEL_LITERAL = re.compile(
    r"\b(gpt-[\w.\-]+|o[134](?:-[\w.\-]+)?|claude-[\w.\-]+|"
    r"(?:meta|mistral|amazon|cohere|anthropic|openai|qwen|deepseek)\.[\w.\-:]+)\b"
)

MULTIAGENT_MARKERS = ("Send(", "StateGraph", "Crew(", "GroupChat", "add_edge", "Command(")


@dataclass
class Screen:
    py_files: int = 0
    loc: int = 0
    framework: str = "plain"
    llm_sdk: str = "none"
    model_ids: list[str] = field(default_factory=list)
    is_multiagent: bool = False
    layout: str = "flat"
    packaging: str = "none"
    description: str = ""
    reject: str | None = None


def _imports(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _matches(imports: set[str], markers: tuple[str, ...]) -> bool:
    return any(i == m or i.startswith(m + ".") for i in imports for m in markers)


def screen(root: Path, max_py_files: int = 200, max_loc: int = 40_000) -> Screen:
    out = Screen()
    imports: set[str] = set()
    models: set[str] = set()

    for path in root.rglob("*.py"):
        if SKIP_DIRS & set(path.relative_to(root).parts):
            continue
        out.py_files += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        out.loc += text.count("\n")
        models.update(MODEL_LITERAL.findall(text))
        if any(m in text for m in MULTIAGENT_MARKERS):
            out.is_multiagent = True
        try:
            imports |= _imports(ast.parse(text))
        except SyntaxError:
            # Not fatal to the screen. validate.py's V001/V002 is where a file
            # that does not parse becomes a finding about the port.
            continue

    out.model_ids = sorted(models)

    for name, markers in FRAMEWORK_MARKERS:
        if _matches(imports, markers):
            out.framework = name
            break

    hits = [name for name, markers in SDK_MARKERS if _matches(imports, markers)]
    if {"openai", "anthropic"} <= set(hits):
        out.llm_sdk = "both"
    elif hits:
        out.llm_sdk = hits[0]

    if (root / "src").is_dir():
        out.layout = "src"
    for candidate in ("pyproject.toml", "setup.py", "setup.cfg"):
        if (root / candidate).is_file():
            out.packaging = candidate
            break

    readme = next((p for p in root.glob("README*") if p.is_file()), None)
    if readme:
        body = readme.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        out.description = " ".join(line for line in body[:12] if line.strip())[:500]

    # Rejections. Each is a fact about scope, not a failure of the skill, so the
    # test row records `screened` as the furthest step and stops there.
    if out.py_files == 0:
        out.reject = "no python files"
    elif out.py_files > max_py_files or out.loc > max_loc:
        out.reject = f"too large: {out.py_files} files, {out.loc} loc"
    elif out.llm_sdk == "none" and not out.model_ids:
        # Both signals absent. One alone is not enough to reject on: a wrapper
        # hides the SDK, and a model id read from config leaves no literal.
        out.reject = "no LLM call found"
    elif out.layout == "src" and out.packaging == "none":
        # M24: without packaging metadata there is no editable install, and the
        # Ventis change that would make a src/ layout importable has no PR. The
        # port cannot succeed, and that is a finding about Ventis, not the skill.
        out.reject = "src/ layout with no packaging metadata (M24, no PR)"

    return out
