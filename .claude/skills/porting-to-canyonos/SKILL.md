---
name: porting-to-canyonos
description: Port existing Python agents—including LangChain, LangGraph, CrewAI, AutoGen, and custom implementations—to CanyonOS Core. Use for migrations, `.car` packaging, adapter and workflow generation, validation, deployment, or diagnosing build and runtime failures. Creates and validates a self-contained `.car`, then requires explicit approval before `canyonos deploy`.
---

# Port an agent project to CanyonOS

Requires Python, Docker, and the `canyonos` CLI. `prepare.py` uses the Python
standard library; `validate.py` also requires `pyyaml`.

## Progress

Copy this checklist into the response and update it while working:

```text
Port progress:
- [ ] 1. Prepare `.car`
- [ ] 2. Survey the copy and choose service boundaries
- [ ] 3. Write adapters, workflow, declarations, and reviewed configuration
- [ ] 4. Gap validation exits 0; report readiness and stop
```

## 1. Prepare `.car`

Read [references/preparation.md](references/preparation.md) in full. Choose the
import root from actual imports and use `prepare.py`; do not assemble or refresh
`.car` manually. After preparation, edit only `.car`.

## 2. Survey and design

Read [references/source-survey.md](references/source-survey.md) in full. Produce
its survey record and choose the smallest useful service map before writing
runtime code or configuration.

## 3. Implement the port

Read [references/adapter.md](references/adapter.md) before changing `.car/app`
and [references/manifest.md](references/manifest.md) before changing
`.car/config`. Keep this binding exact:

```text
config entry name == yaml agent.name == entrypoint class name
```

Use the View/Change configuration flow in `manifest.md`; prefer
`canyonos config` in an interactive terminal. Preserve the source-integrity
boundary defined in `preparation.md`.

Read these only when triggered:

- [references/llm-proxy.md](references/llm-proxy.md) when the target uses
  `llm_proxy`.
- [references/ec2.md](references/ec2.md) when any entry uses `provider: EC2`.

## 4. Gap validation and stop

Read
[references/validation-and-deploy.md](references/validation-and-deploy.md).
Validate only authored contracts that CanyonOS tooling does not strongly
guarantee. Fix every reported error, hand off warnings and blockers, then stop.
Do not run `canyonos deploy` without explicit user approval.

## Diagnose an approved deployment

After an explicitly approved deployment fails, start with
[references/troubleshooting.md](references/troubleshooting.md). Read
[references/runtime-contract.md](references/runtime-contract.md) only when a
runtime mechanism or validator finding needs explanation.
