"""Suggest REST tool classifications using a local Ollama model.

Asks the model to classify OpenAPI operations from ``ayon_openapi.json`` as
read_only / write / destructive and admin or not, then prints where the
suggestion disagrees with ``ayon_mcp.rest_policy``. It never edits
``rest_policy.py`` - the output is a starting point for a human review, and
the committed policy stays the only thing used at runtime.

Disagreements are split by direction:

- stricter: the model rates the operation more dangerous than the policy
  does. Usually cheap to accept after a quick look.
- looser: the model rates it less dangerous. Needs explicit sign-off -
  accepting one weakens a guard.

Run from ``services/mcp`` (so ``ayon_mcp`` is importable)::

    uv run python -m scripts.suggest_rest_policy --match "^/api/projects"

By default only operations without an explicit override are checked; pass
``--all`` to re-check the overrides too. Answers are cached per model and
operation text, so re-runs only query new or changed operations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
from ayon_mcp.openapi_codegen import HTTP_METHODS
from ayon_mcp.rest_policy import (
    SIDE_EFFECT_OVERRIDES,
    SIDE_EFFECT_RANK,
    is_admin,
    side_effect_for,
)
from chuk_tool_processor.guards import SideEffectClass

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SPEC = PROJECT_ROOT / "ayon_openapi.json"
DEFAULT_CACHE = PROJECT_ROOT / ".rest_policy_suggestions.json"
DEFAULT_OLLAMA_HOST = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "qwen2.5:7b"

SYSTEM_PROMPT = """\
You classify REST endpoints of AYON, a VFX/animation production tracking
server, for an AI assistant's safety policy. Judge the real effect of the
call, not just the HTTP method: a POST that only runs a query is read_only,
a POST that restarts the server is destructive.

side_effect:
- read_only: no server state changes.
- write: creates or changes data in a way that can be corrected afterwards.
- destructive: deletes data, overwrites it without history, ends sessions,
  restarts or reconfigures the server, or can otherwise not be undone.

admin: true if the endpoint handles secrets, credentials, API keys, auth or
sessions, user accounts or access rights, server/service lifecycle, or
addon/installer distribution - things an assistant working on production
data should not touch by default. Reads count too when the response itself
is sensitive (secret values, API keys).

Answer with JSON only."""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "side_effect": {
            "type": "string",
            "enum": ["read_only", "write", "destructive"],
        },
        "admin": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["side_effect", "admin", "reason"],
}


@dataclass
class Operation:
    """One OpenAPI operation, as shown to the model."""

    method: str
    path: str
    summary: str
    description: str

    @property
    def key(self) -> str:
        """Cache key that changes when the operation text does."""
        text = f"{self.method} {self.path}\n{self.summary}\n{self.description}"
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        return f"{self.method} {self.path} {digest}"


@dataclass
class Suggestion:
    """The model's classification next to the current policy."""

    method: str
    path: str
    summary: str
    current_side_effect: str
    current_admin: bool
    suggested_side_effect: str
    suggested_admin: bool
    reason: str

    @property
    def direction(self) -> str | None:
        """``stricter``/``looser``/``mixed``, or None if they agree.

        Returns:
            How the suggestion differs from the current policy.

        """
        current = SIDE_EFFECT_RANK[SideEffectClass(self.current_side_effect)]
        suggested = SIDE_EFFECT_RANK[
            SideEffectClass(self.suggested_side_effect)
        ]
        stricter = suggested > current or (
            self.suggested_admin and not self.current_admin
        )
        looser = suggested < current or (
            self.current_admin and not self.suggested_admin
        )
        if stricter and looser:
            return "mixed"
        if stricter:
            return "stricter"
        if looser:
            return "looser"
        return None


def load_operations(spec: dict[str, Any]) -> list[Operation]:
    """Return every operation in an OpenAPI spec.

    Returns:
        Operations sorted by path and method.

    """
    operations = []
    for path, item in spec.get("paths", {}).items():
        for method, operation in item.items():
            if method not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            operations.append(
                Operation(
                    method=method.upper(),
                    path=path,
                    summary=str(operation.get("summary") or "").strip(),
                    description=str(
                        operation.get("description") or ""
                    ).strip(),
                )
            )
    return sorted(operations, key=lambda op: (op.path, op.method))


def classify(
    client: httpx.Client,
    *,
    host: str,
    model: str,
    operation: Operation,
) -> dict[str, Any]:
    """Ask the model to classify one operation.

    The answer is checked by ``_validate_answer``, which raises if it
    doesn't match ``RESPONSE_SCHEMA``.

    Returns:
        ``{"side_effect": ..., "admin": ..., "reason": ...}``.

    """
    prompt = (
        f"Endpoint: {operation.method} {operation.path}\n"
        f"Summary: {operation.summary or '-'}\n"
        f"Description: {operation.description[:1500] or '-'}"
    )
    response = client.post(
        f"{host}/api/chat",
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "format": RESPONSE_SCHEMA,
            "stream": False,
            "options": {"temperature": 0.0, "seed": 0},
        },
        timeout=300,
    )
    response.raise_for_status()
    answer = json.loads(response.json()["message"]["content"])
    return _validate_answer(answer)


