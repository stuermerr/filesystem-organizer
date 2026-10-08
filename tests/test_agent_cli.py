from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from tests.test_cli import PROJECT_ROOT, run_cli, tree_digest


def run_preflight_with_free_bytes(
    working_directory: Path, free_bytes: int, *arguments: str
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(PROJECT_ROOT)
    launcher = (
        "import shutil, sys, types; "
        "shutil.disk_usage = lambda path: types.SimpleNamespace("
        "total=0, used=0, free=int(sys.argv[1])); "
        "from filesystem_organizer.__main__ import main; "
        "raise SystemExit(main(sys.argv[2:]))"
    )
    return subprocess.run(
        [sys.executable, "-c", launcher, str(free_bytes), *arguments],
        cwd=working_directory,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_runs_reports_an_empty_default_run_output_root(tmp_path: Path) -> None:
    result = run_cli(tmp_path, "runs")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "run_output_root": str((tmp_path / "output").resolve()),
        "runs": [],
    }
    assert result.stderr == ""
    assert not (tmp_path / "output").exists()


def test_runs_reports_exactly_one_run(tmp_path: Path) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "one.txt").write_text("one")
    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    scan_result = json.loads(scan.stdout)

    result = run_cli(tmp_path, "runs")

    assert result.returncode == 0, result.stderr
    assert len(json.loads(result.stdout)["runs"]) == 1
    assert json.loads(result.stdout)["runs"][0]["run_id"] == scan_result["run_id"]


def test_runs_discovers_complete_and_incomplete_runs_deterministically(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "one.txt").write_text("one")
    output_root = tmp_path / "run output"
    scans = [
        run_cli(
            tmp_path,
            "scan",
            str(selected_root),
            "--output-root",
            str(output_root),
        )
        for _ in range(2)
    ]
    assert all(scan.returncode == 0 for scan in scans)
    scan_results = [json.loads(scan.stdout) for scan in scans]
    incomplete = scan_results[1]
    database_path = Path(incomplete["analysis_run"]) / "analysis.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE analysis_runs SET status = 'scanning', completed_at = NULL "
            "WHERE run_id = ?",
            (incomplete["run_id"],),
        )
    before = database_path.read_bytes()

    result = run_cli(tmp_path, "runs", str(output_root))

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["run_output_root"] == str(output_root.resolve())
    assert payload["runs"] == sorted(
        payload["runs"], key=lambda run: run["analysis_run"]
    )
    assert {run["status"] for run in payload["runs"]} == {"complete", "scanning"}
    assert {run["run_id"] for run in payload["runs"]} == {
        scan["run_id"] for scan in scan_results
    }
    for run in payload["runs"]:
        assert set(run) == {
            "analysis_run",
            "completed_at",
            "run_id",
            "selected_backup_root",
            "snapshot_id",
            "started_at",
            "status",
        }
        assert run["selected_backup_root"] == str(selected_root.resolve())
    assert database_path.read_bytes() == before


def test_runs_refuses_unsafe_and_ambiguous_workspace_layouts(tmp_path: Path) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "one.txt").write_text("one")
    output_root = tmp_path / "output"
    scan = run_cli(
        tmp_path,
        "scan",
        str(selected_root),
        "--output-root",
        str(output_root),
    )
    assert scan.returncode == 0, scan.stderr
    scan_result = json.loads(scan.stdout)
    run_path = Path(scan_result["analysis_run"])
    duplicate = output_root / "runs" / "duplicate"
    duplicate.mkdir()
    (duplicate / "analysis.sqlite3").hardlink_to(run_path / "analysis.sqlite3")

    ambiguous = run_cli(tmp_path, "runs", str(output_root))

    assert ambiguous.returncode == 1
    assert ambiguous.stdout == ""
    assert "ambiguous duplicate run identities" in ambiguous.stderr

    unsafe_output = tmp_path / "unsafe-output"
    (unsafe_output / "runs").mkdir(parents=True)
    escaped = tmp_path / "escaped"
    escaped.mkdir()
    (unsafe_output / "runs" / "escaped").symlink_to(escaped, target_is_directory=True)

    unsafe = run_cli(tmp_path, "runs", str(unsafe_output))

    assert unsafe.returncode == 1
    assert unsafe.stdout == ""
    assert "unsafe Analysis Run entry" in unsafe.stderr


