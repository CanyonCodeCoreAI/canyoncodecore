# Surveying the copied source

Read this after `prepare.py` and before choosing service boundaries. Survey
`.car/app`, not a guessed abstraction of the original repository.

Identify all of the following:

1. The production entry point and callable input/output. If several
   implementations look plausible, trace imports from the documented route,
   CLI, or launch path instead of choosing by filename.
2. Framework-owned control flow: graphs, crews, chats, routing, fan-out,
   commands, and interrupts.
3. Runtime-injected stores, context, memory, sessions, and callback managers.
4. Sync/async boundaries and objects tied to an event loop.
5. The transitive import graph and the source's pinned runtime distributions.
6. Model provider, credential names, streaming, and optional `llm_proxy` use.
7. Independent work that benefits from separate resource or replica profiles.
8. Whether imports resolve with `.car/app` as `/app`; read `packaging.md` when
   they do not.
9. Non-Python runtime files such as prompts, CrewAI YAML, PDFs, templates,
   schemas, and corpora. If the runtime cannot sweep all files, report this as a
   blocker; do not embed files or rewrite paths to hide it.
10. Whether every Python file on the selected import graph parses. Existing
    syntax errors are source defects; report them and obtain approval before
    changing even the copied version.

Run `python3 <skill_dir>/validate.py .car` after the survey and after every
change. Missing or malformed required inputs fail closed. If a required runtime
capability is reported unavailable, stop instead of assuming it exists.
