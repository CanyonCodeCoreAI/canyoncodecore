FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl docker.io iproute2 netcat-openbsd python3 \
    && rm -rf /var/lib/apt/lists/*

CMD ["bash"]