def test_runs_refuses_malformed_and_unsupported_workspaces(tmp_path: Path) -> None:
    malformed_output = tmp_path / "malformed-output"
    (malformed_output / "runs" / "broken").mkdir(parents=True)

    malformed = run_cli(tmp_path, "runs", str(malformed_output))

    assert malformed.returncode == 1
    assert "missing a safe analysis.sqlite3" in malformed.stderr

    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "one.txt").write_text("one")
    unsupported_output = tmp_path / "unsupported-output"
    scan = run_cli(
        tmp_path,
        "scan",
        str(selected_root),
        "--output-root",
        str(unsupported_output),
    )
    assert scan.returncode == 0, scan.stderr
    scan_result = json.loads(scan.stdout)
    with sqlite3.connect(
        Path(scan_result["analysis_run"]) / "analysis.sqlite3"
    ) as connection:
        connection.execute("UPDATE analysis_runs SET schema_version = 999")

    unsupported = run_cli(tmp_path, "runs", str(unsupported_output))

    assert unsupported.returncode == 1
    assert "Unsupported Analysis Run schema version 999" in unsupported.stderr


def test_status_reports_complete_run_without_plans_read_only(tmp_path: Path) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "one.txt").write_text("one")
    (selected_root / "skipped").symlink_to(tmp_path / "elsewhere")
    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    scan_result = json.loads(scan.stdout)
    run_path = Path(scan_result["analysis_run"])
    database_path = run_path / "analysis.sqlite3"
    before = database_path.read_bytes()

    result = run_cli(tmp_path, "status", str(run_path))

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == {
        "analysis_run": str(run_path),
        "checkpoint_relative_path": "skipped",
        "completed_at": payload["completed_at"],
        "hash_checkpoint_relative_path": None,
        "inventory_count": 1,
        "plans": [],
        "recovery_available": False,
        "run_id": scan_result["run_id"],
        "schema_version": 10,
        "selected_backup_root": str(selected_root.resolve()),
        "skipped_entry_finding_count": 1,
        "snapshot_id": scan_result["snapshot_id"],
        "started_at": payload["started_at"],
        "status": "complete",
        "writer_active": False,
    }
    assert payload["completed_at"]
    assert payload["started_at"]
    assert database_path.read_bytes() == before


def test_status_refuses_missing_malformed_unsupported_and_ambiguous_runs(
    tmp_path: Path,
) -> None:
    missing = run_cli(tmp_path, "status", str(tmp_path / "missing"))
    assert missing.returncode == 1
    assert "does not exist" in missing.stderr

    malformed_run = tmp_path / "malformed"
    malformed_run.mkdir()
    malformed = run_cli(tmp_path, "status", str(malformed_run))
    assert malformed.returncode == 1
    assert "missing a safe analysis.sqlite3" in malformed.stderr

    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "one.txt").write_text("one")
    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    scan_result = json.loads(scan.stdout)
    run_path = Path(scan_result["analysis_run"])
    database_path = run_path / "analysis.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE analysis_runs SET schema_version = 999")
    unsupported = run_cli(tmp_path, "status", str(run_path))
    assert unsupported.returncode == 1
    assert "Unsupported Analysis Run schema version 999" in unsupported.stderr

    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE analysis_runs SET schema_version = 10")
        connection.execute(
            "INSERT INTO analysis_runs VALUES "
            "('other-run', 'other-snapshot', 10, ?, 'complete', ?, ?, NULL, NULL, NULL)",
            (
                str(selected_root.resolve()),
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
    ambiguous = run_cli(tmp_path, "status", str(run_path))
    assert ambiguous.returncode == 1
    assert "exactly one run identity" in ambiguous.stderr


def test_status_reports_plan_lifecycle_and_materialization_recovery_state(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "one.txt").write_text("one")
    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])

    first_plan = run_cli(tmp_path, "plan", str(run_path))
    assert first_plan.returncode == 0, first_plan.stderr
    first = json.loads(first_plan.stdout)
    finalized = run_cli(
        tmp_path,
        "plan-finalize",
        str(run_path),
        "--plan-id",
        first["plan_id"],
    )
    assert finalized.returncode == 0, finalized.stderr
    materialized = run_cli(
        tmp_path, "materialize", str(run_path), first["plan_id"], "--yes"
    )
    assert materialized.returncode == 0, materialized.stderr

    second_plan = run_cli(tmp_path, "plan", str(run_path))
    assert second_plan.returncode == 0, second_plan.stderr
    second = json.loads(second_plan.stdout)

    result = run_cli(tmp_path, "status", str(run_path))

    assert result.returncode == 0, result.stderr
    plans = json.loads(result.stdout)["plans"]
    assert plans == sorted(
        plans, key=lambda plan: (plan["created_at"], plan["plan_id"])
    )
    assert {plan["plan_id"] for plan in plans} == {
        first["plan_id"],
        second["plan_id"],
    }
    by_id = {plan["plan_id"]: plan for plan in plans}
    assert by_id[first["plan_id"]]["status"] == "finalized"
    assert by_id[first["plan_id"]]["finalized_at"]
    assert by_id[first["plan_id"]]["materialization_status"] == "complete"
    assert by_id[first["plan_id"]]["recovery_available"] is False
    assert by_id[second["plan_id"]] == {
        "created_at": by_id[second["plan_id"]]["created_at"],
        "finalized_at": None,
        "intended_destination": second["intended_destination"],
        "materialization_status": "not-started",
        "plan_id": second["plan_id"],
        "recovery_available": False,
        "status": "draft",
    }


