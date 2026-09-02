FROM python:3.11-slim

RUN apt-get update && apt-get install -y docker.io && rm -rf /var/lib/apt/lists/*

COPY . /ventis
RUN pip install /ventis

EXPOSE 8000

ENTRYPOINT ["python", "-m", "ventis.server"]
