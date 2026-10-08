from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from tests.example_fixture import OUTPUT_ROOT, copy_prepared_source
from tests.test_cli import run_cli, tree_digest
from tests.test_example_fixture import OMITTED_SOURCE_PATHS

MATERIALIZATION_CRASH_POINTS = (
    "admission",
    "copying",
    "postwritten",
)

TARGET_INVENTORY_COUNT = 33
TARGET_SKIPPED_COUNT = 2
TARGET_OPERATION_COUNT = 30
TARGET_SAME_SIZE_NONDUPLICATE_BYTES = 31


@dataclass
class MilestoneRun:
    selected_root: Path
    output_root: Path
    run_path: Path
    run_id: str
    snapshot_id: str
    plan_id: str
    destination: Path
    scan_result: dict[str, object]
    plan_result: dict[str, object]
    finalize_result: dict[str, object]
    materialize_result: dict[str, object]
    report_markdown: str
    full_report_markdown: str
    plan_report_markdown: str
    full_plan_report_markdown: str
    finalized_plan_report_markdown: str
    finalized_full_plan_report_markdown: str


def relative_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def relative_directories(root: Path) -> set[str]:
    """Return every directory below a Materialized Consolidation root."""
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_dir() and not path.is_symlink()
    }


def _assert_matches_example_output(materialized: Path) -> None:
    expected = relative_files(OUTPUT_ROOT)
    actual = relative_files(materialized)
    assert set(actual) == set(expected), (
        "Materialized Consolidation file paths differ from the example output"
    )
    for relative_path, expected_bytes in expected.items():
        assert actual[relative_path] == expected_bytes, relative_path
    assert relative_directories(materialized) == relative_directories(
        OUTPUT_ROOT
    ), "Materialized Consolidation directory paths differ from the example output"
    assert not any(path.is_symlink() for path in materialized.rglob("*")), (
        "Materialized Consolidation contains a symbolic link"
    )


def _scan_fixture(
    tmp_path: Path, selected_root: Path, output_root: Path
) -> dict[str, object]:
    scan = run_cli(
        tmp_path, "scan", str(selected_root), "--output-root", str(output_root)
    )
    assert scan.returncode == 0, scan.stderr
    return cast(dict[str, object], json.loads(scan.stdout))


def _run_path(scan_result: dict[str, object]) -> Path:
    return Path(str(scan_result["analysis_run"]))


def _create_plan(tmp_path: Path, run_path: Path) -> str:
    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    return str(json.loads(plan.stdout)["plan_id"])


def _finalize_plan(tmp_path: Path, run_path: Path, plan_id: str) -> None:
    finalize = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)
    assert finalize.returncode == 0, finalize.stderr


