"""Build and accumulate the OTel Log Data Model-shaped records kept in a future's `logs` field."""

import json
import logging
import time
import traceback

# Base OTel SeverityNumber per stdlib level's range: https://opentelemetry.io/docs/specs/otel/logs/data-model/
_SEVERITY_NUMBER = {
    logging.DEBUG: 5,
    logging.INFO: 9,
    logging.WARNING: 13,
    logging.ERROR: 17,
    logging.CRITICAL: 21,
}


def _otel_entry(
    severity_number,
    severity_text,
    body,
    *,
    agent_id,
    agent_name,
    endpoint,
    logger_name=None,
    exception_type=None,
    exception_message=None,
    exception_stacktrace=None,
):
    """Assemble one OTel Log Data Model record as a JSON-serializable dict."""
    now = time.time()
    return {
        "Timestamp": now,
        "ObservedTimestamp": now,
        "SeverityNumber": severity_number,
        "SeverityText": severity_text,
        "Body": body,
        "TraceId": None,
        "SpanId": None,
        "Attributes": {
            "logger.name": logger_name,
            "agent.id": agent_id,
            "agent.name": agent_name,
            "endpoint": endpoint,
            "exception.type": exception_type,
            "exception.message": exception_message,
            "exception.stacktrace": exception_stacktrace,
        },
        "Resource": {"service.name": agent_name},
    }


def error_type_name(error, error_name=None):
    """Return the category name for the future's cheap `error` field, or the error_name override for a non-exception failure."""
    return error_name or (
        type(error).__name__ if isinstance(error, BaseException) else "RuntimeError"
    )


def build_failure_entry(error, agent_id=None, agent_name=None, endpoint=None, error_name=None):
    """Return an OTel-shaped log entry for one future failure (an exception or a plain message)."""
    is_exception = isinstance(error, BaseException)
    return _otel_entry(
        _SEVERITY_NUMBER[logging.ERROR],
        "ERROR",
        str(error),
        agent_id=agent_id,
        agent_name=agent_name,
        endpoint=endpoint,
        exception_type=error_type_name(error, error_name),
        exception_message=str(error) if is_exception else None,
        exception_stacktrace=(
            "".join(traceback.format_exception(type(error), error, error.__traceback__))
            if is_exception and error.__traceback__ is not None
            else None
        ),
    )


def build_log_entry(record, agent_id=None, agent_name=None, endpoint=None):
    """Return an OTel-shaped log entry for one stdlib `logging.LogRecord`."""
    exc_type = exc_message = exc_stacktrace = None
    if record.exc_info:
        exc_type = record.exc_info[0].__name__ if record.exc_info[0] else None
        exc_message = str(record.exc_info[1]) if record.exc_info[1] else None
        exc_stacktrace = "".join(traceback.format_exception(*record.exc_info))
    return _otel_entry(
        max((v for k, v in _SEVERITY_NUMBER.items() if record.levelno >= k), default=1),
        record.levelname,
        record.getMessage(),
        agent_id=agent_id,
        agent_name=agent_name,
        endpoint=endpoint,
        logger_name=record.name,
        exception_type=exc_type,
        exception_message=exc_message,
        exception_stacktrace=exc_stacktrace,
    )


def append_log_entry(redis_client, future_key, entry):
    """Append entry to the future's `logs` hash field, preserving prior entries."""
    existing = redis_client.hget(future_key, "logs")
    try:
        logs = json.loads(existing) if existing else []
    except (TypeError, ValueError):
        logs = []
    logs.append(entry)
    redis_client.hset(future_key, "logs", json.dumps(logs))
