"""Benchmark the public consolidation workflow against ``example_source``."""

from __future__ import annotations

import argparse
import json
import platform
import resource
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.example_fixture import copy_prepared_source


def _run_command(*arguments: str) -> tuple[str, float]:
    started = time.perf_counter()
    completed = subprocess.run(
        [sys.executable, "-m", "filesystem_organizer", *arguments],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    elapsed = time.perf_counter() - started
    if completed.returncode != 0:
        raise RuntimeError(
            f"benchmark command failed ({' '.join(arguments)}): {completed.stderr.strip()}"
        )
    return completed.stdout, elapsed


def _workspace_size(path: Path) -> int:
    return sum(
        candidate.stat().st_size for candidate in path.rglob("*") if candidate.is_file()
    )


def _run_once() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="fso-example-benchmark-") as temporary:
        benchmark_root = Path(temporary)
        selected_root = copy_prepared_source(benchmark_root / "selected-backup-root")
        output_root = benchmark_root / "run-output"
        source_files = [
            candidate
            for candidate in selected_root.rglob("*")
            if candidate.is_file() and not candidate.is_symlink()
        ]
        source_bytes = sum(candidate.stat().st_size for candidate in source_files)
        stages: dict[str, float] = {}
        usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
        total_started = time.perf_counter()

        scan_output, stages["scan"] = _run_command(
            "scan", str(selected_root), "--output-root", str(output_root)
        )
        scan = json.loads(scan_output)
        run_path = Path(scan["analysis_run"])

        report, stages["report"] = _run_command("report", str(run_path))
        full_report, stages["report-full"] = _run_command(
            "report", str(run_path), "--detail", "full"
        )
        plan_output, stages["plan"] = _run_command("plan", str(run_path))
        plan = json.loads(plan_output)
        plan_id = str(plan["plan_id"])
        plan_report, stages["plan-report"] = _run_command(
            "plan-report", str(run_path), "--plan-id", plan_id
        )
        full_plan_report, stages["plan-report-full"] = _run_command(
            "plan-report", str(run_path), "--plan-id", plan_id, "--detail", "full"
        )
        _, stages["plan-finalize"] = _run_command(
            "plan-finalize", str(run_path), "--plan-id", plan_id
        )
        materialize_usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
        materialize_output, stages["materialize"] = _run_command(
            "materialize", str(run_path), plan_id, "--yes"
        )
        materialize_usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)
        materialized = json.loads(materialize_output)
        total_seconds = time.perf_counter() - total_started
        usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)

        database_path = run_path / "analysis.sqlite3"
        with sqlite3.connect(database_path) as connection:
            inventory_entries = int(
                connection.execute("SELECT COUNT(*) FROM inventory_entries").fetchone()[
                    0
                ]
            )
            successful_identity_rows, successful_identity_bytes = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(byte_size), 0) "
                "FROM content_identities WHERE read_outcome = 'successful'"
            ).fetchone()
            candidates = int(
                connection.execute(
                    "SELECT candidate_count FROM structural_analysis"
                ).fetchone()[0]
            )
            relationships = int(
                connection.execute(
                    "SELECT COUNT(*) FROM directory_relationships"
                ).fetchone()[0]
            )
            comparison_count = int(
                connection.execute(
                    "SELECT comparison_count FROM structural_analysis"
                ).fetchone()[0]
            )
            materialization_journal_rows = int(
                connection.execute(
                    "SELECT COUNT(*) FROM materialization_events WHERE plan_id = ?",
                    (plan_id,),
                ).fetchone()[0]
            )
            attempt = connection.execute(
                "SELECT cloned_file_count, cloned_byte_count, streamed_file_count, "
                "streamed_byte_count FROM materialization_attempts WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            execution_evidence_rows = int(
                connection.execute(
                    "SELECT COUNT(*) FROM execution_file_evidence WHERE plan_id = ?",
                    (plan_id,),
                ).fetchone()[0]
            )
            successful_path_bytes = int(
                connection.execute(
                    "SELECT COALESCE(SUM(expected_byte_size), 0) "
                    "FROM execution_manifest_entries AS entry "
                    "JOIN execution_manifests AS manifest USING (attempt_id) "
                    "WHERE manifest.plan_id = ? AND entry.entry_kind = 'file'",
                    (plan_id,),
                ).fetchone()[0]
            )

        destination = Path(str(materialized["destination"]))
        return {
            "total_seconds": total_seconds,
            "source_files": len(source_files),
            "source_bytes": source_bytes,
            "stages": stages,
            "database_bytes": database_path.stat().st_size,
            "wal_bytes": (
                database_path.with_name(database_path.name + "-wal").stat().st_size
                if database_path.with_name(database_path.name + "-wal").exists()
                else 0
            ),
            "workspace_bytes": _workspace_size(run_path),
            "inventory_entries": inventory_entries,
            "successful_identity_rows": int(successful_identity_rows),
            "successful_identity_bytes": int(successful_identity_bytes),
            "structural_candidates": candidates,
            "structural_relationships": relationships,
            "structural_comparisons": comparison_count,
            "report_bytes": len(report.encode("utf-8")),
            "full_report_bytes": len(full_report.encode("utf-8")),
            "plan_report_bytes": len(plan_report.encode("utf-8")),
            "full_plan_report_bytes": len(full_plan_report.encode("utf-8")),
            "materialized_files": sum(
                candidate.is_file() for candidate in destination.rglob("*")
            ),
            "materialize_input_blocks": (
                materialize_usage_after.ru_inblock - materialize_usage_before.ru_inblock
            ),
            "materialize_output_blocks": (
                materialize_usage_after.ru_oublock - materialize_usage_before.ru_oublock
            ),
            "successful_path_source_bytes_read": successful_path_bytes,
            "successful_path_destination_bytes_written": successful_path_bytes,
            "materialization_journal_rows": materialization_journal_rows,
            "execution_evidence_rows": execution_evidence_rows,
            "cloned_file_count": int(attempt[0]),
            "cloned_byte_count": int(attempt[1]),
            "streamed_file_count": int(attempt[2]),
            "streamed_byte_count": int(attempt[3]),
            "input_blocks": usage_after.ru_inblock - usage_before.ru_inblock,
            "output_blocks": usage_after.ru_oublock - usage_before.ru_oublock,
            "max_rss_kib": usage_after.ru_maxrss,
        }


