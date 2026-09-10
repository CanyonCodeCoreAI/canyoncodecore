# Reconciliation Loop for Ventis GlobalController — Design

Status: **implemented**. Desired replica counts live durably in Redis; a
GlobalController-supervised, GlobalController-restarted reconciler process converges the
running instances onto them and replaces instances that stop answering. Scaling and
replacement are Redis writes plus a wake signal — no caller ever waits on Docker or EC2.
This doc is a design/rationale reference; the actual files (`state.py`, `reconciler.py`,
`canyonos_core/controller/controller_context.py`, `canyonos_core/controller/utils/process_supervisor.py`)
are the source of truth for current behavior.

## Context
Before this change, the number of running replicas per agent was whatever
`GlobalController.launch_docker_agents()` provisioned once at startup from the YAML.
There was no way to change it at runtime, and nothing ever replaced a replica that died:
`_on_controller_unhealthy` was a no-op hook, and the signal it fired on could not detect a
dead container at all (see §5). Design: a separate reconciler process that reads a desired
replica count out of Redis, compares it against the instance records `InstanceManager`
already writes, and creates or destroys instances until the two agree.

Decisions (final status):

- **Level-triggered, not edge-triggered.** Every pass recomputes the whole picture — what
  should exist (desired counts in Redis) and what does exist (`agent_instance:*` hashes) —
  and acts on the difference. Nothing is derived from the *event* that woke the loop.
  Rationale: a lost wake signal, a Redis blip, or a crash halfway through a pass costs
  latency, never correctness. The periodic sweep (`sweep_interval`, defaults to
  `poll_interval`) exists precisely so the system converges even when *every* signal is
  lost. An edge-triggered design would need each delta delivered exactly once, and there
  is no mechanism here that guarantees that.
- **Desired state is durable in Redis; the queue carries only wake signals.**
  `reconciler:wake` payloads are an agent name or `"*"` — never "add 2" or "remove
  instance X". The desired count is already durable in
  `agent:{name}:desired_replicas` on its own, so a delta in the queue would be redundant
  at best and a second, divergent source of truth at worst (a re-delivered or dropped
  delta would permanently skew the count). The queue exists for exactly two reasons:
  to decouple the caller from provisioning latency, and to serialize all reconciles into
  one worker so two provisioners can't race on the same replica slot. Duplicate signals
  are free — `drain()` collapses a burst into one pass.
- **Redis wins over the YAML once written.** `seed_desired` writes each agent's configured
  `replicas` only if the key is absent. A scale applied at runtime therefore survives a
  GlobalController restart instead of being silently reverted to whatever the file still
  says.
- **Separate OS process, not a thread.** Same `ProcessSupervisor`
  (`canyonos_core/controller/utils/process_supervisor.py`) the OTel exporter branch introduced:
  `register`/`start_all` to spawn, `check_and_respawn` from GlobalController's existing
  poll tick, `terminate_all` from `stop()`. Rationale: provisioning is slow, blocking, and
  failure-prone (Docker, SSH, EC2 API calls), and it must not be able to stall
  GlobalController's health/metrics polling — which is the very loop that decides an
  instance is unhealthy. Redis is already the whole hand-off boundary between the two, so
  a process boundary costs almost nothing here.
- **Health is probed directly, not read from `:status`.** The pre-existing
  `controller:{host}:{port}:status` key cannot express "dead" (§5), so the reconciler uses
  a TCP connect probe plus a metrics-freshness check instead. GlobalController still reads
  `:status` for its own logging and startup wait; that path was not changed.
- **A shared `ControllerContext` base, not an import of `GlobalController`.** The
  reconciler needs config, Redis clients, `_run_cmd` and `InstanceManager` — but must not
  import `global_controller`, whose module-level `sys.path.insert(0,
  os.path.abspath("grpc_stubs"))` is cwd-relative (§8).

## Implementation summary

### 1. Redis schema

