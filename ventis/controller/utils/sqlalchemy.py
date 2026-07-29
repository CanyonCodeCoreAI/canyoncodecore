"""Pull future hashes from Redis and upsert runtime_information rows."""

import os
import time

from sqlalchemy import create_engine, text
from ventis.controller.utils import pricing
from ventis.utils.redis_client import RedisClient

_engine = None
_project_id = None

RUNTIME_TABLE_NAME = "runtime_information"

_RUNTIME_UPSERT = text(
    f"""
    INSERT INTO {RUNTIME_TABLE_NAME} (
        future_id, session_id, workflow, agent, execution_time,
        cpu_resource, gpu_resource, created_at, updated_at, queue_time, fail, parent_id,
        input_token_count, output_token_count, token_count, errors,
        cache_hit_ratio, total_cost, model, project_id
    ) VALUES (
        :future_id, :session_id, :workflow, :agent, :execution_time,
        :cpu_resource, :gpu_resource, :created_at, :updated_at, :queue_time, :fail, :parent_id,
        :input_token_count, :output_token_count, :token_count, :errors,
        :cache_hit_ratio, :total_cost, :model, :project_id
    )
    ON CONFLICT(future_id) DO UPDATE SET
        session_id=excluded.session_id,
        workflow=excluded.workflow,
        agent=excluded.agent,
        execution_time=excluded.execution_time,
        cpu_resource=excluded.cpu_resource,
        gpu_resource=excluded.gpu_resource,
        created_at=excluded.created_at,
        updated_at=excluded.updated_at,
        queue_time=excluded.queue_time,
        fail=excluded.fail,
        parent_id=excluded.parent_id,
        input_token_count=excluded.input_token_count,
        output_token_count=excluded.output_token_count,
        token_count=excluded.token_count,
        errors=excluded.errors,
        cache_hit_ratio=excluded.cache_hit_ratio,
        total_cost=excluded.total_cost,
        model=excluded.model,
        project_id=excluded.project_id
    """
)


_RUNTIME_CREATE_TABLE = text(
    f"""
    CREATE TABLE IF NOT EXISTS {RUNTIME_TABLE_NAME} (
        future_id TEXT PRIMARY KEY,
        session_id TEXT,
        workflow TEXT,
        agent TEXT,
        execution_time REAL,
        cpu_resource REAL,
        gpu_resource REAL,
        created_at TEXT,
        updated_at TEXT,
        queue_time REAL,
        fail INTEGER,
        parent_id TEXT,
        input_token_count INTEGER,
        output_token_count INTEGER,
        token_count INTEGER,
        errors INTEGER,
        cache_hit_ratio REAL,
        total_cost REAL,
        model TEXT,
        project_id TEXT
    )
    """
)


AGENT_TABLE_NAME = "agent_information"

_AGENT_UPSERT = text(
    f"""
    INSERT INTO {AGENT_TABLE_NAME} (
        agent_id, health, queue_length, cpu_percent, gpu_percent, disk_percent, memory_percent,
        error_count, full_failures, requests_served, throughput, updated_at
    ) VALUES (
        :agent_id, :health, :queue_length, :cpu_percent, :gpu_percent, :disk_percent, :memory_percent,
        :error_count, :full_failures, :requests_served, :throughput, :updated_at
    )
    ON CONFLICT(agent_id) DO UPDATE SET
        health=excluded.health,
        queue_length=excluded.queue_length,
        cpu_percent=excluded.cpu_percent,
        gpu_percent=excluded.gpu_percent,
        disk_percent=excluded.disk_percent,
        memory_percent=excluded.memory_percent,
        error_count=excluded.error_count,
        full_failures=excluded.full_failures,
        requests_served=excluded.requests_served,
        throughput=excluded.throughput,
        updated_at=excluded.updated_at
    """
)


_AGENT_CREATE_TABLE = text(
    f"""
    CREATE TABLE IF NOT EXISTS {AGENT_TABLE_NAME} (
        agent_id VARCHAR(255) PRIMARY KEY,
        health VARCHAR(32),
        queue_length INTEGER NOT NULL DEFAULT 0,
        cpu_percent DOUBLE,
        gpu_percent DOUBLE,
        disk_percent DOUBLE,
        memory_percent DOUBLE,
        error_count BIGINT NOT NULL DEFAULT 0,
        full_failures BIGINT NOT NULL DEFAULT 0,
        requests_served BIGINT NOT NULL DEFAULT 0,
        throughput DOUBLE,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """
)
def assign_project_id(project_id) -> None:
  global _project_id
  _project_id = project_id