def test_materialize_preflight_emits_json_without_mutating_any_workspace(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "one.txt").write_text("one")
    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_result = json.loads(plan.stdout)
    plan_id = plan_result["plan_id"]
    finalized = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)
    assert finalized.returncode == 0, finalized.stderr
    destination = Path(plan_result["intended_destination"])
    database_path = run_path / "analysis.sqlite3"
    database_before = database_path.read_bytes()
    source_before = tree_digest(selected_root)

    result = run_cli(tmp_path, "materialize-preflight", str(run_path), plan_id)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == {
        "analysis_run": str(run_path),
        "destination": str(destination),
        "explicit_directory_count": 0,
        "free_bytes": payload["free_bytes"],
        "operation_count": 1,
        "plan_id": plan_id,
        "run_id": plan_result["run_id"],
        "selected_backup_root": str(selected_root.resolve()),
        "structural_union_count": 0,
        "sufficient_free_space": True,
        "total_bytes": 3,
    }
    assert payload["free_bytes"] >= payload["total_bytes"]
    assert result.stderr == ""
    assert not destination.exists()
    assert database_path.read_bytes() == database_before
    assert tree_digest(selected_root) == source_before


def test_materialize_preflight_refuses_source_drift_before_destination_creation(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    source = selected_root / "one.txt"
    source.write_text("one")
    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_result = json.loads(plan.stdout)
    plan_id = plan_result["plan_id"]
    finalized = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)
    assert finalized.returncode == 0, finalized.stderr
    destination = Path(plan_result["intended_destination"])
    source.write_text("two")

    result = run_cli(tmp_path, "materialize-preflight", str(run_path), plan_id)

    assert result.returncode == 1
    assert result.stdout == ""
    assert "metadata" in result.stderr
    assert not destination.exists()


def test_status_reports_interrupted_run_checkpoint_as_recoverable(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "one.txt").write_text("one")
    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    scan_result = json.loads(scan.stdout)
    run_path = Path(scan_result["analysis_run"])
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        connection.execute(
            "UPDATE analysis_runs SET status = 'scanning', completed_at = NULL "
            "WHERE run_id = ?",
            (scan_result["run_id"],),
        )

    result = run_cli(tmp_path, "status", str(run_path))

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "scanning"
    assert payload["checkpoint_relative_path"] == "one.txt"
    assert payload["completed_at"] is None
    assert payload["recovery_available"] is True
    assert payload["writer_active"] is False


