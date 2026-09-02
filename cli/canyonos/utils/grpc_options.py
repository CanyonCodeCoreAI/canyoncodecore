# Keepalive options for gRPC channels/servers so idle connections get detected instead of stalling, see GRPC_STALLING_FIX.md

"""
grpc.keepalive_time_ms (int) - time period sender pings the server.
grpc.keepalive_timeout_ms (int) - time server waits for the ping before erroring
grpc.keepalive_permit_without_calls (bool) - allows for sender to send requests even when no active stream is open
grpc.http2.max_pings_without_data (int) - allows sender to send # of pings in a row without sending real data, putting it at 0 removes the limit
grpc.http2.min_ping_interval_without_data_ms (int) - the period of time the server would accept pings without blocking the sender (default: 5 minutes)
"""
GRPC_CHANNEL_OPTIONS = [
    ("grpc.keepalive_time_ms", 30000),
    ("grpc.keepalive_timeout_ms", 10000),
    ("grpc.keepalive_permit_without_calls", 1),
    ("grpc.http2.max_pings_without_data", 0),
]

GRPC_SERVER_OPTIONS = [
    ("grpc.keepalive_time_ms", 30000),
    ("grpc.keepalive_timeout_ms", 10000),
    ("grpc.keepalive_permit_without_calls", 1),
    ("grpc.http2.max_pings_without_data", 0),
    ("grpc.http2.min_ping_interval_without_data_ms", 10000),
]