def _get_engine(database_url):
    global _engine
    if _engine is None:
        _engine = create_engine(
            os.environ.get("VENTIS_DATABASE_URL", str(database_url))
        )
        with _engine.begin() as conn:
            conn.execute(_RUNTIME_CREATE_TABLE)
            conn.execute(_AGENT_CREATE_TABLE)
    return _engine


def pull_runtime_information(redis_client):
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


def send_runtime_information(
    rows,
    redis_client: RedisClient | None = None,
    database_url="",
):
    """UPSERT rows with observed CPU and GPU resource values."""
    if not rows:
        return
    with _get_engine(database_url).begin() as conn:
        for raw in rows:
            agent = raw.get("agent")
            fid = raw.get("future_id")
            if not fid:
                continue
            session_id = raw.get("request_id")
            if not session_id:
                continue
            workflow = (
                redis_client.get(f"request:{session_id}:workflow")
                if redis_client is not None
                else None
            )
            start = float(raw.get("created_at") or 0)
            end = float(raw.get("finished_at") or time.time())
            cpu_resource = float(raw.get("cpu_resource") or 0)
            gpu_resource = float(raw.get("gpu_resource", 0))
            queue_time = float(raw.get("queue_time") or 0)
            input_token_count = int(float(raw.get("input_token_count") or 0))
            output_token_count = int(float(raw.get("output_token_count") or 0))
            token_count = int(float(raw.get("token_count") or 0))
            input_cache_tokens = int(float(raw.get("input_cache_tokens") or 0))
            cache_hit_ratio = input_cache_tokens / token_count if token_count else 0.0
            model = raw.get("model")
            token_cost = pricing.compute_token_cost(
                model, input_token_count, output_token_count
            )
            instance_type = (
                redis_client.get(f"agent:{agent}:instance_type")
                if redis_client is not None and agent
                else None
            )
            server_cost = pricing.compute_server_cost(instance_type, end - start)
            total_cost = server_cost + token_cost

            conn.execute(
                _RUNTIME_UPSERT,
                {
                    "future_id": fid,
                    "session_id": session_id,
                    "workflow": workflow,
                    "agent": agent,
                    "execution_time": end - start,
                    "cpu_resource": cpu_resource,
                    "gpu_resource": gpu_resource,
                    "created_at": start,
                    "updated_at": end,
                    "queue_time": queue_time,
                    "fail": raw.get("failed", 1),
                    "parent_id": raw.get("parent") or None,
                    "input_token_count": input_token_count,
                    "output_token_count": output_token_count,
                    "token_count": token_count,
                    "errors": int(raw.get("errors") or 0),
                    "cache_hit_ratio": cache_hit_ratio,
                    "total_cost": total_cost,
                    "model": model,
                    "project_id": _project_id
                },
            )


def send_agent_information(rows, database_url=""):
    """UPSERT per-instance telemetry rows (health/cpu/gpu/uptime heartbeat)."""
    if not rows:
        return
    now = time.time()
    with _get_engine(database_url).begin() as conn:
        for raw in rows:
            agent_id = raw.get("agent_id")
            if not agent_id:
                continue
            conn.execute(
                _AGENT_UPSERT,
                {
                    "agent_id": agent_id,
                    "health": raw.get("status") or "unknown",
                    "queue_length": int(float(raw.get("queue_length") or 0)),
                    "cpu_percent": float(raw.get("cpu_percent") or 0.0),
                    "gpu_percent": float(raw.get("gpu_percent") or 0.0),
                    "disk_percent": float(raw.get("disk_percent") or 0.0),
                    "memory_percent": float(raw.get("memory_percent") or 0.0),
                    "error_count": int(raw.get("errors") or 0),
                    "full_failures": int(raw.get("failures") or 0),
                    "requests_served": int(float(raw.get("requests_served") or 0)),
                    "throughput": float(raw.get("throughput") or 0.0),
                    "updated_at": raw.get("updated_at") or now,
                },
            )