| Key | Type | Written by | Read by |
| --- | --- | --- | --- |
| `agent:{name}:desired_replicas` | int (string) | GlobalController — `seed_desired` in `__init__`, `state.scale` from `_scale` | Reconciler (`get_desired`, `desired_agent_specs`) |
| `reconciler:wake` | list | GlobalController — `_request_reconcile`, from `_scale`, `replace_instance`, and the non-healthy branch of `_poll_controllers` | Reconciler (`drain`: one `BRPOP` then non-blocking `RPOP`s) |
| `reconciler:reap` | set | GlobalController — `replace_instance` (`SADD`) | Reconciler (`take_reap_requests`: `SMEMBERS` then `SREM`) |
| `agent:{name}:instances` | set | `InstanceManager` in **either** process (`_add_instance_to_agent` / `remove_instance`) | both |
| `agent_instance:{provider}:{name}:{index}` | hash | `InstanceManager` in **either** process (`_next_host_port` port reservation, then `_write_instance`; deleted by `remove_instance`) | both |
| `routing_table:services` / `:endpoints` / `:stateful` | set / hash / hash | `publish_routing_snapshot`, in **either** process | routing clients |
| `controller:{host}:{port}:metrics` | hash | the agent container's `LocalController._metrics_loop` | GlobalController poll; reconciler freshness check |
| `controller:{host}:{port}:status` | string | the agent container's `LocalController` | GlobalController only — deliberately not the reconciler's health signal (§5) |

The first three keys are the new ones; everything below them is pre-existing observed
state that the reconciler reads rather than owns. Desired state and the instance records
live on the **primary** client (`context.redis`, which is the localhost node's Redis once
`attach_local_node_redis` has run, §9); `controller:*` metrics live on the Redis of the
node the instance actually runs on, reached via `node_redis_for_instance`.

Nothing sets a TTL on any of these. The desired count is not garbage-collected when an
agent leaves the config (§Known gaps).

### 2. `state.py` — desired state and the wake queue
Plain functions over a `RedisClient`, no class, so any process — GlobalController, the
reconciler, or a future API — can call them without constructing a controller.

- `seed_desired(redis, agent_specs)` — write-if-absent, per the "Redis wins" decision above.
- `set_desired` / `get_desired` — clamp at 0; `get_desired` falls back to the caller's
  default and logs when the stored value is not an integer, rather than raising inside a
  reconcile pass.
- `scale(redis, name, delta)` — `INCRBY`, then a clamping `SET` to 0 if the result went
  negative. `INCRBY` (not read-modify-write) so two concurrent scalers can't lose an
  increment.
- `request_reconcile(redis, name=WAKE_ALL)` — `LPUSH` of a name or `"*"`.
- `drain(redis, timeout=1)` — one `BRPOP` (bounded, so shutdown latency is ~1s, not the
  sweep interval), then up to `_DRAIN_LIMIT` non-blocking `RPOP`s, returning a **set**.
  The set is what collapses a burst of identical signals into one pass; the limit is there
  so one drain can't spin forever on a queue being written to concurrently.
- `request_replace` / `take_reap_requests` — targeted destruction. `take_reap_requests`
  claims by removing up front, so a crash mid-pass loses the request rather than
  re-destroying that instance on every future pass forever.
- `desired_agent_specs(redis, agent_specs)` — the **full** spec list with each spec's
  `replicas` replaced by its desired count. This is the load-bearing one; see §4.

The four new `RedisClient` methods (`incrby`, `lpush`, `rpop`, `brpop`) are thin
pass-throughs; `rpop`/`brpop` decode like the existing `get` does.

### 3. `reconciler.py` — the reconcile pass
Each pass is reap-then-fill:

```
_reap(agent):   desired = get_desired(agent)
                claimed = take_reap_requests(agent)
                for instance in list_instances(agent):
                    if id in claimed:                       -> remove ("replacement requested")
                    elif int(replica_index) >= desired:     -> remove ("surplus to desired count")
                    elif not _is_healthy(instance):         -> remove ("unhealthy")
_fill():        ensure_instances(desired_agent_specs(all configured agents))
```

`reconcile(agent)` reaps one agent then fills; `reconcile_all()` reaps every agent and
fills **once** at the end, since `_fill` is global anyway (§4) — one pass, not one per
agent. `reconcile_all` wraps each agent's reap and the fill in its own try/except, so one
broken agent spec can't stop the others from converging.

**Why the reap rule is `replica_index >= desired`, not "remove the last N".** Slots are
identified by index (`{provider}:{name}:{index}`), and `ensure_instances` iterates
`range(replicas)`. Making the *predicate* on the index rather than on a count gives two
properties for free:

- Scale-down is deterministic and idempotent. Scaling 5→2 always removes indices 2, 3, 4,
  whichever pass notices, however many times it runs. A "remove N" rule would depend on
  iteration order and on nothing else having changed since the count was computed.
