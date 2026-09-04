# Session notes: canyonos serve, OTel pipeline, examples

## Fixed (code changes, rebuilt+pushed `saakeths/canyonos:latest` where needed)

1. **`ventis/stub_generator.py`**: stubs only ever got placed at ONE path
   (nested-at-entrypoint, never flat), breaking any workflow that imports a
   sibling agent directly (`from split_agent import SplitAgent`, e.g.
   `examples/epigenomics`). Now placed at both.
2. **`otel_exporter.py`**: hardcoded Redis to `localhost`, but it runs inside
   the GC container (bridge networking) while Redis is a sibling container —
   crash-looped forever. Fixed to `host.docker.internal`.
3. **`ventis/OTLP_Exporter/db.py`**: `write_waiting_rows()` unconditionally
   priced every future via a `aws_instance_pricing` table that only exists for
   EC2 deployments — silently dropped **every** span for **every**
   `provider: local` deployment, always. Wrapped cost lookups in try/except,
   falls back to $0.
4. **`dashboard_stack.py`**: `web` port was hardcoded 8080 with no fallback.
   Now searches for a free port (reuses an already-running dashboard's port
   if one exists), same pattern as `init.py`'s GC port selection.
5. **`dashboard_stack.py`**: `database.url` was required; made optional
   (dashboard boots fine with no DB configured in the project's own config).
6. **`dashboard.compose.yml`**: added `pull_policy: never` for the local-only
   `canyonos-otel-receiver:local` image (compose was trying to pull it from a
   registry that doesn't have it). Sequenced `otel-receiver` to start after
   `api` (both raced to create `otel_spans`; `api`'s bare `CREATE TABLE` lost
   the race and crashed).

## Bundled (new, working)

- `db` (plain Postgres) + `otel-receiver` (new `Dockerfile` for
  `otlp_pg_receiver`, built locally as `canyonos-otel-receiver:local`) added
  to `dashboard.compose.yml`. Verified real spans, sent via the actual OTLP
  gRPC exporter, land in `otel_spans`.

## Workarounds applied, NOT real fixes (will resurface)

- **`canyonos quit` only tears down the GC container + volume**, never the
  deployed agent/workflow/redis containers. Had to `docker rm -f` those by
  exact name every time before a truly clean restart.
- **The named workspace volume is additive-only** (`docker cp`, never
  clears) — files from a previous project leak into the next one's build
  until you manually nuke the volume.
- **`otlp_pg_receiver` holds one Postgres connection with no reconnect
  logic** — a DB restart silently kills every future write until the
  receiver container itself is restarted.
- **`joke_writer/.car` layout** (`app/`, `config/`) doesn't match what
  `ventis/cli.py`'s build step expects (`agents/`, flat entrypoints) — worked
  around by manually copying files into the shape it wants. The real fix
  (`nickhuo/car-artifact-layout`, already pushed) was not merged in.
- **joke_writer's LLM calls are stubbed to return `"animal"`** — for testing
  only, real Bedrock creds needed to restore actual behavior (commented-out
  code left in place).

## Known, not touched

- Pre-existing OrbStack local-provider startup race (first request right
  after a container reports healthy can fail); a fix exists on an unrelated,
  unmerged branch.
- Stale global `uv tool install` is a recurring trap — always
  `uv tool install --reinstall .` after any `cli/` change.

## What's still needed to actually see data in the UI

The whole pipeline up to Postgres now genuinely works. **Nothing shows up in
the dashboard because `canyonos-api`/`canyonos-web` have zero code that reads
or displays `otel_spans`** — confirmed by inspecting their actual source
(they're a `cc-forge` rebrand: deploy/project management + a static
code-structure diagram, unrelated data model). To close the loop:

1. New API route(s) in `canyon-code-forge/packages/api` that query
   `otel_spans`.
2. New UI screen(s) in `canyon-code-forge/apps/web` to render it.
3. `web` currently has **no path to reach `api` at all** even once that
   exists — no reverse proxy in its Caddyfile, and `api`'s port isn't
   published to the host in `dashboard.compose.yml`. Needs one or the other
   before the browser can fetch anything.

All of the above is real feature work in a different repo, not a config or
wiring fix.
