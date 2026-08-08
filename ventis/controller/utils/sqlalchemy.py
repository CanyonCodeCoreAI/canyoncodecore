"""Pull future hashes from Redis and upsert runtime_information rows."""

import logging
import os
import time
from datetime import datetime, timezone

from sqlalchemy import create_engine, text
from ventis.controller.utils import pricing
from ventis.utils.redis_client import RedisClient

logger = logging.getLogger(__name__)

_engines = {}  # resolved url -> Engine
_project_id = None

RUNTIME_TABLE_NAME = "runtime_information"

_RUNTIME_UPSERT = text(
    f"""
    INSERT INTO {RUNTIME_TABLE_NAME} (
        future_id, parent_id, session_id, project_id, agent_id, model,
        cpu, gpu, started_at, finished_at, execution_time_ms, queue_time_ms,
        input_token_count, output_token_count, token_count, errors, failed,
        server_cost, token_cost, total_cost, cached_tokens, cache_hit_ratio
    ) VALUES (
        :future_id, :parent_id, :session_id, :project_id, :agent_id, :model,
        :cpu, :gpu, :started_at, :finished_at, :execution_time_ms, :queue_time_ms,
        :input_token_count, :output_token_count, :token_count, :errors, :failed,
        :server_cost, :token_cost, :total_cost, :cached_tokens, :cache_hit_ratio
    )
    ON CONFLICT(future_id) DO UPDATE SET
        parent_id=excluded.parent_id,
        session_id=excluded.session_id,
        project_id=excluded.project_id,
        agent_id=excluded.agent_id,
        model=excluded.model,
        cpu=excluded.cpu,
        gpu=excluded.gpu,
        started_at=excluded.started_at,
        finished_at=excluded.finished_at,
        execution_time_ms=excluded.execution_time_ms,
        queue_time_ms=excluded.queue_time_ms,
        input_token_count=excluded.input_token_count,
        output_token_count=excluded.output_token_count,
        token_count=excluded.token_count,
        errors=excluded.errors,
        failed=excluded.failed,
        server_cost=excluded.server_cost,
        token_cost=excluded.token_cost,
        total_cost=excluded.total_cost,
        cached_tokens=excluded.cached_tokens,
        cache_hit_ratio=excluded.cache_hit_ratio
    """
)


_RUNTIME_CREATE_TABLE = text(
    f"""
    CREATE TABLE IF NOT EXISTS {RUNTIME_TABLE_NAME} (
        future_id VARCHAR(255) PRIMARY KEY,
        parent_id VARCHAR(255),
        session_id VARCHAR(255) NOT NULL,
        project_id UUID NOT NULL,
        agent_id VARCHAR(255),
        model VARCHAR(255),
        cpu DOUBLE PRECISION,
        gpu DOUBLE PRECISION,
        started_at TIMESTAMPTZ NOT NULL,
        finished_at TIMESTAMPTZ,
        execution_time_ms BIGINT,
        queue_time_ms BIGINT,
        input_token_count BIGINT NOT NULL DEFAULT 0,
        output_token_count BIGINT NOT NULL DEFAULT 0,
        token_count BIGINT NOT NULL DEFAULT 0,
        errors INTEGER NOT NULL DEFAULT 0,
        failed BOOLEAN NOT NULL DEFAULT false,
        server_cost NUMERIC(12,6) NOT NULL DEFAULT 0,
        token_cost NUMERIC(12,6) NOT NULL DEFAULT 0,
        total_cost NUMERIC(12,6) NOT NULL DEFAULT 0,
        cached_tokens BIGINT NOT NULL DEFAULT 0,
        cache_hit_ratio DOUBLE PRECISION,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """
)

AGENT_TABLE_NAME = "agent_information"

_AGENT_UPSERT = text(
    f"""
    INSERT INTO {AGENT_TABLE_NAME} (
        agent_id, name, health, queue_length, cpu_percent, gpu_percent, disk_percent, memory_percent,
        error_count, full_failures, requests_served, throughput, updated_at
    ) VALUES (
        :agent_id, :name, :health, :queue_length, :cpu_percent, :gpu_percent, :disk_percent, :memory_percent,
        :error_count, :full_failures, :requests_served, :throughput, :updated_at
    )
    ON CONFLICT(agent_id) DO UPDATE SET
        name=excluded.name,
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
        name TEXT,
        health VARCHAR(32),
        queue_length INTEGER NOT NULL DEFAULT 0,
        cpu_percent DOUBLE PRECISION,
        gpu_percent DOUBLE PRECISION,
        disk_percent DOUBLE PRECISION,
        memory_percent DOUBLE PRECISION,
        error_count BIGINT NOT NULL DEFAULT 0,
        full_failures BIGINT NOT NULL DEFAULT 0,
        requests_served BIGINT NOT NULL DEFAULT 0,
        throughput DOUBLE PRECISION,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """
)
def assign_project_id(project_id) -> None:
  global _project_id
  _project_id = project_id

def _get_engine(database_url):
    """Return a cached, bootstrapped Engine for `database_url`, keyed by resolved URL."""
    global _engines
    url = os.environ.get("VENTIS_DATABASE_URL", str(database_url))
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    engine = _engines.get(url)
    if engine is None:
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(_RUNTIME_CREATE_TABLE)
            conn.execute(_AGENT_CREATE_TABLE)
        _engines[url] = engine
    return engine