def _median(runs: list[dict[str, Any]]) -> dict[str, int | float]:
    metric_names = (
        "total_seconds",
        "source_files",
        "source_bytes",
        "database_bytes",
        "wal_bytes",
        "workspace_bytes",
        "inventory_entries",
        "successful_identity_rows",
        "successful_identity_bytes",
        "structural_candidates",
        "structural_relationships",
        "structural_comparisons",
        "report_bytes",
        "full_report_bytes",
        "plan_report_bytes",
        "full_plan_report_bytes",
        "materialized_files",
        "materialize_input_blocks",
        "materialize_output_blocks",
        "successful_path_source_bytes_read",
        "successful_path_destination_bytes_written",
        "materialization_journal_rows",
        "execution_evidence_rows",
        "cloned_file_count",
        "cloned_byte_count",
        "streamed_file_count",
        "streamed_byte_count",
        "input_blocks",
        "output_blocks",
        "max_rss_kib",
    )
    result = {
        name: statistics.median(run[name] for run in runs) for name in metric_names
    }
    for stage in runs[0]["stages"]:
        result[f"{stage}_seconds"] = statistics.median(
            run["stages"][stage] for run in runs
        )
    return result


def _record_history(path: Path, result: dict[str, Any]) -> None:
    if path.exists():
        history = json.loads(path.read_text(encoding="utf-8"))
        if history.get("schema_version") != 1:
            raise ValueError(f"Unsupported benchmark history schema: {path}")
    else:
        history = {"schema_version": 1, "benchmarks": []}
    history["benchmarks"].append(result)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(history, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _git_source_state() -> dict[str, object]:
    def git(*arguments: str) -> str:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        return completed.stdout.strip()

    return {
        "revision": git("rev-parse", "HEAD"),
        "tree": git("rev-parse", "HEAD^{tree}"),
        "dirty": bool(git("status", "--porcelain", "--untracked-files=normal")),
    }


def _environment_state() -> dict[str, str]:
    uv = subprocess.run(
        ["uv", "--version"],
        text=True,
        capture_output=True,
        check=True,
    )
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "uv_version": uv.stdout.strip(),
    }


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--history", type=Path, required=True)
    options = parser.parse_args(arguments)
    if options.repetitions < 1:
        parser.error("--repetitions must be at least 1")

    runs = [_run_once() for _ in range(options.repetitions)]
    result = {
        "label": options.label,
        "recorded_at": datetime.now(UTC).isoformat(),
        "source_fixture": "example_source",
        "repetitions": options.repetitions,
        "environment": _environment_state(),
        "source_state": _git_source_state(),
        "median": _median(runs),
        "runs": runs,
    }
    _record_history(options.history, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
