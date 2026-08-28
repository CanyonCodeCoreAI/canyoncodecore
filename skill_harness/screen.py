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

# `.claude` is here because the harness copies the skill under test into the repo
# at stage 4. Screening a tree that has already been through a run would
# otherwise read validate.py's own imports as the repo's.
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".tox",
             "build", "dist", ".claude"}

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

# Model ids as they appear in source. A literal this matches is a candidate for
# the shim's mapping table, which a human reads before the run — so a miss costs
# a stage 8 provider error, and a false match costs that human's attention.
MODEL_LITERAL = re.compile(
    r"\b(gpt-[\w.\-]+|o[134](?:-[\w.\-]+)?|claude-[\w.\-]+|"
    # A vendor-prefixed Bedrock id ends in a version marker (`-v1:0`, `-1:0`,
    # `-2507`). Requiring one keeps `meta.com` and other hostnames out; without
    # it the list a human reads to build the model map fills with domains.
    r"(?:meta|mistral|amazon|cohere|anthropic|openai|qwen|deepseek)"
    r"\.[\w.\-]*(?:v?\d+(?::\d+)?|\d{4}))\b"
)

# LangChain's `init_chat_model` picks its provider at runtime from a
# "<provider>/<model>" string, so a repo can depend entirely on Anthropic while
# importing nothing named anthropic. Every LangGraph template is built this way
# and most default to Claude -- reading only the imports classifies them as
# having no provider at all.
PROVIDER_STRING = re.compile(
    r"[\"']((?:anthropic|openai|google_genai|google_vertexai|bedrock|bedrock_converse|"
    r"cohere|mistralai|fireworks|groq|ollama|together|deepseek|xai)[:/][\w.\-:]+)[\"']"
)

MULTIAGENT_MARKERS = ("Send(", "StateGraph", "Crew(", "GroupChat", "add_edge", "Command(")

# Backing services the harness does not stand up. A repo that reads one of these
# gets as far as a real request and then fails on a credential -- which is a fact
# about the repo's dependencies, not about the port, and costs a whole agent
# budget to discover. Redis is absent from the list: ventis deploy provides it.
EXTERNAL_SERVICE_VARS = re.compile(
    r"\b(ELASTICSEARCH_(?:URL|API_KEY|USER|PASSWORD)|PINECONE_[A-Z_]+|MONGODB_[A-Z_]+|"
    r"WEAVIATE_[A-Z_]+|QDRANT_[A-Z_]+|CHROMA_[A-Z_]+|SUPABASE_[A-Z_]+|"
    r"TAVILY_[A-Z_]+|SERPAPI_[A-Z_]+|EXA_API_KEY|FIRECRAWL_[A-Z_]+|"
    r"LANGSMITH_[A-Z_]+|POSTGRES_[A-Z_]+|DATABASE_URL)\b"
)


@dataclass
class Screen:
    py_files: int = 0
    root_py_files: int = 0
    loc: int = 0
    framework: str = "plain"
    llm_sdk: str = "none"
    model_ids: list[str] = field(default_factory=list)
    is_multiagent: bool = False
    external_services: list[str] = field(default_factory=list)
    # "<provider>/<model>" literals -- what init_chat_model resolves at runtime.
    provider_hints: list[str] = field(default_factory=list)
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


def editable_install_available() -> bool:
    """Whether the Ventis under test can install the source as a package.

    Asked of the code rather than assumed, the same way validate.py asks it.
    Without it, M24 holds in its strict form: only modules that land flat at
    /app import at all, and packaging metadata rescues nothing.
    """
    try:
        from ventis import stub_generator
    except Exception:  # noqa: BLE001 - a missing install must not crash the screen
        return False
    return hasattr(stub_generator, "_install_step")


