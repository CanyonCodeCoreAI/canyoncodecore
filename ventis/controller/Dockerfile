# syntax=docker/dockerfile:1
FROM python:3.11-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

RUN apt-get update \
    && apt-get install -y --no-install-recommends docker.io openssh-client zstd \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY controller/requirements.txt .
RUN --mount=type=cache,target=/root/.cache/uv uv pip install --system -r requirements.txt

COPY __init__.py ventis/__init__.py
COPY controller/ ventis/controller/
COPY utils/ ventis/utils/
COPY OTLP_Exporter/ ventis/OTLP_Exporter/

RUN python -m grpc_tools.protoc \
    -Iventis/controller/proto \
    --python_out=. \
    --grpc_python_out=. \
    ventis/controller/proto/local_controler.proto

EXPOSE 50051

ENTRYPOINT ["python", "-m", "ventis.controller.global_controller"]
