# Survey the copied source

**When:** after `prepare.py`, before writing adapters or configuration.

**Inspect:** `.car/app`, not a framework-based guess about the original tree.

**Output:** a short survey record and the smallest useful service map. Do not
start adapter or config work until each section is resolved or marked as a
blocker.

## 1. Public behavior

Record:

- the production entrypoint and callable input/output;
- the documented route, CLI, or launch path that proves this is the entrypoint;
- prompts, tools, schemas, parsing, retries, model clients, and node bodies that
  remain source-owned.

If multiple implementations look plausible, trace imports from the documented
launch path instead of choosing by filename.

## 2. Control flow and state

Record:

- framework-owned graphs, crews, chats, routing, fan-out, commands, and
  interrupts;
- independent work that may justify separate resources or replicas;
- injected stores, context, memory, sessions, checkpointers, and callback
  managers;
- sync/async boundaries and objects tied to an event loop.

This evidence feeds the service-boundary decision below and `adapter.md`.

## 3. Imports and runtime inputs

Record:

- the transitive import graph from the selected entrypoint;
- source lockfiles and pinned runtime distributions;
- whether every import resolves with `.car/app` mounted as `/app`;
- model providers, credential variable names, streaming calls, and optional
  `llm_proxy` use;
- non-Python files opened at runtime: prompts, framework YAML, PDFs, templates,
  schemas, certificates, and corpora.

Use the import-root and runtime-assets sections of `preparation.md` when imports
do not resolve, packaging metadata matters, or runtime code reads non-Python
files.

## 4. Capability questions

Record only capabilities that affect this source, such as editable installation,
full-file sweeping, or environment-file injection. Inspect the installed runtime
or its capability probe; do not run validation merely to reconfirm properties
already guaranteed by `prepare.py`.

An existing syntax error on the selected import graph is a source defect;
obtain approval before changing even the copied version. The final gap validator
checks authored runtime code and cross-file bindings after the port is complete.
If a required runtime capability is unavailable, report a blocker instead of
assuming support.

## 5. Choose service boundaries

**Output:** the smallest useful service map, including which framework edges
cross services and which services require `replicas: 1`.

Start with one service. Split only when doing so creates independently parallel
work or a genuinely distinct resource or replica profile.

- Keep a ReAct loop together; each turn needs shared message history.
- Hoist supervisor task lists and `Send`-style fan-out into the workflow.
- Do not create a one-replica service with no distinct resource profile merely
  to mirror every source graph node.

Rewrite framework-owned edges as ordinary Python **only where they cross a
service boundary**. If every graph node stays in one service, preserve
`graph.compile().invoke(...)` and wrap it. Rewriting internal edges restates
working source behavior without creating a deployment benefit. Where an edge
must move into the workflow, import the connected source node functions
unchanged.

Construct runtime-injected service objects from source configuration. Never
invent models, embedding dimensions, stores, or defaults silently; report any
choice the source does not specify.

A service object that holds state across requests—for example a vector store,
memory, or checkpointer created in `__init__`—requires `replicas: 1` for
correctness. The controller can route each call to a different replica, and
those replicas cannot see one another's in-memory state. Record this as a
constraint in the configuration review and handoff, not as a sizing preference.
