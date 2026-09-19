# Craftly platform (B2).
#
# Deliberately plain: one stage would have been enough, but the build stage
# keeps uv and the lockfile out of the runtime image, which halves it and
# means a shell in the container has no package manager to misuse.

FROM python:3.11-slim AS build

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependencies before source, so editing a route does not reinstall FastAPI.
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev --no-install-project

FROM python:3.11-slim

# Runs as a non-root user. The service writes to exactly two directories and
# both are declared volumes; nothing else in the image needs to be writable.
RUN useradd --create-home --uid 10001 craftly

WORKDIR /srv
COPY --from=build /build/.venv /srv/.venv
COPY app ./app

ENV PATH="/srv/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CRAFTLY_DATABASE_URL=sqlite:////data/craftly.db \
    CRAFTLY_MEDIA_DIR=/data/media \
    CRAFTLY_QR_DIR=/data/qr

RUN mkdir -p /data && chown -R craftly:craftly /data /srv
USER craftly
VOLUME ["/data"]
EXPOSE 8200

# The health endpoint counts rows rather than returning a constant, so an
# orchestrator restarting on it is reacting to a database it cannot read
# rather than to a process that happens to still be alive.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8200/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8200"]