def test_materialize_preflight_uses_explicit_custom_destination(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "one.txt").write_text("one")
    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_id = json.loads(plan.stdout)["plan_id"]
    finalized = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)
    assert finalized.returncode == 0, finalized.stderr
    destination = tmp_path / "custom destination"

    result = run_cli(
        tmp_path,
        "materialize-preflight",
        str(run_path),
        plan_id,
        "--destination",
        str(destination),
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["destination"] == str(destination.resolve())
    assert not destination.exists()


def test_materialize_preflight_refuses_draft_unknown_and_occupied_plans(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "one.txt").write_text("one")
    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_id = json.loads(plan.stdout)["plan_id"]

    draft = run_cli(tmp_path, "materialize-preflight", str(run_path), plan_id)
    unknown = run_cli(tmp_path, "materialize-preflight", str(run_path), "unknown")

    assert draft.returncode == 1
    assert "not finalized" in draft.stderr
    assert unknown.returncode == 1
    assert "not found" in unknown.stderr

    finalized = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)
    assert finalized.returncode == 0, finalized.stderr
    destination = tmp_path / "occupied"
    destination.mkdir()
    (destination / "stray.txt").write_text("occupied")
    occupied = run_cli(
        tmp_path,
        "materialize-preflight",
        str(run_path),
        plan_id,
        "--destination",
        str(destination),
    )

    assert occupied.returncode == 1
    assert "already exists" in occupied.stderr
    assert (destination / "stray.txt").read_text() == "occupied"


def test_materialize_preflight_refuses_unsafe_destination_shapes(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "one.txt").write_text("one")
    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_id = json.loads(plan.stdout)["plan_id"]
    finalized = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)
    assert finalized.returncode == 0, finalized.stderr

    inside = selected_root / "unsafe"
    inside_result = run_cli(
        tmp_path,
        "materialize-preflight",
        str(run_path),
        plan_id,
        "--destination",
        str(inside),
    )
    assert inside_result.returncode == 1
    assert "outside the Selected Backup Root" in inside_result.stderr
    assert not inside.exists()

    regular_file = tmp_path / "occupied-file"
    regular_file.write_text("occupied")
    file_result = run_cli(
        tmp_path,
        "materialize-preflight",
        str(run_path),
        plan_id,
        "--destination",
        str(regular_file),
    )
    assert file_result.returncode == 1
    assert "already exists" in file_result.stderr
    assert regular_file.read_text() == "occupied"


def test_materialize_preflight_refuses_invalid_structural_snapshot(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    for name in ("first", "second"):
        root = selected_root / name
        root.mkdir(parents=True)
        (root / "shared.txt").write_text("same")
        for region in ("shared-region-one", "shared-region-two"):
            (root / region).mkdir()
            (root / region / "anchor.txt").write_text(region)
    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_result = json.loads(plan.stdout)
    plan_id = plan_result["plan_id"]
    finalized = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)
    assert finalized.returncode == 0, finalized.stderr
    (selected_root / "first" / "added.txt").write_text("drift")

    result = run_cli(tmp_path, "materialize-preflight", str(run_path), plan_id)

    assert result.returncode == 1
    assert "Structural Snapshot Revalidation failed" in result.stderr
    assert not Path(plan_result["intended_destination"]).exists()


def test_materialize_preflight_reports_insufficient_space_as_successful_json(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "one.txt").write_text("one")
    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_result = json.loads(plan.stdout)
    plan_id = plan_result["plan_id"]
    finalized = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)
    assert finalized.returncode == 0, finalized.stderr

    result = run_preflight_with_free_bytes(
        tmp_path,
        0,
        "materialize-preflight",
        str(run_path),
        plan_id,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["free_bytes"] == 0
    assert payload["total_bytes"] == 3
    assert payload["sufficient_free_space"] is False
    assert not Path(plan_result["intended_destination"]).exists()


def test_materialize_preflight_facts_match_materialization_result(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "one.txt").write_text("one")
    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_id = json.loads(plan.stdout)["plan_id"]
    finalized = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)
    assert finalized.returncode == 0, finalized.stderr

    preflight_result = run_cli(
        tmp_path, "materialize-preflight", str(run_path), plan_id
    )
    assert preflight_result.returncode == 0, preflight_result.stderr
    preflight = json.loads(preflight_result.stdout)
    materialize_result = run_cli(
        tmp_path, "materialize", str(run_path), plan_id, "--yes"
    )

    assert materialize_result.returncode == 0, materialize_result.stderr
    materialized = json.loads(materialize_result.stdout)
    for field in (
        "analysis_run",
        "destination",
        "explicit_directory_count",
        "operation_count",
        "plan_id",
        "run_id",
        "total_bytes",
    ):
        assert materialized[field] == preflight[field]
