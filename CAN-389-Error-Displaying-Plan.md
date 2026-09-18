# CAN-389: Surface the real deploy failure instead of burying it

## Problem

`cli/canyonos/deploy.py`'s quiet-mode tail (`_tail_quiet` / `_reveal_failure`)
correctly detects a fatal line (`CRITICAL:`, `ERROR:`, a traceback, etc.) via
`PhaseTracker.feed()` and `_ERROR_MARKERS`, prints "Deploy failed.", and
replays the buffered log up to that point.

After that, `_reveal_failure` keeps raw-echoing the container's log stream
for another `_REVEAL_GRACE_SECONDS` (30s) with no filtering at all. If an
unrelated background process in the container (e.g. the OTel exporter
retrying a now-dead Redis connection every ~2s, each attempt a full
traceback) is still alive and noisy during that window, it can produce
hundreds of lines that scroll the actual root cause off screen. The user
sees "Deploy failed." followed by a wall of unrelated noise, with the real
cause visible only if they scroll back up.

Confirmed case: an EC2 deploy failing with
`CRITICAL: Failed to launch configured runtimes: Unable to locate
credentials` (missing `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`) got
buried under ~300 lines of `ERROR:__main__:Failed to read otel:destinations
from Redis ... Connection refused` repeats during the 30s grace drain.

## Fix

Minimal, no new filtering/dedup logic during the drain (keeps `-v`/`canyonos
logs` parity: the full raw stream is still shown, nothing is hidden):

1. `_tail_quiet` captures the line that actually tripped `is_error` (the
   line `PhaseTracker.feed()` flagged) before breaking out of its loop.
2. That line is passed into `_reveal_failure`.
3. After the existing grace-period drain finishes, `_reveal_failure`
   reprints that one line again (via `ui.fail`, same styling as "Deploy
   failed.") as the last thing on screen, right before the existing
   "run `canyonos deploy -v`..." hint.

This mirrors the common pattern (e.g. pytest streaming full output, then
reprinting just the failure summary at the bottom): nothing is hidden or
filtered, but the actual cause is guaranteed to be the last thing visible
regardless of what an unrelated process logs afterward.

## Scope

- `cli/canyonos/deploy.py`: `_tail_quiet`, `_reveal_failure`.
- `tests/test_deploy_progress.py`: cover that the triggering line is
  reprinted after the grace-drain, and that a run with no error doesn't
  print a spurious "Cause:" line.

Out of scope: filtering/deduping the noisy repeats live during the drain,
and fixing the OTel exporter's own noisy retry behavior (separate issue,
tracked in company-memory, not part of this ticket).
