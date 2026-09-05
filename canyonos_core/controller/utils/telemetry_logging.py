"""Pull future hashes from Redis and upsert runtime_information rows."""

import logging
import os
import time
from datetime import datetime, timezone

from sqlalchemy import create_engine, text
from canyonos_core.controller.utils import pricing
from canyonos_core.controller.utils.redis_client import RedisClient

logger = logging.getLogger(__name__)

_engine = None
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


def assign_project_id(project_id) -> None:
  global _project_id
  _project_id = project_id

def _get_engine(database_url):
    global _engine
    if _engine is None:
        url = os.environ.get("CANYONOS_DATABASE_URL", str(database_url))
        if url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        _engine = create_engine(url)
    return _engine


def pull_runtime_information(redis_client):
    """Scan node Redis for future execution metrics.
    Each future's identity and execution metrics both live at future:{future_id}
    """
    rows = []
    for key in redis_client.scan_keys("future:*"):
        if key.endswith(":children") or key.endswith(":consumers"):
            continue
        data = redis_client.hgetall(key)
        if data:
            data["future_id"] = data.get("id") or key.split(":")[1]
            rows.append(data)
    return rows


def _demo_cost_multiplier(env_var):
    """Off (1x) unless the env var opts in; logs a warning since it inflates recorded costs."""
    raw = os.environ.get(env_var)
    if raw is None:
        return 1
    logger.warning("%s=%s is set -- displayed costs are scaled and do not reflect real recorded costs.", env_var, raw)
    return float(raw)


def send_runtime_information(
    rows,
    redis_client: RedisClient | None = None,
    database_url="",
):
    """UPSERT rows with observed CPU and GPU resource values."""
    if not rows:
        return

    # Demo-only multipliers for scaling displayed costs; not real recorded costs.
    token_cost_multiplier = _demo_cost_multiplier("CANYONOS_DEMO_TOKEN_COST_MULTIPLIER")
    server_cost_multiplier = _demo_cost_multiplier("CANYONOS_DEMO_SERVER_COST_MULTIPLIER")

    with _get_engine(database_url).begin() as conn:
        for raw in rows:
            agent_id = raw.get("agent")
            fid = raw.get("future_id")
            if not fid:
                continue
            session_id = raw.get("request_id")
            if not session_id:
                continue
            # A future without finished_at is still executing -- skip it and let a
            # later poll, once it has actually finished, write the real measurements
            # instead.
            if not raw.get("finished_at"):
                continue
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
                redis_client.get(f"agent:{agent_id}:instance_type")
                if redis_client is not None and agent_id
                else None,
                end - start,
            )

            server_cost *= server_cost_multiplier
            token_cost *= token_cost_multiplier

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
                    "queue_time_ms": round(float(raw.get("queue_time") or 0) * 1000),
                    "input_token_count": input_token_count,
                    "output_token_count": output_token_count,
                    "token_count": token_count,
                    "errors": int(raw.get("errors") or 0),
                    "failed": bool(int(raw.get("failed", 1))),
                    "server_cost": server_cost,
                    "token_cost": token_cost,
                    "total_cost": server_cost + token_cost,
                    "cached_tokens": cached_tokens,
                    "cache_hit_ratio": cached_tokens / token_count if token_count else 0.0,
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
                    "name": raw.get("agent_name"),
                    "health": raw.get("status") or "unknown",
                    "queue_length": int(float(raw.get("queue_length") or 0)),
                    "cpu_percent": float(raw.get("cpu_percent") or 0.0),
                    "gpu_percent": float(raw.get("gpu_percent") or 0.0),
                    "disk_percent": float(raw.get("disk_percent") or 0.0),
                    "memory_percent": float(raw.get("memory_percent") or 0.0),
                    "error_count": int(raw.get("error_count") or 0),
                    "full_failures": int(raw.get("full_failures") or 0),
                    "requests_served": int(float(raw.get("requests_served") or 0)),
                    "throughput": float(raw.get("throughput") or 0.0),
                    "updated_at": datetime.fromtimestamp(
                        float(raw.get("updated_at") or now), tz=timezone.utc
                    ),
                },
            )
