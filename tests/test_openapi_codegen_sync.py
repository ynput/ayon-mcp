"""Tests for OpenAPI spec sync and conditional tool generation."""

from __future__ import annotations

import json
from pathlib import Path

from ayon_mcp import openapi_codegen


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_sync_skips_generation_when_hash_unchanged_and_generated_exists(
    monkeypatch,
    tmp_path: Path,
):
    project_root = tmp_path
    spec_payload = {
        "openapi": "3.1.0",
        "paths": {
            "/api/ping": {
                "get": {
                    "operationId": "ping",
                }
            }
        },
    }
    spec_path = project_root / "ayon_openapi.json"
    generated_init = (
        project_root
        / "ayon_mcp"
        / "tools"
        / "openapi_generated"
        / "__init__.py"
    )

    _write_json(spec_path, spec_payload)
    generated_init.parent.mkdir(parents=True, exist_ok=True)
    generated_init.write_text("# generated\n", encoding="utf-8")

    monkeypatch.setattr(
        openapi_codegen,
        "fetch_openapi_spec",
        lambda base_url, timeout=10.0, api_key="": spec_payload,
    )

    called = {"count": 0}

    def _fake_generate_openapi_tools(*, spec=None, project_root=None):
        called["count"] += 1
        return (1, 1)

    monkeypatch.setattr(
        openapi_codegen,
        "generate_openapi_tools",
        _fake_generate_openapi_tools,
    )

    changed = openapi_codegen.sync_openapi_tools_from_server(
        "http://server",
        project_root=project_root,
    )

    assert changed is False
    assert called["count"] == 0


def test_sync_regenerates_when_hash_changed(monkeypatch, tmp_path: Path):
    project_root = tmp_path
    old_spec_payload = {
        "openapi": "3.1.0",
        "paths": {"/api/ping": {"get": {"operationId": "ping"}}},
    }
    new_spec_payload = {
        "openapi": "3.1.0",
        "paths": {"/api/ping": {"get": {"operationId": "health_check"}}},
    }
    spec_path = project_root / "ayon_openapi.json"

    _write_json(spec_path, old_spec_payload)

    monkeypatch.setattr(
        openapi_codegen,
        "fetch_openapi_spec",
        lambda base_url, timeout=10.0, api_key="": new_spec_payload,
    )

    called = {"count": 0, "spec": None}

    def _fake_generate_openapi_tools(*, spec=None, project_root=None):
        called["count"] += 1
        called["spec"] = spec
        return (1, 1)

    monkeypatch.setattr(
        openapi_codegen,
        "generate_openapi_tools",
        _fake_generate_openapi_tools,
    )

    changed = openapi_codegen.sync_openapi_tools_from_server(
        "http://server",
        project_root=project_root,
    )

    assert changed is True
    assert called["count"] == 1
    assert called["spec"] == new_spec_payload
    assert json.loads(spec_path.read_text(encoding="utf-8")) == new_spec_payload