def _run_milestone_workflow(tmp_path: Path) -> MilestoneRun:
    """Execute scan -> report -> plan -> plan-report -> finalize -> materialize."""
    selected_root = copy_prepared_source(tmp_path / "selected-backup-root")
    output_root = tmp_path / "output"
    before = tree_digest(selected_root)

    scan_result = _scan_fixture(tmp_path, selected_root, output_root)
    run_path = _run_path(scan_result)
    run_id = str(scan_result["run_id"])
    snapshot_id = str(scan_result["snapshot_id"])

    report = run_cli(tmp_path, "report", str(run_path))
    assert report.returncode == 0, report.stderr
    full_report = run_cli(tmp_path, "report", str(run_path), "--detail", "full")
    assert full_report.returncode == 0, full_report.stderr

    plan_result = json.loads(run_cli(tmp_path, "plan", str(run_path)).stdout)
    plan_id = str(plan_result["plan_id"])

    plan_report = run_cli(tmp_path, "plan-report", str(run_path), "--plan-id", plan_id)
    assert plan_report.returncode == 0, plan_report.stderr
    full_plan_report = run_cli(
        tmp_path, "plan-report", str(run_path), "--plan-id", plan_id, "--detail", "full"
    )
    assert full_plan_report.returncode == 0, full_plan_report.stderr

    finalize_result = json.loads(
        run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id).stdout
    )

    finalized_plan_report = run_cli(
        tmp_path, "plan-report", str(run_path), "--plan-id", plan_id
    )
    assert finalized_plan_report.returncode == 0, finalized_plan_report.stderr
    finalized_full_plan_report = run_cli(
        tmp_path,
        "plan-report",
        str(run_path),
        "--plan-id",
        plan_id,
        "--detail",
        "full",
    )
    assert finalized_full_plan_report.returncode == 0, finalized_full_plan_report.stderr

    materialize = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")
    assert materialize.returncode == 0, materialize.stderr
    materialize_result = json.loads(materialize.stdout)
    destination = Path(str(materialize_result["destination"]))

    assert tree_digest(selected_root) == before

    return MilestoneRun(
        selected_root=selected_root,
        output_root=output_root,
        run_path=run_path,
        run_id=run_id,
        snapshot_id=snapshot_id,
        plan_id=plan_id,
        destination=destination,
        scan_result=scan_result,
        plan_result=plan_result,
        finalize_result=finalize_result,
        materialize_result=materialize_result,
        report_markdown=report.stdout,
        full_report_markdown=full_report.stdout,
        plan_report_markdown=plan_report.stdout,
        full_plan_report_markdown=full_plan_report.stdout,
        finalized_plan_report_markdown=finalized_plan_report.stdout,
        finalized_full_plan_report_markdown=finalized_full_plan_report.stdout,
    )


def test_milestone_workflow_matches_example_output_byte_for_byte(
    tmp_path: Path,
) -> None:
    run = _run_milestone_workflow(tmp_path)

    assert run.scan_result["status"] == "complete"
    assert run.scan_result["inventory_count"] == TARGET_INVENTORY_COUNT
    assert run.scan_result["skipped_count"] == TARGET_SKIPPED_COUNT
    assert run.scan_result["selected_backup_root"] == str(run.selected_root.resolve())

    assert run.plan_result["operation_count"] == TARGET_OPERATION_COUNT
    assert run.finalize_result["status"] == "finalized"
    assert run.materialize_result["status"] == "materialized"
    assert run.materialize_result["operation_count"] == TARGET_OPERATION_COUNT
    assert run.materialize_result["plan_id"] == run.plan_id

    _assert_matches_example_output(run.destination)


def test_materialized_consolidation_is_clean_user_facing_tree(tmp_path: Path) -> None:
    from filesystem_organizer import analysis_run, materialization

    run = _run_milestone_workflow(tmp_path)

    assert str(run.destination) == str(run.plan_result["intended_destination"])
    assert run.destination.parents[1] == run.output_root.resolve()

    assert not (run.destination / materialization.STAGING_DIRECTORY_NAME).exists()
    assert not any(run.destination.rglob("*.part"))
    assert not any(run.destination.rglob("*" + analysis_run.DATABASE_NAME))
    assert not any(run.destination.rglob("*.sqlite3"))


def test_approval_gating_is_enforced_through_cli(tmp_path: Path) -> None:
    selected_root = copy_prepared_source(tmp_path / "selected-backup-root")
    before = tree_digest(selected_root)
    output_root = tmp_path / "output"

    scan_result = _scan_fixture(tmp_path, selected_root, output_root)
    run_path = _run_path(scan_result)
    plan_id = _create_plan(tmp_path, run_path)

    draft = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")
    assert draft.returncode == 1
    assert "not finalized" in draft.stderr

    _finalize_plan(tmp_path, run_path, plan_id)

    headless = run_cli(tmp_path, "materialize", str(run_path), plan_id)
    assert headless.returncode == 1
    assert "requires --yes" in headless.stderr

    approved = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")
    assert approved.returncode == 0, approved.stderr
    assert tree_digest(selected_root) == before


