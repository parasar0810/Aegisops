FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY --from=ghcr.io/astral-sh/uv:0.8.15 /uv /usr/local/bin/uv
WORKDIR /app

# Dependencies from the lockfile (crewai, google-adk, a2a-sdk, mcp, ...)
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY demo_app ./demo_app
COPY scripts ./scripts

# /app/secrets seeds the shared "am-key" volume, owned by the app user (works on Linux hosts too).
RUN useradd --create-home --uid 10001 aegis && mkdir -p /app/data /app/secrets \n    && echo not-yet-registered > /app/secrets/aegis_ingest_key \
    && echo not-yet-registered > /app/secrets/shopfront_ingest_key && chown -R aegis /app/data /app/secrets
USER aegis
ENV PATH="/app/.venv/bin:$PATH" AEGIS_STATE_DIR=/app/data/sre_state

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/system/health', timeout=4).status==200 else 1)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
