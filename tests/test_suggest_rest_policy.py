"""Tests for the LLM-backed REST policy suggester script."""

from __future__ import annotations

import json
import warnings

import httpx
import pytest

from ayon_mcp.rest_policy import SIDE_EFFECT_OVERRIDES
from scripts.suggest_rest_policy import (
    DEFAULT_SPEC,
    Operation,
    Suggestion,
    classify,
    format_report,
    load_operations,
    suggest,
)


def _suggestion(**overrides) -> Suggestion:
    values = {
        "method": "POST",
        "path": "/api/thing",
        "summary": "Thing",
        "current_side_effect": "write",
        "current_admin": False,
        "suggested_side_effect": "write",
        "suggested_admin": False,
        "reason": "because",
    }
    values.update(overrides)
    return Suggestion(**values)


def test_load_operations_skips_non_methods() -> None:
    spec = {
        "paths": {
            "/api/b": {"get": {"summary": "B"}, "parameters": []},
            "/api/a": {"delete": {"description": "Remove A"}},
        }
    }

    operations = load_operations(spec)

    assert [(op.method, op.path) for op in operations] == [
        ("DELETE", "/api/a"),
        ("GET", "/api/b"),
    ]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, None),
        ({"suggested_side_effect": "destructive"}, "stricter"),
        ({"suggested_admin": True}, "stricter"),
        ({"suggested_side_effect": "read_only"}, "looser"),
        ({"current_admin": True}, "looser"),
        (
            {"suggested_side_effect": "read_only", "suggested_admin": True},
            "mixed",
        ),
    ],
)
def test_suggestion_direction(overrides: dict, expected: str | None) -> None:
    assert _suggestion(**overrides).direction == expected


def test_format_report_includes_paste_ready_lines() -> None:
    report = format_report([
        _suggestion(suggested_side_effect="destructive", suggested_admin=True),
        _suggestion(path="/api/ok"),
    ])

    assert "Stricter than current policy" in report
    assert "('POST', '/api/thing'): SideEffectClass.DESTRUCTIVE," in report
    assert r'(frozenset({"POST"}), r"^/api/thing$"),' in report
    assert "1/2 operations agree with policy." in report


def test_classify_sends_schema_and_parses_answer() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        answer = {"side_effect": "destructive", "admin": True, "reason": "r"}
        return httpx.Response(
            200, json={"message": {"content": json.dumps(answer)}}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    operation = Operation("POST", "/api/system/restart", "Restart", "")

    answer = classify(client, host="http://ollama", model="m", operation=operation)

    assert answer == {"side_effect": "destructive", "admin": True, "reason": "r"}
    assert seen["format"]["required"] == ["side_effect", "admin", "reason"]
    assert seen["options"]["temperature"] == 0.0


def test_classify_rejects_unknown_side_effect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        answer = {"side_effect": "maybe", "admin": False, "reason": ""}
        return httpx.Response(
            200, json={"message": {"content": json.dumps(answer)}}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(ValueError, match="maybe"):
        classify(
            client,
            host="http://ollama",
            model="m",
            operation=Operation("GET", "/api/x", "", ""),
        )


def test_suggest_uses_cache_without_calling_model() -> None:
    operation = Operation("POST", "/api/query", "Query", "")
    cache = {
        f"m|{operation.key}": {
            "side_effect": "read_only",
            "admin": False,
            "reason": "cached",
        }
    }

    # host is unreachable on purpose - a cache miss would raise
    [result] = suggest(
        [operation], host="http://127.0.0.1:9", model="m", cache=cache
    )

    assert result.reason == "cached"
    assert result.direction is None


@pytest.mark.llm
def test_model_agrees_with_reviewed_overrides(
    require_ollama: None, ollama_host: str, ollama_model: str
) -> None:
    """Report (not fail) where the model disagrees with the override table.

    A disagreement means either the override deserves a second look or the
    model is wrong - a human decides which.
    """
    if not DEFAULT_SPEC.exists():
        pytest.skip("no local OpenAPI spec")
    spec = json.loads(DEFAULT_SPEC.read_text(encoding="utf-8"))
    operations = [
        op for op in load_operations(spec)
        if (op.method, op.path) in SIDE_EFFECT_OVERRIDES
    ]

    suggestions = suggest(operations, host=ollama_host, model=ollama_model)

    disagreements = [s for s in suggestions if s.direction is not None]
    if disagreements:
        warnings.warn(format_report(disagreements), stacklevel=1)
    assert len(suggestions) == len(operations)