- Orphans get cleaned up. If a hole in the slot sequence ever leaves an instance at index
  7 while desired is 3 — a partial earlier scale-down, an aborted provision, a stale
  record from a previous config — no count-based rule would ever notice it, because
  `ensure_instances` only ever looks at `range(0, 3)`. The index predicate reaps it on the
  next pass.

**Why refilling a reaped slot needs no extra code.** `ensure_instances` is create-only and
per-slot idempotent: for each `(provider, name, index)` it reads the instance hash and
skips the slot entirely if the record exists *and* carries a `runtime_id`. `remove_instance`
deletes that hash. So reaping an unhealthy replica at index 1 leaves a hole, and the very
next `_fill` — same pass — sees index 1 missing and provisions a fresh container into it.
Replacement is the composition of two existing behaviors, not a new code path.

### 4. The routing-snapshot hazard (read before touching `_fill`)
`_fill` must hand `ensure_instances` the **full** spec list for every configured agent,
with per-agent desired counts substituted in — never a single agent's spec. That is what
`state.desired_agent_specs` is for, and why it iterates the whole `context.controllers`
list.

The reason is in `InstanceManager.publish_routing_snapshot(agent_specs)`, which
`ensure_instances` calls at the end of every invocation with exactly the specs it was
given:

```python
services = {agent_spec["name"] for agent_spec in agent_specs}
...
for stale in existing_services - services:
    redis_client.srem(self.SERVICES_SET_KEY, stale)
    hdel(self.ROUTING_STATEFUL_KEY, stale)
    hdel(self.ROUTING_ENDPOINTS_KEY, stale)
```

It treats the spec list as the complete world and removes every service absent from it.
Calling `ensure_instances([one_spec])` to scale one agent would therefore delete every
*other* agent from `routing_table:services`, `routing_table:endpoints` and
`routing_table:stateful` — the entire routing table collapsed to one service, with the
other agents still running and now unreachable, until the next full pass rebuilt it.
`remove_instance` has the same exposure and papers over it with
`getattr(self, "_agent_specs", ...)`, reusing whatever list the last `ensure_instances`
was given. Both call sites in this change (`_fill` and
`GlobalController.launch_docker_agents`) pass the full list.

### 5. Health and liveness — the pre-existing gap and its replacement
**The gap.** `controller:{host}:{port}:status` is written only by the agent container's own
`LocalController`: `"healthy"` at init (`local_controller.py:75`), then re-set to
`"healthy"` on every metrics interval by `_metrics_loop` (`local_controller.py:140`). The
only other value written anywhere in the codebase is `"stopped"`, in
`LocalController.stop()` (`local_controller.py:765`) — a *graceful* shutdown path that by
definition does not run when a container is killed, OOMs, hangs, or its host disappears.
There is no TTL on the key.

The consequence: a dead replica reads `healthy` forever. `GlobalController._poll_controllers`
reads that key and only reaches its unhealthy branch on `status != "healthy"`, i.e. only
when the key is *absent* (`node_redis.get(...) or "unknown"`) — which happens when the
node's whole Redis is gone or was never written, not when one replica dies. Self-heal
built on this signal was impossible: the only failure it can observe is the one where the
observer itself has lost the node.

**The replacement.** `_is_healthy` requires two independent positives:

1. `_accepts_connections` — a TCP connect to the instance's own `host` + `host_port` from
   the reconciler's `agent_instance:*` record, with a 2s timeout. This is exactly the
   technique `EC2/_runtime._check_controller_health` (`_runtime.py:296`) already uses to
   decide a launched container is up — the change is applying it on *every* pass rather
   than once at bootstrap. It is the only signal here the instance cannot fake by being a
   process that no longer works: a dead container has no listener.
2. `_reports_are_fresh` — `controller:{agent_host}:{host_port}:metrics` exists and its
   `updated_at` is within `stale_after` (`3 * poll_interval`; a replica writes on the same
   cadence GlobalController polls with, so this allows a couple of missed reports). The
   host is mapped through `_agent_host_key`, because a container writes its key under the
   host string *it* sees (`host.docker.internal` for a local node), not `localhost`.

The second check exists because the first is not sufficient. A gRPC server thread can keep
accepting connections while the agent behind it has stopped making progress — a wedged
executor, a deadlocked agent method, a hung dependency. The listener says "alive"; the
heartbeat says otherwise. Conversely the first check exists because the second is not
sufficient either: metrics are written to the node's Redis, and a stale hash cannot
distinguish "the replica is wedged" from "the replica was never there".

