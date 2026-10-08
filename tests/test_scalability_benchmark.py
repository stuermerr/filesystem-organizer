from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_SCRIPT = PROJECT_ROOT / "scripts" / "benchmark_example_fixture.py"


def test_example_fixture_benchmark_records_fresh_complete_workflow(
    tmp_path: Path,
) -> None:
    history_path = tmp_path / "history.json"

    completed = subprocess.run(
        [
            sys.executable,
            str(BENCHMARK_SCRIPT),
            "--label",
            "test-baseline",
            "--repetitions",
            "1",
            "--history",
            str(history_path),
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["label"] == "test-baseline"
    assert result["repetitions"] == 1
    assert result["source_fixture"] == "example_source"
    assert len(result["source_state"]["revision"]) == 40
    assert len(result["source_state"]["tree"]) == 40
    assert isinstance(result["source_state"]["dirty"], bool)
    assert result["environment"]["system"]
    assert result["environment"]["release"]
    assert result["environment"]["machine"]
    assert result["environment"]["python_implementation"]
    assert result["environment"]["python_version"]
    assert result["environment"]["uv_version"].startswith("uv ")
    assert result["median"]["total_seconds"] > 0
    assert result["median"]["source_files"] > 0
    assert result["median"]["source_bytes"] > 0
    assert result["median"]["database_bytes"] > 0
    assert result["median"]["inventory_entries"] > 0
    assert result["median"]["successful_identity_rows"] > 0
    assert result["median"]["successful_identity_bytes"] > 0
    assert result["median"]["report_bytes"] > 0
    assert result["median"]["full_report_bytes"] > result["median"]["report_bytes"]
    assert result["median"]["plan_report_bytes"] > 0
    assert (
        result["median"]["full_plan_report_bytes"]
        > result["median"]["plan_report_bytes"]
    )
    assert result["median"]["materialized_files"] > 0
    assert result["median"]["materialize_input_blocks"] >= 0
    assert result["median"]["materialize_output_blocks"] >= 0
    assert result["median"]["successful_path_source_bytes_read"] > 0
    assert result["median"]["successful_path_destination_bytes_written"] > 0
    assert result["median"]["materialization_journal_rows"] >= 0
    assert result["median"]["execution_evidence_rows"] > 0
    assert (
        result["median"]["cloned_file_count"]
        + result["median"]["streamed_file_count"]
        == result["median"]["materialized_files"]
    )
    assert set(result["runs"][0]["stages"]) == {
        "scan",
        "report",
        "report-full",
        "plan",
        "plan-report",
        "plan-report-full",
        "plan-finalize",
        "materialize",
    }

    history = json.loads(history_path.read_text(encoding="utf-8"))
    assert history["schema_version"] == 1
    assert history["benchmarks"] == [result]
