#!/usr/bin/env bash
# Install (if needed) and start Ollama, then pull the model used by the
# LLM agent tests (tests/test_llm_agent.py).
#
# Usage:
#   ./scripts/setup_ollama.sh [model]
#   OLLAMA_MODEL=llama3.1 ./scripts/setup_ollama.sh

set -euo pipefail

MODEL="${1:-${OLLAMA_MODEL:-qwen2.5:7b}}"
HOST="${OLLAMA_HOST:-http://localhost:11434}"

if ! command -v ollama >/dev/null 2>&1; then
    echo "Ollama not found. Installing..." >&2
    curl -fsSL https://ollama.com/install.sh | sh
fi

if ! curl -fsS "$HOST/api/tags" >/dev/null 2>&1; then
    echo "Starting ollama serve..." >&2
    nohup ollama serve >/tmp/ollama-serve.log 2>&1 &
    disown

    echo "Waiting for Ollama at $HOST ..." >&2
    for _ in $(seq 1 30); do
        if curl -fsS "$HOST/api/tags" >/dev/null 2>&1; then
            break
        fi
        sleep 1
    done
    if ! curl -fsS "$HOST/api/tags" >/dev/null 2>&1; then
        echo "Ollama did not become reachable at $HOST. Check /tmp/ollama-serve.log" >&2
        exit 1
    fi
fi

echo "Pulling model $MODEL ..." >&2
ollama pull "$MODEL"

echo "Ollama ready at $HOST with model $MODEL." >&2
