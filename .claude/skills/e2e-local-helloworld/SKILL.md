---
name: e2e-local-helloworld
description: Run a full local end-to-end test of the `helloworld` example -- `ventis build`, `ventis deploy` fully locally (provider: local, no database, no otel hooks), fire 50 requests at the workflow endpoint, poll them to completion, tear down, and report pass/fail. Use when the user asks to "run an e2e test", "test the controller/reconciler end to end", "deploy helloworld locally and hit it", or similar against this repo's own code (not a remote/staging system).
---

# E2E Local Helloworld

Builds and deploys `examples/helloworld` entirely on this machine (Docker containers, no EC2,
no Postgres/sqlite, no OTel exporters), sends 50 requests to its `Workflow` endpoint, and reports
whether they all completed. This is a skeleton: it covers one config, one query, and no chaos
(killing an instance mid-run to watch the reconciler replace it) -- extend it as those needs show up.

## Prerequisites

- Docker (or an OrbStack/equivalent daemon) running locally.
- `pip install -e .` (or equivalent) so `ventis` resolves to this checked-out code -- the point of
  this skill is to test *this repo's* controller/reconciler, not a released version.
- Docker and port-binding calls need real network/socket access the sandbox blocks, so run every
  step below with `dangerouslyDisableSandbox: true`.

Uses `examples/helloworld/config/e2e_local.yaml` -- a fixture that mirrors
`config/global_controller.yaml` with `provider: local` everywhere and the `database`/`ec2` blocks
dropped. If the real config gains a new agent, update this fixture by hand; it does not derive
itself.

## Step 0 -- clean slate

Idempotent: removes only the containers this skill itself would create, so a previous failed run
never blocks a new one.

```bash
docker rm -f ventis-redis-localhost ventis-local-exampleagent-0 ventis-local-vllmagent-0 ventis-local-workflow-0 2>/dev/null || true
```

## Step 1 -- build

```bash
cd examples/helloworld
ventis clean
ventis build -c config/e2e_local.yaml
```

Treat a nonzero exit as a hard failure -- report it and stop, don't try to deploy a build that
didn't finish.

## Step 2 -- deploy, wait for health

Deploy runs forever (it's the controller's poll loop), so background it directly rather than via
the `run_in_background` tool option -- that option is for a command that *exits* on its own.

```bash
cd examples/helloworld
SCRATCH="<scratchpad_dir>"
nohup ventis deploy -c config/e2e_local.yaml > "$SCRATCH/deploy.log" 2>&1 &
echo $! > "$SCRATCH/deploy.pid"
disown
```

Then wait for the workflow port to answer, as a single-notification background wait (per the
Monitor tool's guidance: an `until` loop run via Bash `run_in_background` gives one notification
when it exits, instead of polling with foreground `sleep`). A made-up request id 404s, but that
still proves the Flask app is up and routing -- so check for *any* HTTP response, not a 2xx:

```bash
until curl -so /dev/null -w '%{http_code}' http://localhost:8080/status/e2e-health-probe | grep -qE '^[0-9]+$'; do sleep 1; done
echo "WORKFLOW_UP"
```

Run this with Bash `run_in_background: true`, timeout ~60000ms. If it doesn't come up within the
timeout, dump `$SCRATCH/deploy.log`'s tail and stop -- that's a real failure, not something to
retry silently.

## Step 3 -- fire 50 requests

```bash
SCRATCH="<scratchpad_dir>"
LOGFILE="$SCRATCH/e2e_requests.txt"
> "$LOGFILE"
sent=0
failed=0
for i in $(seq 1 50); do
  resp=$(curl -sS -m 10 -w '\nHTTPCODE:%{http_code}' -X POST http://localhost:8080/main \
    -H 'Content-Type: application/json' -d "{\"name\": \"Request-$i\"}" 2>&1)
  code=$(echo "$resp" | grep -o 'HTTPCODE:[0-9]*' | cut -d: -f2)
  body=$(echo "$resp" | grep -v 'HTTPCODE:')
  if [ "$code" = "202" ]; then
    rid=$(echo "$body" | grep -o '"request_id":"[^"]*"' | cut -d'"' -f4)
    echo "$(date +%s)|$rid" >> "$LOGFILE"
    sent=$((sent+1))
  else
    failed=$((failed+1))
    echo "POST_FAILED code=${code:-none} body=$body"
  fi
done
echo "SEND_PHASE_COMPLETE sent=$sent failed=$failed"
```

## Step 4 -- poll to completion

Single-notification background wait again: poll every logged request until all are
`done`/`error` or the timeout hits.

```bash
SCRATCH="<scratchpad_dir>"
LOGFILE="$SCRATCH/e2e_requests.txt"
deadline=$(( $(date +%s) + 60 ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  pending=0
  while IFS='|' read -r ts rid; do
    [ -z "$rid" ] && continue
    # "status" is a read-only special var in zsh -- don't reuse that name here.
    req_status=$(curl -sS -m 10 "http://localhost:8080/status/$rid" | grep -o '"status":"[^"]*"' | cut -d'"' -f4)
    [ "$req_status" != "done" ] && [ "$req_status" != "error" ] && pending=$((pending+1))
  done < "$LOGFILE"
  [ "$pending" -eq 0 ] && break
  sleep 2
done
echo "POLL_PHASE_COMPLETE pending=$pending"
```

Run via Bash `run_in_background: true`, timeout ~90000ms. After it reports, do one final sweep
(same loop, no waiting) to classify every request as `done` / `error` / `still_pending` /
`unreachable`, and for `done` ones, spot-check that `result.greeting` looks like
`"Hello, Request-N! I'm the ExampleAgent."`.

## Step 5 -- teardown

```bash
SCRATCH="<scratchpad_dir>"
kill -TERM "$(cat "$SCRATCH/deploy.pid")" 2>/dev/null
```

`GlobalController.cleanup()` runs on that SIGTERM and tears down the reconciler subprocess and
every Docker container it launched. Confirm it actually finished rather than assuming:

```bash
docker ps --filter "name=ventis-" --format '{{.Names}}'
```

Should print nothing after a few seconds. If containers remain, fall back to Step 0's cleanup
command and note in the report that teardown needed a manual assist.

## Step 6 -- report

State plainly: build result, time-to-healthy, sent/failed counts from Step 3, and
done/error/still-pending/unreachable counts plus a couple of sample results from Step 4. Call out
anything in `$SCRATCH/deploy.log` at WARNING or above (this is exactly where a `ProcessSupervisor`
respawn, a Redis connection failure, or a reconciler crash would show up) rather than only
reporting the request counts -- a 50/50 done count with a reconciler exception in the log is not a
clean pass.

## Notes / known gaps in this skeleton

- One fixed query shape (`{"name": "Request-N"}`), 50 requests, no concurrency control, no chaos
  step (killing a container mid-run to watch `replace_instance`/reconciler self-heal fire). Add
  those when there's a specific case to test.
- `examples/helloworld/config/e2e_local.yaml` is a hand-maintained fixture, not generated from
  `config/global_controller.yaml` -- keep them in sync manually.
- Local-provider only. EC2 needs real AWS credentials and is out of scope here.
