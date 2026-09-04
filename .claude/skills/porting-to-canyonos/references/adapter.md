# Writing what goes into `.car/app`

Read this before writing any adapter. Every rule here can pass static build
checks and fail only when a container loads -- which is why the trigger is the
step, not a symptom.

## Contents

- Choosing the entrypoint
- Bridging async
- Multi-turn and session state

## Choosing the entrypoint

The entrypoint is the one module in the copy the build destroys: each agent's
stub is written over its own `entrypoint` path in every image except that
agent's own, so everywhere else that path holds a generated class and nothing
else. Every gate below applies to whichever module you pick; each one
disqualifies it as it stands, so fix the module or point `entrypoint` at
another.

1. **Does anything else the deployment imports read this module for its real
   contents?** The workflow, or any module the workflow imports at module scope.
   If yes, the stub replaces it there and the workflow dies at container
   startup. Put the adapter in a sibling module that imports this one and point
   `entrypoint` at the sibling. This is why "edit the copied module in place" is
   a preference and not a rule: it is right only for a module nothing else
   imports.
2. **Does the module's package `__init__.py` re-export a name from it?**
   (`from .graph import graph`) Python runs `__init__.py` before any submodule,
   so every peer image that touches that package re-runs a re-export the stub
   cannot satisfy and raises ImportError at startup. Point `entrypoint` at a
   module the `__init__` does not re-export from; add one if it re-exports from
   all of them. V033.
3. **Is every segment of the path a Python identifier?** `travel-planner.py` and
   `steps/06_agent.py` load fine -- the controller loads by file path -- but the
   workflow's `from steps.06_agent import X` is a SyntaxError, which no import
   guard catches. Rename the file inside the copy, or add a normally-named
   sibling that loads it by path and re-exposes the class. V034.
4. **Does the module use relative imports?** (`from . import data_service`) The
   controller loads the entrypoint with `spec_from_file_location`, which leaves
   `__package__` empty, so every relative import *in the entrypoint itself*
   fails at agent load. Make its top-level imports absolute; the modules it
   imports keep theirs. V035.
5. **Does module-level code perform a real run?** A script ending in
   `result = crew.kickoff(...)` / `print(result)` fires that run whenever the
   module loads, before a request exists. Delete the
   invocation and keep the construction: M18 protects prompts, tools, schemas
   and model calls, not a script's own main body.

## Bridging async

Use one `asyncio.run(...)` per declared method, at the method boundary, around
the whole call. One per awaited coroutine builds a fresh event loop and a fresh
connection pool per graph superstep.

If the instance holds anything bound to a loop -- an `asyncio.Lock`, a client
constructed inside a coroutine -- `asyncio.run` cannot be used at all. The
runtime calls declared methods repeatedly on one instance, and the second call
raises `Lock is bound to a different event loop`. Run one persistent loop on a
background thread and submit with `run_coroutine_threadsafe`. Seed any
`ContextVar` the source's async code reads on the calling thread immediately
before submitting: `call_soon_threadsafe` copies the context at schedule time,
not inside the loop.

## Multi-turn and session state

The platform sends one `{query: string}` per request and keeps nothing between
them. A source with per-conversation state -- a `thread_id`, a checkpointer, a
memory keyed by session -- carries that id *inside* `query`: accept either a
bare string or a JSON object in that one field and pass the id through to the
source unchanged. Do not add a second workflow parameter for it; the platform
never sends one.