def test_report_artifacts_prove_duplicate_evidence_and_skipped_findings(
    tmp_path: Path,
) -> None:
    run = _run_milestone_workflow(tmp_path)

    report = run.report_markdown
    assert "# Analysis Run Report" in report
    assert f"**Run ID:** `{run.run_id}`" in report
    assert f"**Snapshot ID:** `{run.snapshot_id}`" in report
    assert f"**Selected Backup Root:** `{run.selected_root}`" in report
    assert "**Readable regular files:** 33" in report
    assert "**Skipped entries:** 2" in report
    assert "## Exact Duplicate Groups" not in report

    report = run.full_report_markdown
    assert "## Exact Duplicate Groups" in report
    assert "BLAKE3-256 v1" in report
    duplicate_section = report.split("## Exact Duplicate Groups", maxsplit=1)[1].split(
        "## Directory Relationship Graph", maxsplit=1
    )[0]
    assert (
        "**Canonical Copy:** `snapshot-2023-11-01/Home/Projects/ai-assistant/docs/recovery-checklist.txt`" in duplicate_section
    )
    assert "**Reason:** newest modification timestamp" in duplicate_section
    assert "**Canonical Copy:** `snapshot-2025-07-01/Home/Desktop/annual_tax_statement_2024.pdf`" in (
        duplicate_section
    )
    assert duplicate_section.count("**Canonical Copy:**") == 2

    group_member_counts = []
    for chunk in duplicate_section.split("\n### `")[1:]:
        member_rows = [line for line in chunk.splitlines() if line.startswith("| `")]
        group_member_counts.append(len(member_rows))
    assert sorted(group_member_counts) == [2, 3]

    inventory_section = report.split("## Persisted Inventory Evidence", maxsplit=1)[1]
    assert (
        f"| `snapshot-2025-07-01/Home/Desktop/same-size-alpha.txt` | regular-file | "
        f"{TARGET_SAME_SIZE_NONDUPLICATE_BYTES} |" in inventory_section
    )
    assert (
        f"| `snapshot-2025-07-01/Home/Desktop/same-size-bravo.txt` | regular-file | "
        f"{TARGET_SAME_SIZE_NONDUPLICATE_BYTES} |" in inventory_section
    )

    skipped_section = report.split("## Skipped Entry Findings", maxsplit=1)[1]
    assert "| `snapshot-2025-07-01/Home/Desktop/unreadable.txt` | unreadable |" in skipped_section
    assert "| `snapshot-2025-07-01/Home/Desktop/link-to-annual-tax-statement` | symbolic-link |" in (
        skipped_section
    )


