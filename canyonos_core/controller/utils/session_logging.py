"""Session-row reads and writes for the `session` Postgres table.

upsert_session is called synchronously from deploy.py's handle_workflow(), before the
workflow's background thread is dispatched, so a request's session row is always committed
before any Future for that request can exist -- this is what makes runtime_information's
session_id foreign key never race against session creation.

get_session is the read side, used by deploy.py's /status endpoint once a finished request's
Redis keys have expired and the session row is the only remaining copy of its outcome.
"""

import json
import os
from datetime import datetime, timezone

from sqlalchemy import create_engine, text

SESSION_TABLE_NAME = "session"

_SESSION_UPSERT = text(
    f"""
    INSERT INTO {SESSION_TABLE_NAME} (
        session_id, project_id, status, input, output, created_at, updated_at
    ) VALUES (
        :session_id, :project_id, :status, :input_payload, :output_payload, :created_at, :updated_at
    )
    ON CONFLICT (session_id) DO UPDATE SET
        status = excluded.status,
        updated_at = excluded.updated_at,
        input = COALESCE(excluded.input, {SESSION_TABLE_NAME}.input),
        output = COALESCE(excluded.output, {SESSION_TABLE_NAME}.output)
    """
)

_SESSION_SELECT = text(
    f"""
    SELECT status, output FROM {SESSION_TABLE_NAME}
    WHERE session_id = :session_id AND project_id = :project_id
    """
)

_engines = {}  # resolved url -> Engine


def _get_engine(database_url):
    """Return a cached Engine for `database_url`, building one on first use per resolved URL."""
    global _engines
    url = os.environ.get("CANYONOS_DATABASE_URL", str(database_url))
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    engine = _engines.get(url)
    if engine is None:
        engine = create_engine(url)
        _engines[url] = engine
    return engine


def upsert_session(
    database_url,
    project_id,
    session_id,
    status,
    timestamp,
    input_payload=None,
    output_payload=None,
):
    """Create or update a session row with the given status."""
    ts = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
    with _get_engine(database_url).begin() as conn:
        conn.execute(
            _SESSION_UPSERT,
            {
                "session_id": session_id,
                "project_id": project_id,
                "status": status,
                "input_payload": (
                    json.dumps(input_payload) if input_payload is not None else None
                ),
                "output_payload": (
                    json.dumps(output_payload) if output_payload is not None else None
                ),
                "created_at": ts,
                "updated_at": ts,
            },
        )


def get_session(database_url, project_id, session_id):
    """Return {"status": ..., "output": ...} for a session, or None if there is no such row.

    This is what lets deploy.py answer GET /status once a finished request's Redis
    keys have aged out -- the session row is the durable copy of the same state.
    """
    with _get_engine(database_url).connect() as conn:
        row = (
            conn.execute(
                _SESSION_SELECT,
                {"session_id": session_id, "project_id": project_id},
            )
            .mappings()
            .first()
        )
    return dict(row) if row is not None else None
