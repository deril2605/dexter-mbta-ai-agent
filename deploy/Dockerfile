# Dexter brain container for Azure Container Apps.
# Build context is the repo root:  docker build -f deploy/Dockerfile -t dexter .
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first (better layer caching). Copy only what the build
# backend needs to resolve metadata, then the package source.
COPY pyproject.toml README.md ./
COPY dexter/ ./dexter/
RUN pip install ".[tracing]"

# Static terminal UI (served only when DEXTER_SERVE_WEB=true).
COPY web/ ./web/

# Serve the web client by default in this image; override per-environment.
ENV DEXTER_SERVE_WEB=true

EXPOSE 8000
# Bind 0.0.0.0 so ACA ingress can reach it. Single worker keeps the in-process
# route cache and graph checkpointer coherent (state is per-process).
CMD ["uvicorn", "dexter.service.app:app", "--host", "0.0.0.0", "--port", "8000"]