def test_plan_artifacts_prove_canonical_rationale_and_immutability(
    tmp_path: Path,
) -> None:
    run = _run_milestone_workflow(tmp_path)

    assert run.plan_result["run_id"] == run.run_id
    assert run.plan_result["snapshot_id"] == run.snapshot_id
    assert run.plan_result["schema_version"] == 9
    assert run.plan_result["status"] == "draft"
    assert run.plan_result["operation_count"] == TARGET_OPERATION_COUNT

    draft_report = run.plan_report_markdown
    assert "## Consolidation Plan Operations" not in draft_report

    draft_report = run.full_plan_report_markdown
    assert f"**Plan ID:** `{run.plan_id}`" in draft_report
    assert f"**Run ID:** `{run.run_id}`" in draft_report
    assert f"**Snapshot ID:** `{run.snapshot_id}`" in draft_report
    assert "**Schema Version:** 9" in draft_report
    assert "**Status:** draft" in draft_report
    assert f"**Operation Count:** {TARGET_OPERATION_COUNT}" in draft_report
    operations_section = draft_report.split(
        "## Consolidation Plan Operations", maxsplit=1
    )[1].split("## Plan Output Entries", maxsplit=1)[0]
    assert "## Plan Output Entries" in draft_report
    assert "Structural Union (strict-subset)" in operations_section
    assert "| `snapshot-2025-07-01/Home/Desktop/same-size-alpha.txt`" in operations_section
    assert "| `snapshot-2025-07-01/Home/Desktop/same-size-bravo.txt`" in operations_section
    assert "| `snapshot-2025-07-01/Home/Desktop/meeting-notes.txt`" in operations_section
    assert "| `snapshot-2025-07-01/Home/Projects/ai-assistant/docs/meeting-notes.txt`" in operations_section
    assert "| `snapshot-2025-07-01/Home/Desktop/empty-file.txt`" in operations_section
    assert "| `snapshot-2025-07-01/Home/Personal/Finance/2024/receipt_☕_berlin.pdf`" in operations_section
    assert "| `snapshot-2025-07-01/Home/Personal/Travel/2023-Århus/boarding-pass.pdf`" in operations_section
    assert "| `snapshot-2025-07-01/Home/Desktop/Invoice #1042 – Müller & Söhne.PDF`" in operations_section
    assert "| `snapshot-2025-07-01/.DS_Store`" in operations_section
    assert "| `snapshot-2025-07-01/System/Mac/._Vacation.jpg`" in operations_section
    assert "| `snapshot-2025-07-01/System/Windows/desktop.ini`" in operations_section
    assert "| `snapshot-2025-07-01/Home/Projects/ai-assistant/.env.example`" in operations_section
    assert "## Canonical Copy Overrides" not in draft_report

    expected_operation_paths = set(relative_files(OUTPUT_ROOT))
    assert "## Canonical Copy Overrides" not in run.finalized_full_plan_report_markdown
    finalized_operations_section = run.finalized_full_plan_report_markdown.split(
        "## Consolidation Plan Operations", maxsplit=1
    )[1].split("## Plan Output Entries", maxsplit=1)[0]
    assert finalized_operations_section == operations_section
    assert "**Status:** finalized" in run.finalized_full_plan_report_markdown

    retained_paths = {
        line.split("` | `", maxsplit=1)[1].split("` |", maxsplit=1)[0]
        for line in operations_section.splitlines()
        if line.startswith("| `")
    }
    assert retained_paths == expected_operation_paths

    for omitted in OMITTED_SOURCE_PATHS:
        assert omitted not in retained_paths


def test_fixture_coverage_manifested_in_materialized_tree(tmp_path: Path) -> None:
    run = _run_milestone_workflow(tmp_path)

    materialized = relative_files(run.destination)

    assert "snapshot-2025-07-01/Home/Projects/ai-assistant/docs/recovery-checklist.txt" in materialized
    assert "snapshot-2025-07-01/Home/Desktop/annual_tax_statement_2024.pdf" in materialized
    assert "snapshot-2025-07-01/Home/Desktop/same-size-alpha.txt" in materialized
    assert "snapshot-2025-07-01/Home/Desktop/same-size-bravo.txt" in materialized
    assert "snapshot-2025-07-01/Home/Desktop/meeting-notes.txt" in materialized
    assert "snapshot-2025-07-01/Home/Projects/ai-assistant/docs/meeting-notes.txt" in materialized
    assert materialized["snapshot-2025-07-01/Home/Desktop/empty-file.txt"] == b""
    assert "snapshot-2025-07-01/Home/Personal/Finance/2024/receipt_☕_berlin.pdf" in materialized
    assert "snapshot-2025-07-01/Home/Personal/Travel/2023-Århus/boarding-pass.pdf" in materialized
    assert "snapshot-2025-07-01/Home/Desktop/Invoice #1042 – Müller & Söhne.PDF" in materialized
    assert "snapshot-2025-07-01/.DS_Store" in materialized
    assert "snapshot-2025-07-01/System/Mac/._Vacation.jpg" in materialized
    assert "snapshot-2025-07-01/System/Windows/desktop.ini" in materialized
    assert "snapshot-2025-07-01/Home/Projects/ai-assistant/.env.example" in materialized

    for omitted in OMITTED_SOURCE_PATHS:
        assert omitted not in materialized


