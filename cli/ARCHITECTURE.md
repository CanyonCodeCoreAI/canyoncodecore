# CanyonOS CLI — Architecture

## The one idea to keep in your head

**The CLI does almost nothing. The container does everything.**

`canyonos` is a thin client. It never builds, compiles, or runs your workflow
itself — it manages a **Global Controller (GC) container**, ships your project
into it, and drives it over a small HTTP API. Everything you see in your
terminal is the CLI *narrating* what the container is doing.

If you remember only one picture, remember this:

```
        YOU                  CLI (host)                 GLOBAL CONTROLLER (container)
         │                       │                                │
         │  canyonos deploy      │                                │
         ├──────────────────────▶│   pull + run container         │
         │                       ├───────────────────────────────▶│
         │                       │   copy project in (docker cp)  │
         │                       ├───────────────────────────────▶│  /workspace
         │                       │   POST /deploy                 │
         │                       ├───────────────────────────────▶│  canyonos build + launch
         │                       │◀── log stream (docker logs) ───┤     │
         │◀── readable progress ─┤                                │     ▼
         │                       │                          spawns Redis + agents
         │                       │                          (sibling containers)
```

---

## How the pieces connect

```
┌───────────────────────────── your machine ─────────────────────────────┐
│                                                                         │
│   ┌───────────┐        HTTP :8000         ┌──────────────────────────┐  │
│   │  canyonos │ ───── /deploy /clean ────▶│   Global Controller       │  │
│   │    CLI    │       /status /endpoints  │   container               │  │
│   │           │ ───── docker cp ─────────▶│   ├─ /workspace (a copy   │  │
│   │           │ ───── docker logs -f ────▶│   │  of your project)     │  │
│   └─────┬─────┘                           │   └─ runs `canyonos`        │  │
│         │                                 └───────────┬──────────────┘  │
│         │ docker compose                              │ docker.sock     │
│         ▼                                             ▼ (spawns siblings)│
│   ┌───────────────────────┐              ┌───────────────────────────┐  │
│   │  Dashboard stack      │◀── traces ───│  Redis + your agent /      │  │
│   │  web · api · postgres │  (OTLP)      │  workflow containers       │  │
│   └───────────────────────┘              └───────────────────────────┘  │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

Three things worth internalizing about this diagram:

1. **The container talks to the host Docker daemon.** The GC mounts the host's
   `docker.sock`, so the Redis and agent/workflow containers it launches are
   **siblings on your machine**, not nested inside it. (This is why teardown has
   to be explicit — see `stop` vs `quit` below.)
2. **Your project is a *copy*, not a live mount.** Files are `docker cp`'d into
   a named volume at `/workspace`. Editing files on the host after a deploy does
   **not** reach the running build.
3. **The dashboard is separate.** It's its own compose stack that just *renders*
   the OTLP traces your workflow emits — it isn't in the deploy critical path.

State connecting the CLI to its container is a single file:
`~/.canyonos/state.json` (container id + port). Every command that needs the
container reads it.

---

## The three commands that matter

### `build` — get your code into CanyonOS shape

```
 you ──▶ canyonos build ──▶ pick agent (Claude/Codex) + scope
                        └─▶ fetch the porting skill from GitHub
                        └─▶ launch your coding agent with it
                                      │
                                      ▼
                              generates  .car/   ◀── canyonos-formatted project
                                                     (originals untouched)
```

A **host-side, agent-driven** step. The CLI installs the CanyonOS porting skill
onto your coding agent and hands it a prompt; the agent produces a `.car/`
folder — the canyonos-ready version of your project plus its config. **No
container is involved yet.**

### `deploy` — the main path

```
 canyonos deploy
   │
   ├─ 1. start fresh   → ensure Docker up, tear down any old controller,
   │                      pull + run the GC container, save state (previous canyonos init)
   │
   ├─ 2. ship code     → docker cp your project into /workspace
   │
   ├─ 3. trigger       → POST /deploy   (container runs `canyonos`:
   │                      build stubs/images + launch the workflow)
   │
   └─ 4. narrate       → tail container logs, boil them down to phases,
                         and when the workflow reports "up":
                           • auto-start the dashboard (canyonos serve)
                           • print where everything lives
```

Everything after step 3 happens *inside* the container. The CLI's real job in
step 4 is turning a very noisy log stream (a full image-build transcript, etc.)
into a short, readable progression — and, on failure, revealing the part it had
been hiding so you can see the actual cause.

When it finishes you get a summary panel: the **dashboard URL** and each
**workflow endpoint** (`POST /main`), using the real address the container
placed the workflow at.

```
   ┌─ Deploy is live ─────────────────────────────┐
   │  Dashboard   http://127.0.0.1:8080           │
   │  POST        http://127.0.0.1:8000/main      │
   │  body        {"query": "..."}                │
   └──────────────────────────────────────────────┘
```

### `config` — view or edit settings

```
 canyonos config ──▶ View   → pretty tables of agents / otel / general
                 └─▶ Change → interactive editor (comments & order preserved)
```

The important mental model isn't the editor — it's **what a change costs you**:
canyonos config only allows you to change the config file, changing the source code requires a redeploy.

```
 change type              takes effect by...
 ───────────────────────  ───────────────────────────────────
 config value only        reloads in place  (no rebuild)
 workflow *code* changes   full redeploy     (container holds a copy)
```

---

## Lifecycle: what stays and what goes

Because the deploy spawns real sibling containers, "make it stop" has two levels of "stop":

```
                   deploy   sibling      GC         project files
                   stops?   containers?  container? (volume)?
  ───────────────  ───────  ───────────  ─────────  ─────────────
  canyonos stop      ✅        ✅          keep         keep
  canyonos quit      ✅        ✅          remove       remove
```

- **`stop`** — pause the show, keep the stage set. Redeploy without re-pulling.
- **`quit`** — full teardown. Removes the container *and* the `/workspace`
  volume (your copied files). Every `deploy` quietly does this to any previous
  controller, so each deploy starts clean.

And to observe without changing anything:

- **`logs`** — re-attach to the same live log stream `deploy` shows. Useful
  after you Ctrl+C out of a deploy: the deploy keeps running; you just stopped
  *watching*. (Ctrl+C on `logs` likewise only detaches.)

```
  deploy ──▶ (Ctrl+C) ──▶ still running in the container
     │                          ▲
     └── logs ─────────────────┘   re-attach anytime
```

---

## The whole loop, one screen

```
  cd your-project
     │
     ▼
  build     port your code → .car/            (opens your coding agent)
     │
     ▼
  deploy    build + launch in the container   (dashboard opens itself)
     │
     ├─ status   where does the workflow answer?
     ├─ config   tweak settings (live reload); redeploy for code changes
     ├─ logs     re-attach to the stream
     │
     ▼
  stop      halt the deploy, keep container + files
     or
  quit      full teardown, remove everything
```

That's the entire system: a thin CLI, one container that does the heavy
lifting, a pile of sibling containers it spawns, and a dashboard watching the
whole thing.
