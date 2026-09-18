#!/usr/bin/env bash

set -euo pipefail

if ! command -v uv >/dev/null 2>&1; then
    echo "uv is not installed. Installing uv..." >&2
    installer="$(mktemp)"
    trap 'rm -f "$installer"' EXIT
    curl -fsSL https://astral.sh/uv/install.sh -o "$installer"
    sh "$installer"
    export PATH="$HOME/.local/bin:$PATH"
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
service_root="$repo_root/services/mcp"

if [[ ! -d "$service_root" ]]; then
    echo "Service root not found: $service_root" >&2
    exit 1
fi

pushd "$service_root" >/dev/null
trap 'popd >/dev/null' EXIT

echo "Installing dependencies in $service_root ..." >&2
uv sync --no-install-project >/dev/null

echo "Executing CLI..." >&2
if [[ "${AYON_MCP_ENABLE_TELEMETRY:-}" =~ ^(1|true|yes)$ ]]; then
    uv run opentelemetry-instrument \
        --traces_exporter otlp --metrics_exporter otlp --logs_exporter otlp \
        python -m ayon_mcp.cli "$@"
else
    uv run python -m ayon_mcp.cli "$@"
fi