def pull_runtime_information(redis_client):
    """Scan node Redis for future execution metrics.

    Each future's metrics live at future:{future_id}:metrics, written entirely by
    whichever node executed it -- never split across nodes like the main
    future:{future_id} key (used for the result hand-off) can be.
    """
    for key in redis_client.scan_keys("future:*:metrics"):
        data = redis_client.hgetall(key)
        if data:
            data["future_id"] = data.get("id") or key.split(":")[1]
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

    # Demo-only multipliers for scaling displayed costs; not real recorded costs.
    token_cost_multiplier = 10000
    server_cost_multiplier = 100000

    # Cache instance_type per agent_id -- static per agent, avoids a Redis GET per row.
    instance_type_cache = {}

    def _instance_type(agent_id):
        if agent_id not in instance_type_cache:
            instance_type_cache[agent_id] = (
                redis_client.get(f"agent:{agent_id}:instance_type")
                if redis_client is not None
                else None
            )
        return instance_type_cache[agent_id]

    with _get_engine(database_url).begin() as conn:
        for raw in rows:
            agent_id = raw.get("agent")
            fid = raw.get("future_id")
            if not fid:
                continue
            session_id = raw.get("request_id")
            if not session_id:
                continue
            # A future without finished_at is still executing. Now that metrics live
            # entirely on the executing node (future:{future_id}:metrics is only ever
            # written by the one process that runs it), "incomplete" genuinely means
            # "still running" -- skip it and let a later poll, once it has actually
            # finished, write the real measurements instead.
            if not raw.get("finished_at"):
                continue

            # Isolate each row in its own savepoint so one bad row can't abort the whole batch.
            try:
                start = float(raw.get("created_at") or 0)
                end = float(raw.get("finished_at"))
                input_token_count = int(float(raw.get("input_token_count") or 0))
                output_token_count = int(float(raw.get("output_token_count") or 0))
                token_count = int(float(raw.get("token_count") or 0))
                cached_tokens = int(float(raw.get("input_cache_tokens") or 0))
                model = raw.get("model")
                token_cost = pricing.compute_token_cost(
                    model, input_token_count, output_token_count
                )
                server_cost = pricing.compute_server_cost(
                    _instance_type(agent_id) if agent_id else None,
                    end - start,
                )

                server_cost *= server_cost_multiplier
                token_cost *= token_cost_multiplier

                with conn.begin_nested():
                    conn.execute(
                        _RUNTIME_UPSERT,
                        {
                            "future_id": fid,
                            "parent_id": raw.get("parent") or None,
                            "session_id": session_id,
                            "project_id": _project_id,
                            "agent_id": agent_id,
                            "model": model,
                            "cpu": float(raw.get("cpu_resource") or 0),
                            "gpu": float(raw.get("gpu_resource", 0)),
                            "started_at": datetime.fromtimestamp(start, tz=timezone.utc),
                            "finished_at": datetime.fromtimestamp(end, tz=timezone.utc),
                            "execution_time_ms": round((end - start) * 1000),
                            "queue_time_ms": round(
                                float(raw.get("queue_time") or 0) * 1000
                            ),
                            "input_token_count": input_token_count,
                            "output_token_count": output_token_count,
                            "token_count": token_count,
                            "errors": int(raw.get("errors") or 0),
                            "failed": bool(int(raw.get("failed", 1))),
                            "server_cost": server_cost,
                            "token_cost": token_cost,
                            "total_cost": server_cost + token_cost,
                            "cached_tokens": cached_tokens,
                            "cache_hit_ratio": (
                                cached_tokens / token_count if token_count else 0.0
                            ),
                        },
                    )
            except Exception as e:
                logger.warning(
                    "Failed to write runtime_information for future %s (session %s): %s",
                    fid,
                    session_id,
                    e,
                )
                continue

            # Clear this future's Redis key only now that its row is confirmed persisted.
            if redis_client is not None:
                try:
                    redis_client.delete(f"future:{fid}:metrics")
                except Exception as e:
                    logger.warning(
                        "Wrote runtime_information for future %s but failed to clear its "
                        "Redis key (non-fatal, will retry next poll): %s",
                        fid,
                        e,
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
            # Same per-row savepoint isolation as send_runtime_information.
            try:
                with conn.begin_nested():
                    conn.execute(
                        _AGENT_UPSERT,
                        {
                            "agent_id": agent_id,
                            "name": raw.get("agent_name"),
                            "health": raw.get("status") or "unknown",
                            "queue_length": int(float(raw.get("queue_length") or 0)),
                            "cpu_percent": float(raw.get("cpu_percent") or 0.0),
                            "gpu_percent": float(raw.get("gpu_percent") or 0.0),
                            "disk_percent": float(raw.get("disk_percent") or 0.0),
                            "memory_percent": float(raw.get("memory_percent") or 0.0),
                            "error_count": int(raw.get("error_count") or 0),
                            "full_failures": int(raw.get("full_failures") or 0),
                            "requests_served": int(
                                float(raw.get("requests_served") or 0)
                            ),
                            "throughput": float(raw.get("throughput") or 0.0),
                            "updated_at": datetime.fromtimestamp(
                                float(raw.get("updated_at") or now), tz=timezone.utc
                            ),
                        },
                    )
            except Exception as e:
                logger.warning(
                    "Failed to write agent_information for agent %s: %s",
                    agent_id,
                    e,
                )
