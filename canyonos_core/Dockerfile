FROM python:3.11-slim

RUN apt-get update && apt-get install -y docker.io && rm -rf /var/lib/apt/lists/*

COPY . /src
RUN pip install /src

# global_controller.py bare-imports these; pip install only ships the .proto source.
RUN python -m grpc_tools.protoc \
    -I/src/canyonos_core/controller/proto \
    --python_out=/usr/local/lib/python3.11/site-packages \
    --grpc_python_out=/usr/local/lib/python3.11/site-packages \
    /src/canyonos_core/controller/proto/local_controler.proto

EXPOSE 8000

ENTRYPOINT ["python", "-m", "canyonos_core.server"]


# to run: docker build -f canyonos_core/Dockerfile -t saakeths/canyonos:latest .
