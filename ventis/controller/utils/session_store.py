"""Session-row creation for the `session` Postgres table.

Called synchronously from deploy.py's handle_workflow(), before the workflow's background
thread is dispatched, so a request's session row is always committed before any Future for
that request can exist -- this is what makes runtime_information's session_id foreign key
never race against session creation.
"""

import os
from datetime import datetime, timezone

from sqlalchemy import create_engine, text

SESSION_TABLE_NAME = "session"

_SESSION_UPSERT = text(
    f"""
    INSERT INTO {SESSION_TABLE_NAME} (
        session_id, project_id, status, created_at, updated_at
    ) VALUES (
        :session_id, :project_id, :status, :created_at, :updated_at
    )
    ON CONFLICT (session_id) DO UPDATE SET
        status = excluded.status,
        updated_at = excluded.updated_at
    """
)

_SESSION_CREATE_TABLE = text(
    f"""
    CREATE TABLE IF NOT EXISTS {SESSION_TABLE_NAME} (
        session_id VARCHAR(255) PRIMARY KEY,
        project_id UUID NOT NULL,
        status VARCHAR(32) NOT NULL DEFAULT 'working',
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    )
    """
)

_engine = None


def _get_engine(database_url):
    global _engine
    if _engine is None:
        url = os.environ.get("VENTIS_DATABASE_URL", str(database_url))
        if url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        _engine = create_engine(url)
        with _engine.begin() as conn:
            conn.execute(_SESSION_CREATE_TABLE)
    return _engine


def upsert_session(database_url, project_id, session_id, status, timestamp):
    """Create or update a session row with the given status."""
    ts = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
    with _get_engine(database_url).begin() as conn:
        conn.execute(
            _SESSION_UPSERT,
            {
                "session_id": session_id,
                "project_id": project_id,
                "status": status,
                "created_at": ts,
                "updated_at": ts,
            },
        )
