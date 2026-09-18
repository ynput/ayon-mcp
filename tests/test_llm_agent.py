"""Ollama-driven functional tests for the AYON MCP server.

A local Ollama model drives the MCP server's discovery tools
(`list_ayon_tools` / `search_ayon_tools` / `get_ayon_tool_schema` /
`call_ayon_tool`) against a real AYON project created by the `project`
fixture, and results/metrics are checked deterministically and written
to a JSON report under `tests/reports/`.

Requires:
    AYON_SERVER_URL, AYON_API_KEY - a live AYON server (see `project` fixture)
    A running Ollama daemon with a tool-calling model pulled, e.g.:
        ollama pull qwen2.5:7b

Configure via:
    OLLAMA_HOST  - default http://localhost:11434
    OLLAMA_MODEL - default qwen2.5:7b

Optional: when OTEL_EXPORTER_OTLP_ENDPOINT points at a reachable collector
(e.g. the AYON Vector/Tempo observability stack), the spawned MCP server is
launched through `opentelemetry-instrument` so its own metrics/traces are
emitted there too.

Run with:
    pytest -m llm -v tests/test_llm_agent.py
"""
from __future__ import annotations

from pathlib import Path

import pytest
from llm_agent import (
    DISCOVERY_SYSTEM_PROMPT,
    ScenarioOutcome,
    ScenarioReport,
    open_mcp_session,
    run_agent,
)

pytestmark = [
    pytest.mark.llm,
    pytest.mark.server,
    pytest.mark.usefixtures("require_ollama"),
]

REPORT_DIR = Path(__file__).parent / "reports"


@pytest.fixture(scope="module")
def scenario_report(ollama_model: str, telemetry_enabled: bool):
    """Collect scenario outcomes for this module and write them out."""
    report = ScenarioReport(
        model=ollama_model,
        exposure_mode="discovery",
        telemetry_enabled=telemetry_enabled,
    )
    yield report
    path = report.write(REPORT_DIR)
    print(f"\nLLM agent report written to {path}")


def _record(
    scenario_report: ScenarioReport,
    scenario: str,
    checks: dict[str, bool],
    run,
) -> ScenarioOutcome:
    outcome = ScenarioOutcome(scenario=scenario, checks=checks, run=run)
    scenario_report.add(outcome)
    return outcome


@pytest.mark.asyncio
async def test_agent_lists_folders_in_project(
    mcp_llm_server_params, ollama_host, ollama_model, project, scenario_report
):
    prompt = (
        f"List the folders in the AYON project '{project.project_name}' "
        "and tell me their names."
    )
    async with open_mcp_session(mcp_llm_server_params) as session:
        run = await run_agent(
            session,
            host=ollama_host,
            model=ollama_model,
            system_prompt=DISCOVERY_SYSTEM_PROMPT,
            user_prompt=prompt,
        )

    checks = {
        "called_expected_tool": any(
            name in {"list_folders", "get_folder_hierarchy"}
            for name in run.resolved_tool_names()
        ),
        "mentions_folder_name": project.folder.name.lower()
        in run.final_text.lower(),
    }
    outcome = _record(
        scenario_report, "list_folders_in_project", checks, run
    )
    assert outcome.passed, outcome.to_dict()


@pytest.mark.asyncio
async def test_agent_reports_task_status(
    mcp_llm_server_params, ollama_host, ollama_model, project, scenario_report
):
    prompt = (
        f"In the AYON project '{project.project_name}', what is the "
        f"status of the task named '{project.task.name}'?"
    )
    async with open_mcp_session(mcp_llm_server_params) as session:
        run = await run_agent(
            session,
            host=ollama_host,
            model=ollama_model,
            system_prompt=DISCOVERY_SYSTEM_PROMPT,
            user_prompt=prompt,
        )

    # task_entity is the raw create-task response, which carries no
    # status field; the project fixture's anatomy defines exactly one
    # status, and new tasks default to it.
    status = "not_started"
    checks = {
        "called_expected_tool": any(
            name in {"list_tasks", "get_entity"}
            for name in run.resolved_tool_names()
        ),
        "mentions_status": status.replace("_", " ").lower()
        in run.final_text.lower()
        or status.lower() in run.final_text.lower(),
    }
    outcome = _record(scenario_report, "reports_task_status", checks, run)
    assert outcome.passed, outcome.to_dict()