def _materialize_with_crash(
    tmp_path: Path, run_path: Path, plan_id: str, crash_point: str
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    environment["FSO_MATERIALIZATION_CRASH_POINT"] = crash_point
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "filesystem_organizer",
            "materialize",
            str(run_path),
            plan_id,
            "--yes",
        ],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def _completed_operation_indexes(run_path: Path, plan_id: str) -> list[int]:
    from filesystem_organizer import analysis_run

    database_path = run_path / analysis_run.DATABASE_NAME
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT operation_index FROM materialization_events "
            "WHERE plan_id = ? AND event = 'FILE_COMPLETED' ORDER BY operation_index",
            (plan_id,),
        ).fetchall()
    return [int(row[0]) for row in rows]


def _planned_file_indexes(run_path: Path, plan_id: str) -> list[int]:
    from filesystem_organizer import analysis_run

    database_path = run_path / analysis_run.DATABASE_NAME
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT entry_index FROM plan_output_entries "
            "WHERE plan_id = ? AND entry_kind = 'file' ORDER BY entry_index",
            (plan_id,),
        ).fetchall()
    return [int(row[0]) for row in rows]


def _journal_event_names(run_path: Path, plan_id: str) -> list[str]:
    from filesystem_organizer import analysis_run

    database_path = run_path / analysis_run.DATABASE_NAME
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT event FROM materialization_events WHERE plan_id = ? ORDER BY seq",
            (plan_id,),
        ).fetchall()
    return [str(row[0]) for row in rows]


@pytest.mark.parametrize("crash_point", MATERIALIZATION_CRASH_POINTS)
def test_interrupted_materialization_matches_uninterrupted_result(
    tmp_path: Path, crash_point: str
) -> None:
    selected_root = copy_prepared_source(tmp_path / "selected-backup-root")
    before = tree_digest(selected_root)
    output_root = tmp_path / "output"

    scan_result = _scan_fixture(tmp_path, selected_root, output_root)
    run_path = _run_path(scan_result)
    plan_id = _create_plan(tmp_path, run_path)
    _finalize_plan(tmp_path, run_path, plan_id)

    crashed = _materialize_with_crash(tmp_path, run_path, plan_id, crash_point)
    assert crashed.returncode != 0

    resumed = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")
    assert resumed.returncode == 0, resumed.stderr
    result = json.loads(resumed.stdout)
    assert result["status"] == "materialized"
    destination = Path(str(result["destination"]))

    _assert_matches_example_output(destination)

    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        completed = [
            int(row[0])
            for row in connection.execute(
                "SELECT entry_index FROM execution_file_evidence WHERE plan_id = ? "
                "ORDER BY entry_index",
                (plan_id,),
            )
        ]
    assert completed == _planned_file_indexes(run_path, plan_id)

    assert tree_digest(selected_root) == before


def test_unreadable_fixture_condition_is_established_and_recorded(
    tmp_path: Path,
) -> None:
    selected_root = copy_prepared_source(tmp_path / "selected-backup-root")
    before = tree_digest(selected_root)
    unreadable_path = (
        selected_root / "snapshot-2025-07-01" / "Home" / "Desktop" / "unreadable.txt"
    )

    try:
        unreadable_path.open("rb").read(1)
    except OSError:
        pass
    else:
        pytest.fail(
            "environment cannot establish the unreadable fixture condition: "
            f"{unreadable_path} is readable (running as root?); refusing to continue"
        )

    output_root = tmp_path / "output"
    scan_result = _scan_fixture(tmp_path, selected_root, output_root)
    run_path = _run_path(scan_result)

    report = run_cli(tmp_path, "report", str(run_path))
    assert report.returncode == 0, report.stderr
    assert "| `snapshot-2025-07-01/Home/Desktop/unreadable.txt` | unreadable |" in report.stdout

    plan_id = _create_plan(tmp_path, run_path)
    _finalize_plan(tmp_path, run_path, plan_id)
    materialize = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")
    assert materialize.returncode == 0, materialize.stderr
    destination = Path(str(json.loads(materialize.stdout)["destination"]))

    assert "snapshot-2025-07-01/Home/Desktop/unreadable.txt" not in relative_files(destination)
    assert tree_digest(selected_root) == before
