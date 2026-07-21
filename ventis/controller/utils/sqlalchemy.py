"""Pull future hashes from Redis and upsert runtime_information rows."""

import os
import time

from sqlalchemy import create_engine, text
from ventis.utils.redis_client import RedisClient

_engine = None

_UPSERT = text(
    """
    INSERT INTO runtime_information (
        future_id, session_id, workflow, agent, execution_time,
        cpu_resource, gpu_resource, created_at, updated_at
    ) VALUES (
        :future_id, :session_id, :workflow, :agent, :execution_time,
        :cpu_resource, :gpu_resource, :created_at, :updated_at
    )
    ON CONFLICT(future_id) DO UPDATE SET
        session_id=excluded.session_id,
        workflow=excluded.workflow,
        agent=excluded.agent,
        execution_time=excluded.execution_time,
        cpu_resource=excluded.cpu_resource,
        gpu_resource=excluded.gpu_resource,
        created_at=excluded.created_at,
        updated_at=excluded.updated_at
    """
)


def _get_engine(database_url):
    global _engine
    if _engine is None:
        _engine = create_engine(
            os.environ.get("VENTIS_DATABASE_URL", str(database_url))
        )
    return _engine


def pull_data(redis_client):
    """Scan node Redis for future data"""
    rows = []
    for key in redis_client.scan_keys("future:*"):
        if key.count(":") != 1:
            continue
        data = redis_client.hgetall(key)
        if data:
            data["future_id"] = data.get("id") or key.split(":", 1)[1]
            rows.append(data)
    return rows


def send_data(
    rows,
    resources_by_agent=None,
    redis_client: RedisClient | None = None,
    database_url="",
):
    """UPSERT rows and attach observed cpu/gpu (fallback to allocated resources)."""
    if not rows:
        return
    resources_by_agent = resources_by_agent or {}
    with _get_engine(database_url).begin() as conn:
        for raw in rows:
            agent = raw.get("agent")
            res = resources_by_agent.get(agent, {})
            fid = raw.get("future_id")
            if not fid:
                continue
            session_id = raw.get("request_id")
            workflow = (
                redis_client.get(f"request:{session_id}:workflow")
                if redis_client is not None
                else None
            )
            try:
                start = float(raw.get("created_at") or 0)
            except (TypeError, ValueError):
                start = 0.0

            try:
                end = float(raw.get("finished_at") or time.time())
            except (TypeError, ValueError):
                end = float(time.time())

            try:
                execution_time = float(raw.get("execution_time"))
            except (TypeError, ValueError):
                execution_time = end - start

            try:
                cpu_resource = float(raw.get("cpu_resource"))
            except (TypeError, ValueError):
                try:
                    cpu_resource = float(res.get("cpu", 0))
                except (TypeError, ValueError):
                    cpu_resource = 0.0

            try:
                gpu_resource = float(raw.get("gpu_resource"))
            except (TypeError, ValueError):
                try:
                    gpu_resource = float(res.get("gpu", 0))
                except (TypeError, ValueError):
                    gpu_resource = 0.0

            conn.execute(
                _UPSERT,
                {
                    "future_id": fid,
                    "session_id": session_id,
                    "workflow": workflow,
                    "agent": agent,
                    "execution_time": max(execution_time, 0.0),
                    "cpu_resource": max(cpu_resource, 0.0),
                    "gpu_resource": max(gpu_resource, 0.0),
                    "created_at": str(start),
                    "updated_at": str(end),
                },
            )