**A Redis read failure is deliberately not unhealthy.** If `hgetall` raises,
`_reports_are_fresh` logs and returns `True`. A Redis outage or a network blip is evidence
about the *reconciler's* connectivity, not about the instance, and treating it as
unhealthy would turn one Redis hiccup into a fleet-wide destroy-and-recreate — the
classic self-inflicted outage. A *missing or empty* metrics hash, by contrast, is a real
negative and returns `False` (startup grace, §6, is what keeps that from being fatal to a
container that is still coming up).

### 6. Startup grace vs debounce — why there is one and not the other
These look like the same knob and are not.

**No debounce, on purpose.** An instance that has been observed healthy and then fails a
probe is replaced on the *first* bad signal. Requiring N consecutive failures would add N
× sweep_interval to every real outage in exchange for tolerating a class of flap that the
two-signal design (§5) and the Redis-failure carve-out already cover. Replacement is
cheap and idempotent here; downtime is not.

**Startup grace, also on purpose.** An instance that has *never* reported yet is not
unhealthy, it is starting: the container is pulling, importing, binding its port. Both
health signals legitimately read negative for that whole window. So:

```python
if self._accepts_connections(instance) and self._reports_are_fresh(instance):
    self._seen_healthy.add(instance_id)
    return True
if instance_id not in self._seen_healthy and self._within_startup_grace(instance):
    return True
return False
```

The grace is `max(30, 3 * poll_interval)` seconds measured from `created_at`, a field
newly stamped by `InstanceManager._write_instance`. Two bounds on it matter:

- It is keyed to `created_at`, not to process start, so a replica provisioned mid-run gets
  the same protection as one provisioned at boot.
- It applies **only until the instance is first seen healthy** (`_seen_healthy`). After
  that, `created_at` is irrelevant and the instance is judged on the live probes alone —
  otherwise a young instance that came up and then died would sit protected for the rest
  of its grace window.

Without the grace, the failure mode is not a missed replacement, it is a loop: pass 1
provisions a container, pass 2 (seconds later, still starting) probes it, calls it
unhealthy, destroys it, and provisions a new one — forever, never letting any container
live long enough to finish booting.

### 7. Process lifecycle inside GlobalController
- **Registered in `__init__`, started in `run()`.** `GlobalController.__main__` calls
  `launch_docker_agents()` and then `_wait_for_healthy()` *before* `run()`. Those
  provision the configured instances; a reconciler already running at that moment would be
  doing its own `ensure_instances` concurrently, and the check-then-provision inside
  `ensure_instances` is not atomic across processes — two provisioners can both read an
  empty slot and both fill it, double-provisioning the same replica index (and, for local
  agents, racing on `_next_host_port`). Registering early and starting late keeps the
  `register()` call next to the config it derives from while guaranteeing the reconciler
  starts only after the initial provision has been waited out.
- **`check_and_respawn()` in the poll tick, guarded by `if self.running:`.** A SIGTERM can
  run `stop()` (which calls `terminate_all()`) while `_poll_controllers` is mid-tick; an
  unguarded respawn would resurrect the process that shutdown just intentionally killed,
  leaving an orphan provisioning containers after the controller is gone.
- **`terminate_all()` FIRST in `stop()`, before `_stop_docker_agents()`.** This is the
  **opposite** order from the otel-exporter branch, where `terminate_all()` is last. It has
  to be: the OTel exporter is a pure reader, so draining it last only helps it flush,
  whereas the reconciler is a *writer* whose entire job is to notice missing instances and
  replace them. Leave it running while `_stop_docker_agents()` tears the fleet down and it
  will faithfully provision replacements for everything being stopped — a shutdown that
  never converges. The reconciler must be dead before the first container is removed.
- The child is spawned as `sys.executable -m canyonos_core.reconciler --config
  <abspath>`. The config path is absolutized at `register()` time because the child
  inherits the parent's cwd and nothing guarantees a relative path stays valid.
  `reconciler.main()` installs SIGTERM/SIGINT handlers that clear a module-level `_running`
  flag, so `terminate_all()`'s `.terminate()` gets a clean exit within about one
  `drain()` timeout (~1s), well inside the supervisor's 10s kill fallback.

### 8. Why a second process can reach the provisioning code at all
`InstanceManager` and the provider runtimes were written against `GlobalController`, but
they only ever use a narrow slice of it: `config` / `config_path`, `poll_interval`,
`controllers` / `agent_specs`, `redis`, `node_redis`, `containers`, `redis_containers`,
`_get_node_redis_for`, `_agent_host_key`, `_get_replica_placements`, and `_run_cmd`. That
slice was extracted into `ControllerContext`, which `GlobalController` now subclasses and
the reconciler constructs directly.

