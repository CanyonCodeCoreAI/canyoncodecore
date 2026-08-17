# Decoupling telemetry polling from the global controller's health loop

This branch is the "full" version: the global-controller polling bug fixes
(SIGHUP config/project-id reload, cross-node identity broadcasting, Docker/
Redis container-reuse-on-restart, multi-node `request:completed` gathering)
plus the telemetry-polling decoupling on top of them. `bug/global-stalling`
is the "partial" version -- the same bug fixes, without telemetry decoupling
(so it still has the original single-threaded polling loop and the stalling
behavior that causes).

## The problem

`GlobalController` used to do three things on one thread, once per `poll_interval`
tick: scan every instance's Redis for finished futures, UPSERT them into Postgres,
and check controller health. A slow Postgres round trip (or a stalled write) on
any one instance delayed the health check for every other instance behind it on
that same tick — the controller's own health loop could stall waiting on
telemetry I/O that has nothing to do with health.

## The fix

`TelemetryPoller` (`ventis/controller/telemetry_poller.py`) owns telemetry
persistence entirely: its own thread, its own timer (`poll_interval`), and its
own settings, independent of whatever `GlobalController`'s health loop is doing.
The controller only ever calls `start()` / `stop()` / `update_settings()` on it
and hands it a `targets_provider` callable (`GlobalController._telemetry_targets`)
that it invokes itself, once per cycle, to discover the current instances — the
poller never imports or reaches back into `GlobalController`.

`GlobalController._health_monitor_loop` now does nothing but health checks, on
its own `poll_interval`-paced loop, on its own thread. Either loop can stall or
error without blocking the other.

## The race this introduces, and its fix

Telemetry writes are now asynchronous relative to `_cleanup_loop`, which runs on
its own, unrelated `cleanup_interval` timer. Without a handshake between them,
cleanup could delete a request's futures before the poller ever got to read and
persist their `runtime_information` row — a permanent, silent data loss with no
error anywhere.

The fix is a small ack flag, not a lock: once `send_runtime_information`
commits a future's row, it stamps `future:{future_id}.telemetry_persisted = 1`
in Redis. `GlobalController._trigger_cleanup` checks that flag
(`_telemetry_persisted_for`) — on every node's Redis, same as the multi-node
`request:completed` gather it's layered onto — before broadcasting `Cleanup`
for a request. Any future that exists but isn't acked yet leaves just that
request queued for the next cleanup cycle to retry; other ready requests in
the same batch still go through. A future with no database configured is
never going to get acked, so the check is skipped entirely in that case
(nothing would ever unblock cleanup otherwise).

## Relationship to `bug/global-stalling`

That branch has every fix in this one except the telemetry decoupling itself:
no `TelemetryPoller`, no health/telemetry thread split, no persisted-ack gate
on cleanup, and `send_runtime_information`/health-checking are still one
synchronous per-instance loop, driven by `time.sleep(poll_interval)`. That's a
deliberate, known limitation of that branch, not an oversight -- it's meant to
land the config-reload/identity/container-reuse/multi-node-cleanup fixes on
their own, reviewable independently of the (larger, riskier) threading change
in this branch.
