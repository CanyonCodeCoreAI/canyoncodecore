"""logging.Handler that streams a running future's own log lines into its `logs` field."""

import logging

try:
    import ventis.ventis_context as ventis_context
except ImportError:
    import ventis_context

try:
    from ventis.utils.log_entry import build_log_entry, append_log_entry
except ImportError:
    from log_entry import build_log_entry, append_log_entry


class LogHandler(logging.Handler):
    """Streams DEBUG/INFO log records onto the currently-executing future's `logs` field; see FUTURE_SCHEMA.md."""

    def __init__(self, redis_client, agent_id=None, agent_name=None, endpoint=None):
        super().__init__(level=logging.DEBUG)
        self._redis = redis_client
        self._agent_id = agent_id
        self._agent_name = agent_name
        self._endpoint = endpoint

    def emit(self, record):
        # Skips logging if log is above a warning as its considered an error and handled elsewhere, this prevents duplicate error logging
        if record.levelno >= logging.WARNING:
            return
        future_id = ventis_context.get_current_future_id()
        if not future_id:
            return
        try:
            entry = build_log_entry(
                record,
                agent_id=self._agent_id,
                agent_name=self._agent_name,
                endpoint=self._endpoint,
            )
            append_log_entry(self._redis, f"future:{future_id}", entry)
        except Exception:
            self.handleError(record)