What is deliberately **not** in it: stale-container cleanup, launching Redis containers,
`write_agent_specs`, resource specs, policies, identity, the initial routing snapshot, the
polling loop, the gRPC stubs. Cluster bootstrap stays GlobalController's alone — a second
process has to be able to provision instances without repeating any of it (relaunching
Redis containers under a live cluster would be actively destructive).

The import boundary is the sharper reason the split is a hard requirement rather than
tidiness. `global_controller.py` does this at module level:

```python
sys.path.insert(0, os.path.abspath("grpc_stubs"))
import local_controler_pb2
```

`os.path.abspath` is resolved against the **current working directory**. Importing
`global_controller` therefore only works from a project root that has a generated
`grpc_stubs/` directory, and fails outright anywhere else. A child process must never
import it. Verified: importing `canyonos_core.reconciler.reconciler` from an unrelated
cwd succeeds and pulls in neither `global_controller` nor any `local_controler_pb2`
module.

This split is also the seam for the larger move it was factored for: `InstanceManager` now
depends on `ControllerContext`, not on `GlobalController`, so provisioning could be lifted
out of GlobalController's process entirely without touching `InstanceManager` or the
runtimes.

### 9. Cross-process Redis consistency
Two hazards, two narrow fixes in `ControllerContext`:

- **`node_redis_for_instance(instance)`** — connects to a node's Redis on demand, from the
  instance's own `host` / `redis_port` record, caching into `node_redis`. `node_redis` is
  populated as a side effect of *provisioning* (the EC2 runtime registers a node's client
  during bootstrap), so an instance provisioned by the *other* process has its node's
  Redis registered only in that process's dict. The pre-existing
  `_get_node_redis_for(host)` silently falls back to `self.redis` on a miss, which would
  have the reconciler read a local key and conclude a remote instance's metrics are stale
  — every remote replica reaped on the first pass. `_poll_controllers` was switched to this
  method too, for the same reason in reverse.
- **`attach_local_node_redis()`** — called once in `Reconciler.__init__`. GlobalController
  repoints its primary `self.redis` at `node_redis["localhost"]` after launching that
  node's Redis container, which is published on the agent-level `redis_port` (default
  6379). A process that only *attaches* to a running cluster does the same repoint without
  launching anything, by finding the localhost placement's `redis_port` in the config.
  Without it, a config that sets a non-default agent-level `redis_port` would leave
  GlobalController writing desired state and instance records to one Redis while the
  reconciler read from another — the reconciler would see zero instances, provision a
  duplicate fleet, and never converge. It is a no-op when no agent is placed on localhost.

## Verification approach used during development
- **Unit suite, baseline-compared.** The full pytest suite (excluding
  `test_integration.py` / `test_performance.py`, which need a live deployment) was run on
  this branch and on its branch point (`7594ea2`) in a separate checkout: **10 failed / 119
  passed** at the baseline and **10 failed / 152 passed** on the branch — the same ten
  pre-existing failures (`test_error_propagation`, `test_local_controller_metrics`,
  `test_telemetry_logging` cost math), none of them touched by this change, and 33 new
  passing tests.
- **New tests.** `tests/test_reconciler.py` (28 tests) covers desired-state read/write
  including the clamp and non-integer fallbacks, seed-if-absent semantics, `scale`'s
  negative clamp, `desired_agent_specs` returning the whole list, wake-signal coalescing in
  `drain`, per-agent reap claiming and its one-shot behavior, surplus reaping, `_fill`
  receiving specs for every configured agent, removal on a failed probe, the startup-grace
  keep/expire/`_seen_healthy` cases, an unknown-agent no-op, `reconcile_all` surviving one
  raising agent, and the metrics-freshness cases including the Redis-blip carve-out.
  `tests/test_process_supervisor.py` (5 tests) covers spawn, respawning an exited process
  with a new PID, leaving a live process alone, env merging on top of `os.environ`, and
  `terminate_all` clearing the registry.
- **Import isolation.** Confirmed directly that the reconciler module imports cleanly from
  an unrelated cwd without loading `global_controller` or the gRPC stubs (§8).
- **No live verification.** Nothing here has been exercised against a real EC2 deployment,
  a real multi-node cluster, or a real killed container; the health probes, the
  cross-process Redis paths, and the shutdown ordering are reasoned and unit-tested, not
  observed in production.