def _validate_answer(answer: object) -> dict[str, Any]:
    """Check a model answer against ``RESPONSE_SCHEMA``.

    Ollama's ``format`` constrains generation but isn't a guarantee, so
    nothing is coerced: ``"false"`` must not turn into ``True``.

    Returns:
        The answer, with ``reason`` stripped.

    Raises:
        TypeError: If the answer or one of its fields has the wrong type.
        ValueError: If a field is missing or ``side_effect`` is unknown.

    """
    if not isinstance(answer, dict):
        msg = f"expected a JSON object, got {answer!r}"
        raise TypeError(msg)
    missing = [key for key in RESPONSE_SCHEMA["required"] if key not in answer]
    if missing:
        msg = f"answer is missing {missing}: {answer!r}"
        raise ValueError(msg)
    side_effect = answer["side_effect"]
    if side_effect not in {cls.value for cls in SIDE_EFFECT_RANK}:
        msg = f"unexpected side_effect {side_effect!r}"
        raise ValueError(msg)
    if not isinstance(answer["admin"], bool):
        msg = f"admin must be a boolean, got {answer['admin']!r}"
        raise TypeError(msg)
    if not isinstance(answer["reason"], str):
        msg = f"reason must be a string, got {answer['reason']!r}"
        raise TypeError(msg)
    return {
        "side_effect": side_effect,
        "admin": answer["admin"],
        "reason": answer["reason"].strip(),
    }


def suggest(
    operations: list[Operation],
    *,
    host: str,
    model: str,
    cache: dict[str, Any] | None = None,
) -> list[Suggestion]:
    """Classify operations, reusing cached answers where possible.

    Returns:
        One suggestion per operation, agreeing or not.

    """
    cache = {} if cache is None else cache
    suggestions = []
    with httpx.Client() as client:
        for index, operation in enumerate(operations, start=1):
            cache_key = f"{model}|{operation.key}"
            answer = cache.get(cache_key)
            if answer is None:
                sys.stderr.write(
                    f"[{index}/{len(operations)}] "
                    f"{operation.method} {operation.path}\n"
                )
                answer = classify(
                    client, host=host, model=model, operation=operation
                )
                cache[cache_key] = answer
            suggestions.append(
                Suggestion(
                    method=operation.method,
                    path=operation.path,
                    summary=operation.summary,
                    current_side_effect=side_effect_for(
                        operation.method, operation.path
                    ).value,
                    current_admin=is_admin(operation.method, operation.path),
                    suggested_side_effect=answer["side_effect"],
                    suggested_admin=answer["admin"],
                    reason=answer["reason"],
                )
            )
    return suggestions


def _override_line(suggestion: Suggestion) -> str:
    name = SideEffectClass(suggestion.suggested_side_effect).name
    return (
        f"    ({suggestion.method!r}, {suggestion.path!r}): "
        f"SideEffectClass.{name},"
    )


def _admin_line(suggestion: Suggestion) -> str:
    pattern = "^" + re.escape(suggestion.path) + "$"
    return f'        (frozenset({{"{suggestion.method}"}}), r"{pattern}"),'


def format_report(suggestions: list[Suggestion]) -> str:
    """Render disagreements as a review report with paste-ready lines.

    Returns:
        The report text.

    """
    lines: list[str] = []
    for direction, title in (
        ("stricter", "Stricter than current policy (quick review)"),
        ("mixed", "Mixed - stricter in one respect, looser in another"),
        ("looser", "Looser than current policy (needs explicit sign-off)"),
    ):
        group = [s for s in suggestions if s.direction == direction]
        if not group:
            continue
        lines.extend((f"## {title}: {len(group)}", ""))
        for s in group:
            lines.extend((
                f"{s.method} {s.path} - {s.summary or '(no summary)'}",
                (
                    f"  policy: {s.current_side_effect}"
                    f"{', admin' if s.current_admin else ''}"
                    f"  ->  model: {s.suggested_side_effect}"
                    f"{', admin' if s.suggested_admin else ''}"
                ),
                f"  reason: {s.reason}",
            ))
            if s.suggested_side_effect != s.current_side_effect:
                lines.append(
                    f"  SIDE_EFFECT_OVERRIDES: {_override_line(s).strip()}"
                )
            if s.suggested_admin and not s.current_admin:
                lines.append(f"  ADMIN_PATTERNS: {_admin_line(s).strip()}")
            lines.append("")
    agreed = sum(1 for s in suggestions if s.direction is None)
    lines.append(f"{agreed}/{len(suggestions)} operations agree with policy.")
    return "\n".join(lines)


def _load_cache(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def main(argv: list[str] | None = None) -> int:
    """Run the suggester CLI.

    Returns:
        Process exit code.

    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument(
        "--host",
        default=os.environ.get("OLLAMA_HOST", DEFAULT_OLLAMA_HOST),
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL),
    )
    parser.add_argument(
        "--match", help="only operations whose path matches this regex"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="also re-check operations that already have an override",
    )
    parser.add_argument("--limit", type=int, help="check at most N operations")
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument(
        "--json", type=Path, help="also write all suggestions to this file"
    )
    args = parser.parse_args(argv)

    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    operations = load_operations(spec)
    if not args.all:
        operations = [
            op for op in operations
            if (op.method, op.path) not in SIDE_EFFECT_OVERRIDES
        ]
    if args.match:
        pattern = re.compile(args.match)
        operations = [op for op in operations if pattern.search(op.path)]
    if args.limit is not None:
        operations = operations[: args.limit]

    cache_path = None if args.no_cache else args.cache
    cache = _load_cache(cache_path)
    try:
        suggestions = suggest(
            operations,
            host=args.host.rstrip("/"),
            model=args.model,
            cache=cache,
        )
    finally:
        if cache_path is not None:
            cache_path.write_text(
                json.dumps(cache, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

    if args.json:
        args.json.write_text(
            json.dumps([asdict(s) for s in suggestions], indent=2) + "\n",
            encoding="utf-8",
        )
    sys.stdout.write(format_report(suggestions) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