@pytest.mark.asyncio
async def test_agent_counts_versions_for_product(
    mcp_llm_server_params, ollama_host, ollama_model, project, scenario_report
):
    prompt = (
        f"How many versions exist for the product '{project.product.name}' "
        f"in the AYON project '{project.project_name}'? Answer with a "
        "number."
    )
    async with open_mcp_session(mcp_llm_server_params) as session:
        run = await run_agent(
            session,
            host=ollama_host,
            model=ollama_model,
            system_prompt=DISCOVERY_SYSTEM_PROMPT,
            user_prompt=prompt,
        )

    checks = {
        "called_expected_tool": "list_versions" in run.resolved_tool_names(),
        "mentions_one_version": "1" in run.final_text,
    }
    outcome = _record(
        scenario_report, "counts_versions_for_product", checks, run
    )
    assert outcome.passed, outcome.to_dict()


@pytest.mark.asyncio
async def test_agent_creates_folder_after_authorization(
    mcp_llm_server_params, ollama_host, ollama_model, project, scenario_report
):
    prompt = (
        f"In the AYON project '{project.project_name}', create a new "
        "folder named 'mcp_llm_test_folder' of type 'Asset' under the "
        f"parent folder id {project.folder.id}. I explicitly authorize "
        "this change - go ahead and create it now."
    )
    async with open_mcp_session(mcp_llm_server_params) as session:
        run = await run_agent(
            session,
            host=ollama_host,
            model=ollama_model,
            system_prompt=DISCOVERY_SYSTEM_PROMPT,
            user_prompt=prompt,
        )

    create_calls = [
        c for c in run.tool_calls if c.resolved_name == "create_entity"
    ]
    checks = {
        "called_create_entity": bool(create_calls),
        "confirmed_mutation": any(
            c.arguments.get("confirm_mutation") is True for c in create_calls
        ),
        "creation_succeeded": any(
            isinstance(c.result, dict)
            and c.result.get("success") is True
            and isinstance(c.result.get("result"), dict)
            and c.result["result"].get("entityId")
            for c in create_calls
        ),
    }
    outcome = _record(
        scenario_report, "creates_folder_after_authorization", checks, run
    )
    assert outcome.passed, outcome.to_dict()


@pytest.mark.asyncio
async def test_agent_adds_comment_after_authorization(
    mcp_llm_server_params, ollama_host, ollama_model, project, scenario_report
):
    prompt = (
        f"In the AYON project '{project.project_name}', add the comment "
        "'Reviewed by MCP LLM test' to the task with id "
        f"{project.task.id}. I explicitly authorize this change - go "
        "ahead and post it now."
    )
    async with open_mcp_session(mcp_llm_server_params) as session:
        run = await run_agent(
            session,
            host=ollama_host,
            model=ollama_model,
            system_prompt=DISCOVERY_SYSTEM_PROMPT,
            user_prompt=prompt,
        )

    comment_calls = [
        c for c in run.tool_calls if c.resolved_name == "add_comment"
    ]
    checks = {
        "called_add_comment": bool(comment_calls),
        "confirmed_mutation": any(
            c.arguments.get("confirm_mutation") is True for c in comment_calls
        ),
        "comment_succeeded": any(
            isinstance(c.result, dict)
            and c.result.get("success") is True
            and isinstance(c.result.get("result"), dict)
            and c.result["result"].get("activityId")
            for c in comment_calls
        ),
    }
    outcome = _record(
        scenario_report, "adds_comment_after_authorization", checks, run
    )
    assert outcome.passed, outcome.to_dict()