## Known gaps (not yet built)
- **An agent whose `replicas` is a list is still unsupported, just not silently.**
  `ControllerContext._get_replica_placements` supports the list form (explicit per-replica
  host/port) but `ensure_instances` never has: it does `range(int(spec["replicas"]))`, which
  raises `TypeError` on a list. `desired_agent_specs` therefore passes such a spec through
  **untouched** rather than dropping it, so `launch_docker_agents` still fails loudly at
  startup exactly as it did before this change — dropping it instead would have left the
  agent unlaunched *and* deleted from the routing table by `publish_routing_snapshot` (§4).
  `_reap` skips the agent with a warning rather than raising on the comparison. The list
  form needs either real support in `ensure_instances` or an explicit rejection at config
  load; today it is a loud failure at launch.
- **`scale`'s clamp-after-`INCRBY` is not atomic.** `INCRBY` then a conditional `SET 0` is
  two round trips; a concurrent increment landing between them is lost. Harmless while
  GlobalController is the only writer, but this is not a general-purpose distributed
  counter, and adding a second scaler (an API, a policy engine) needs a Lua script or a
  proper lock.
- **`ProcessSupervisor` has no respawn backoff and no crash-loop cap.** A reconciler that
  dies immediately on startup — bad config, unreachable Redis — is respawned every poll
  tick forever, with no escalation and no giving up.
- **`reload_config()` does not refresh the spec hashes or desired state.**
  `write_agent_specs` and `_write_resource_specs` run only in `__init__`, so after a SIGHUP
  `agent:{name}:` and `agent:{name}:resources` keep the *old* `replicas`, and a newly added
  agent gets no `desired_replicas` seeded until a full restart. The reconciler process does
  not reload its config at all — it reads the YAML once at startup and has no SIGHUP
  handler, so it will not see a new agent either.
- **Deleting an agent from the config is not handled.** Nothing removes
  `agent:{name}:desired_replicas`, and the reconciler only iterates agents that are still
  in the config, so the removed agent's instances are never reaped — they keep running,
  invisible to reconciliation. (Their routing entries *are* removed, by the snapshot logic
  in §4, which makes them unreachable-but-alive.)
- **A mid-provision slot carries no `created_at`, but is not currently reachable by the
  reaper.** `_next_host_port` writes a partial reservation hash (no `runtime_id`, no
  `created_at`) before the container exists. `_reap` cannot see it: it lists via
  `list_instances(agent_name)`, which reads the `agent:{name}:instances` set, and
  `_add_instance_to_agent` only adds a slot *after* provisioning succeeds. So the record is
  invisible to reconciliation until it is complete. This is load-bearing but incidental — a
  future reaper that scanned `agent_instance:*` directly instead of the set would have no
  startup grace for a slot being provisioned, so `created_at` belongs on the reservation
  write too.
- **Routing-snapshot fan-out depends on which node clients happen to be cached.**
  `publish_routing_snapshot` writes to `list(controller.node_redis.values())`, and in the
  reconciler process that dict is populated lazily. `_reap` therefore calls
  `node_redis_for_instance` for every listed instance before removing any of them, so each
  node holding an instance has a client before a routing republish — without that, a pass
  reaping purely by index (which short-circuits before the health probe) would never
  connect to that host and would leave its copy of the routing table stale. A node holding
  *no* instances still receives no update, which is harmless today.
- **Instance identity is still a slot index, not a UUID.** `{provider}:{name}:{index}` is
  what makes `ensure_instances` per-slot idempotent and the reap rule deterministic, but it
  means a replaced instance reuses its predecessor's identity. Records, metrics keys and
  routing entries cannot distinguish "the same replica" from "its third replacement", which
  makes per-generation debugging and any future rolling-update semantics awkward. A stable
  UUID alongside the slot would fix it; `agent_id` already exists per instance but is not
  used as the reconciliation key.
- **`take_reap_requests` matches by substring.** It claims ids containing
  `f":{agent_name}:"`, which is correct for `{provider}:{name}:{index}` ids but would
  mis-claim if an agent name ever contained a colon.
- **The reconciler never reaps an instance whose whole node is gone.** It reads instance
  records from the primary Redis, so a vanished EC2 host's records survive and its slots
  are reaped as "unhealthy" one at a time — but nothing cleans up that node's `node_redis`
  entry or its Redis container record.