def screen(root: Path, max_py_files: int = 200, max_loc: int = 40_000,
           surfaces: frozenset[str] = frozenset({"openai", "anthropic"}),
           editable_install: bool | None = None) -> Screen:
    """`surfaces` is which Bedrock wire formats the credential can actually reach.

    It is an account property, not a property of Bedrock: a Messages-API model
    can be listed by the control plane and still answer `permission_error`. A
    repo whose SDK needs a closed surface is rejected here rather than after an
    agent has spent its budget porting it.

    `editable_install` is the matching question for M24, asked of the Ventis under
    test rather than assumed.
    """
    if editable_install is None:
        editable_install = editable_install_available()
    out = Screen()
    imports: set[str] = set()
    models: set[str] = set()
    hints: set[str] = set()
    services: set[str] = set()

    for path in root.rglob("*.py"):
        if SKIP_DIRS & set(path.relative_to(root).parts):
            continue
        out.py_files += 1
        if path.parent == root:
            out.root_py_files += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        out.loc += text.count("\n")
        models.update(MODEL_LITERAL.findall(text))
        hints.update(PROVIDER_STRING.findall(text))
        services.update(EXTERNAL_SERVICE_VARS.findall(text))
        if any(m in text for m in MULTIAGENT_MARKERS):
            out.is_multiagent = True
        try:
            imports |= _imports(ast.parse(text))
        except SyntaxError:
            # Not fatal to the screen. validate.py's V001/V002 is where a file
            # that does not parse becomes a finding about the port.
            continue

    out.model_ids = sorted(models)
    out.provider_hints = sorted(hints)
    # LangSmith is observability, not a dependency the agent needs to answer.
    out.external_services = sorted(s for s in services if not s.startswith("LANGSMITH_"))

    for name, markers in FRAMEWORK_MARKERS:
        if _matches(imports, markers):
            out.framework = name
            break

    hits = [name for name, markers in SDK_MARKERS if _matches(imports, markers)]

    # Both signals count, and neither may short-circuit the other. A repo can
    # import langchain_openai for its embeddings while its chat model comes from
    # init_chat_model("anthropic/..."), and letting the import win would report
    # such a repo as openai-only and send it to an agent it cannot finish.
    named = {h.split("/")[0].split(":")[0] for h in out.provider_hints}
    redirectable = (set(hits) | named) & {"openai", "anthropic"}
    if len(redirectable) == 2:
        out.llm_sdk = "both"
    elif redirectable:
        out.llm_sdk = redirectable.pop()
    elif hits:
        out.llm_sdk = hits[0]

    # `flat` means the port can import the source, which is a fact about where
    # modules sit relative to the project root -- not about whether a `src/`
    # directory happens to exist.
    if out.root_py_files:
        out.layout = "flat"
    elif (root / "src").is_dir():
        out.layout = "src"
    else:
        out.layout = "nested"
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
    elif out.llm_sdk in ("openai", "anthropic") and out.llm_sdk not in surfaces:
        out.reject = f"{out.llm_sdk} surface unavailable on this credential"
    elif out.llm_sdk == "both" and not {"openai", "anthropic"} <= surfaces:
        out.reject = "needs both surfaces; only " + ",".join(sorted(surfaces))
    elif out.root_py_files == 0 and not editable_install:
        # M24 in its strict form. With no editable install, an adapter can import
        # only what lands flat at /app, so a tree whose modules all sit under
        # sub-directories has no port this Ventis can load -- whatever its
        # packaging says. Deciding it here costs nothing; letting it through
        # costs an agent's whole budget to reach the same conclusion.
        out.reject = (f"no module at the project root ({out.py_files} .py files, "
                      f"all nested) and no editable install (M24)")
    elif out.external_services:
        out.reject = ("needs backing services this harness does not provide: "
                      + ", ".join(out.external_services[:4]))
    elif out.root_py_files == 0 and out.packaging == "none":
        # The editable install exists, but nothing tells it what the root is.
        out.reject = "no module at the project root and no packaging metadata (M24)"

    return out
