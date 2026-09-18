"""Run the LLM agent tests repeatedly and aggregate their reports."""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_DIR = REPO_ROOT / "tests" / "reports"
DEFAULT_OUTPUT_DIR = DEFAULT_REPORT_DIR / "benchmarks"
REPORT_PREFIX = "llm_agent_report_"
LOGGER = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run tests/test_llm_agent.py repeatedly and aggregate metrics."
        )
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=3,
        help="Number of test runs (default: 3).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=(
            "Directory for run reports and the aggregate "
            "(default: tests/reports/benchmarks)."
        ),
    )
    parser.add_argument(
        "--pytest-args",
        nargs=argparse.REMAINDER,
        help="Additional arguments passed to pytest after `--pytest-args`.",
    )
    return parser


def _latest_report(report_dir: Path, started_at: float) -> Path:
    reports = [
        path
        for path in report_dir.glob(f"{REPORT_PREFIX}*.json")
        if path.stat().st_mtime >= started_at
    ]
    if not reports:
        message = (
            f"No {REPORT_PREFIX}*.json report was created in {report_dir}"
        )
        raise FileNotFoundError(message)
    return max(reports, key=lambda path: path.stat().st_mtime)


def _average(values: list[float]) -> float:
    return round(sum(values) / len(values), 3) if values else 0.0


def aggregate_reports(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Build aggregate efficiency metrics from individual test reports.

    Returns:
        Aggregate metrics grouped by run and scenario.

    """
    scenario_metrics: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for report in reports:
        for scenario in report.get("scenarios", []):
            metrics = scenario_metrics[scenario["scenario"]]
            metrics["passed"].append(float(scenario["passed"]))
            for field in (
                "turns",
                "prompt_tokens",
                "completion_tokens",
                "wall_seconds",
            ):
                metrics[field].append(float(scenario.get(field, 0)))

    scenario_averages = {}
    for name, metrics in sorted(scenario_metrics.items()):
        scenario_averages[name] = {
            "runs": len(metrics["passed"]),
            "pass_rate": _average(metrics["passed"]),
            "average_turns": _average(metrics["turns"]),
            "average_prompt_tokens": _average(metrics["prompt_tokens"]),
            "average_completion_tokens": _average(
                metrics["completion_tokens"]
            ),
            "average_wall_seconds": _average(metrics["wall_seconds"]),
        }

    totals = {
        field: [float(report.get(field, 0)) for report in reports]
        for field in (
            "total_prompt_tokens",
            "total_completion_tokens",
            "total_wall_seconds",
        )
    }
    pass_rates = [
        report.get("passed", 0) / report.get("scenario_count", 1)
        for report in reports
    ]
    return {
        "run_count": len(reports),
        "model": reports[0].get("model") if reports else None,
        "exposure_mode": reports[0].get("exposure_mode") if reports else None,
        "average_pass_rate": _average(pass_rates),
        "average_total_prompt_tokens": _average(
            totals["total_prompt_tokens"]
        ),
        "average_total_completion_tokens": _average(
            totals["total_completion_tokens"]
        ),
        "average_total_wall_seconds": _average(totals["total_wall_seconds"]),
        "average_prompt_tokens_per_passed_scenario": _average(
            [
                report.get("total_prompt_tokens", 0) / report["passed"]
                for report in reports
                if report.get("passed", 0)
            ]
        ),
        "scenarios": scenario_averages,
    }


def run_benchmark(
    runs: int,
    output_dir: Path,
    pytest_args: list[str] | None = None,
) -> Path:
    """Run the LLM tests and write an aggregate report.

    Args:
        runs: Number of complete pytest runs to execute.
        output_dir: Directory where run and aggregate reports are written.
        pytest_args: Additional arguments forwarded to pytest.

    Returns:
        Path to the aggregate JSON report.

    Raises:
        ValueError: If ``runs`` is less than one.

    """
    if runs < 1:
        message = "--runs must be at least 1"
        raise ValueError(message)

    output_dir.mkdir(parents=True, exist_ok=True)
    reports: list[dict[str, Any]] = []
    run_files: list[str] = []
    for run_number in range(1, runs + 1):
        started_at = datetime.now(UTC).timestamp()
        command = [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_llm_agent.py",
            "-q",
            "-m", "llm",  # pull in llm marked tests
            *(pytest_args or []),
        ]
        LOGGER.info("Run %s/%s: %s", run_number, runs, " ".join(command))
        completed = subprocess.run(command, cwd=REPO_ROOT, check=False)
        source = _latest_report(DEFAULT_REPORT_DIR, started_at)
        destination = output_dir / f"run_{run_number:03d}.json"
        shutil.copyfile(source, destination)
        report = json.loads(destination.read_text(encoding="utf-8"))
        reports.append(report)
        run_files.append(destination.name)
        LOGGER.info(
            "  exit=%s, passed=%s/%s, prompt_tokens=%s",
            completed.returncode,
            report.get("passed", 0),
            report.get("scenario_count", 0),
            report.get("total_prompt_tokens", 0),
        )

    aggregate = aggregate_reports(reports)
    aggregate["generated_at"] = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    aggregate["run_reports"] = run_files
    aggregate_path = output_dir / "aggregate.json"
    aggregate_path.write_text(
        json.dumps(aggregate, indent=2) + "\n", encoding="utf-8"
    )
    _write_summary(output_dir / "aggregate.txt", aggregate)
    return aggregate_path


def _write_summary(path: Path, aggregate: dict[str, Any]) -> None:
    lines = [
        f"Runs: {aggregate['run_count']}",
        f"Model: {aggregate['model']}",
        f"Average pass rate: {aggregate['average_pass_rate']:.1%}",
        (
            "Average prompt tokens: "
            f"{aggregate['average_total_prompt_tokens']:.0f}"
        ),
        (
            "Average completion tokens: "
            f"{aggregate['average_total_completion_tokens']:.0f}"
        ),
        (
            "Average wall time: "
            f"{aggregate['average_total_wall_seconds']:.3f}s"
        ),
        "",
        "Scenario averages:",
    ]
    for name, metrics in aggregate["scenarios"].items():
        lines.append(
            f"  {name}: pass={metrics['pass_rate']:.1%}, "
            f"turns={metrics['average_turns']:.2f}, "
            f"prompt_tokens={metrics['average_prompt_tokens']:.0f}, "
            f"wall_time={metrics['average_wall_seconds']:.3f}s"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    """Run the command-line benchmark and return its exit status.

    Returns:
        Zero on success, otherwise one.

    """
    args = _parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        aggregate_path = run_benchmark(
            args.runs, args.output_dir, args.pytest_args
        )
    except (FileNotFoundError, ValueError):
        LOGGER.exception("Benchmark failed")
        return 1
    LOGGER.info("Aggregate report: %s", aggregate_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
