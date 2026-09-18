# Local-provider startup race: healthy-in-Redis vs. actually reachable

## Symptom

Right after `ventis deploy -c ... provider: local` reports every controller healthy, the
*first* cross-container gRPC call (e.g. a workflow calling an agent) can fail with:

```
UNAVAILABLE: ipv4:0.250.250.254:<port>: Socket closed
```

Retrying the same call a few seconds to ~30s later (same deployment, no redeploy) succeeds.

## Root cause

`LocalController.__init__` starts the gRPC server synchronously (`start_server()` calls
`server.start()` before returning) and only then reports `status=healthy` to Redis, so that
flag is accurate about the process itself. The gap is one layer down: for `provider: local`,
every agent's published endpoint is `host.docker.internal:<host_port>` (a hairpin through the
Docker/OrbStack host-gateway back into the container's own published port). Verified directly:
a bare TCP connect (`nc -z host.docker.internal <port>`) succeeds immediately -- the
port-forwarding layer accepts the TCP connection -- while, run at the exact same instant, a
real gRPC call to that same address still fails with `Socket closed`. So "TCP port open" does
not imply "a real RPC through that path will complete"; the port-forward/proxy layer for the
hairpin path isn't fully wired up as fast as it starts accepting connections.

## Fix

Two parts, since testing showed the first alone isn't fully reliable:

1. Each local-provider controller, right after starting its own gRPC server and before
   reporting `healthy`, dials *itself* at its own published `host.docker.internal:<port>`
   endpoint -- the exact address other containers will use -- and waits for
   `grpc.channel_ready_future` to resolve (a real connection, not just a socket accept) before
   flipping the Redis flag (`LocalController._wait_until_reachable`). Scoped to
   `agent_host == "host.docker.internal"` only -- the EC2 provider's endpoints are real network
   addresses, not a hairpin through a container host-gateway, and weren't observed to have this
   issue. This reduces how often the race is hit but, tested against a real deploy 3x in a row
   post-fix, didn't eliminate it (1/3 still failed) -- a container dialing *itself* through the
   hairpin isn't guaranteed to reflect whether the path is ready for a *different* source
   container at the same instant.
2. `_forward_request` and `_send_result_callback` (the two places a controller calls another
   controller over gRPC) now retry a `grpc.StatusCode.UNAVAILABLE` a few times with backoff
   (`LocalController._call_with_retry`) before giving up and failing the future. This is what
   actually closes the gap for the user-visible symptom, regardless of which container the race
   hits or how long it takes to settle -- (1) just makes it less likely to need this.
