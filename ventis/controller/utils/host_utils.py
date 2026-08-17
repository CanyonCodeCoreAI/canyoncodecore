"""Shared host-name helpers for translating a config host into its Docker-visible address."""


def is_local_host(host):
    return host in {"localhost", "127.0.0.1"}


def container_routing_host(host):
    """Return the address a Dockerized local controller uses in Redis keys."""
    return "host.docker.internal" if is_local_host(host) else host
