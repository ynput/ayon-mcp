FROM python:3.13-slim

ARG OTEL_EXPORTER_OTLP_ENDPOINT=http://ayon-vector:4317
ARG OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://ayon-tempo:4317


COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /mcp-service
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

COPY . /mcp-service

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

ENV PATH="/mcp-service/.venv/bin:$PATH"
# Console metric exporter otherwise only flushes every 60s by default
ENV OTEL_METRIC_EXPORT_INTERVAL=5000
# Logs/metrics go through Vector's opentelemetry source (grpc)
ENV OTEL_EXPORTER_OTLP_ENDPOINT=$OTEL_EXPORTER_OTLP_ENDPOINT
ENV OTEL_EXPORTER_OTLP_PROTOCOL=grpc
ENV OTEL_EXPORTER_OTLP_INSECURE=true
# Traces go directly to Tempo's OTLP receiver, bypassing Vector (which can't round-trip OTLP trace framing)
ENV OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=$OTEL_EXPORTER_OTLP_TRACES_ENDPOINT
ENV OTEL_SERVICE_NAME=ayon-mcp-service
EXPOSE 8088

CMD ["uv", "run", "opentelemetry-instrument", "--traces_exporter", "otlp", "--metrics_exporter", "otlp", "--logs_exporter", "otlp", "python", "-m", "ayon_mcp.app"]
