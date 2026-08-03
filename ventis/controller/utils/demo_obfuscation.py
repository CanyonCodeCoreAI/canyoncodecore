"""Timestamp obfuscation shared by every table that records demo run data.

The demo database is populated from real runs, but the wall-clock times those runs
happened at are not what should be on display. Every row belonging to a session is
shifted back by the same deterministic per-session amount, so sessions spread out
over the last 30 days while the relative timing *within* a request -- queue waits,
execution times, the order its futures ran in -- survives exactly.

Every writer must derive its shift the same way from the same session id, or a
session row and the runtime_information rows for its own futures land at different
points in time. That is the reason this lives in one module rather than next to
either writer: session_store.py (the `session` table) runs inside the workflow
container, sqlalchemy.py (`runtime_information`) runs in the global controller, and
they must agree.

Kept dependency-free on purpose: session_store.py is copied standalone into the
workflow image (see stub_generator.py's files_to_copy), so anything it imports has
to be importable as a flat module with no `ventis` package around it.
"""

import hashlib
from datetime import datetime, timezone

RANDOM_SHIFT_MAX_SECONDS = 2592000  # 30 days


def shift_for_session(session_id):
    """Deterministic pseudo-random shift (0 to RANDOM_SHIFT_MAX_SECONDS) shared
    by every row belonging to the same session, so a single request's rows all
    land at the same shifted point in time relative to each other."""
    digest = hashlib.sha256(str(session_id).encode()).digest()
    return int.from_bytes(digest[:8], "big") % RANDOM_SHIFT_MAX_SECONDS


def to_timestamptz(epoch_seconds, shift):
    """Convert a Unix-epoch float/string (as gathered via time.time()) to a
    timezone-aware datetime for binding to a TIMESTAMPTZ column, shifted back
    by `shift` seconds."""

    epoch_seconds = float(epoch_seconds) - shift
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
