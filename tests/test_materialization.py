from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
import types
from pathlib import Path

import pytest

from tests.test_cli import run_cli, tree_digest


def _scan_plan_finalize(tmp_path: Path) -> tuple[Path, str, Path, Path]:
    """Scan the example fixture, draft a plan, and finalize it.

    Returns (run_path, plan_id, output_root, selected_root).
    """
    from tests.example_fixture import copy_prepared_source

    selected_root = copy_prepared_source(tmp_path / "selected-backup-root")
    output_root = tmp_path / "output"

    scan = run_cli(
        tmp_path, "scan", str(selected_root), "--output-root", str(output_root)
    )
    assert scan.returncode == 0, scan.stderr
    scan_result = json.loads(scan.stdout)
    run_path = Path(scan_result["analysis_run"])

    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_id = str(json.loads(plan.stdout)["plan_id"])

    layout_path = tmp_path / "materialization-layout.json"
    exported = run_cli(
        tmp_path,
        "plan-layout-export",
        str(run_path),
        plan_id,
        "--output",
        str(layout_path),
    )
    assert exported.returncode == 0, exported.stderr
    layout = json.loads(layout_path.read_text())
    layout["skipped_actions"] = [
        {"path": path, "action": "retain"}
        for path in (
            "snapshot-2025-07-01/Home/Desktop/link-to-annual-tax-statement",
            "snapshot-2025-07-01/Home/Desktop/unreadable.txt",
        )
    ]
    layout_path.write_text(json.dumps(layout))
    applied = run_cli(
        tmp_path, "plan-layout-apply", str(run_path), plan_id, str(layout_path)
    )
    assert applied.returncode == 0, applied.stderr

    finalize = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)
    assert finalize.returncode == 0, finalize.stderr

    return run_path, plan_id, output_root, selected_root


def _scan_simple_plan_finalize(tmp_path: Path) -> tuple[Path, str, Path, Path]:
    """Create a non-structural plan for operation-level materialization tests."""
    selected_root = tmp_path / "selected-backup-root"
    control = selected_root / "Control"
    control.mkdir(parents=True)
    (control / "a-first.txt").write_bytes(b"published before later operations")
    (control / "meeting-notes.txt").write_bytes(b"meeting notes")
    output_root = tmp_path / "output"

    scan = run_cli(
        tmp_path, "scan", str(selected_root), "--output-root", str(output_root)
    )
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])

    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_id = str(json.loads(plan.stdout)["plan_id"])

    finalize = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)
    assert finalize.returncode == 0, finalize.stderr
    return run_path, plan_id, output_root, selected_root


def _structural_plan(tmp_path: Path) -> tuple[Path, str, Path]:
    selected_root = tmp_path / "backup"
    for name in ("first", "second"):
        directory = selected_root / name
        directory.mkdir(parents=True)
        (directory / "same.txt").write_bytes(b"same structural content")
        (directory / "empty").mkdir()
        for region in ("shared-region-one", "shared-region-two"):
            (directory / region).mkdir()
            (directory / region / "anchor.txt").write_text(region)
    link = selected_root / "first" / "unverified-link"
    link.symlink_to("same.txt")
    (selected_root / "second" / "unverified-link").symlink_to("same.txt")
    output_root = tmp_path / "output"
    scan = run_cli(
        tmp_path, "scan", str(selected_root), "--output-root", str(output_root)
    )
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_id = str(json.loads(plan.stdout)["plan_id"])
    finalized = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)
    assert finalized.returncode == 0, finalized.stderr
    return run_path, plan_id, selected_root


@pytest.mark.parametrize(
    "mutation",
    [
        "added",
        "removed",
        "same_size_content_changed",
        "type_changed",
        "empty_directory_changed",
        "newly_unprocessable",
        "formerly_unprocessable_changed",
        "directory_newly_unprocessable",
    ],
)
def test_structural_snapshot_revalidation_refuses_live_evidence_drift_before_destination_creation(
    tmp_path: Path, mutation: str
) -> None:
    run_path, plan_id, selected_root = _structural_plan(tmp_path)
    first = selected_root / "first"
    restore_permissions: tuple[Path, int] | None = None
    if mutation == "added":
        (first / "added.txt").write_bytes(b"new")
    elif mutation == "removed":
        (first / "same.txt").unlink()
    elif mutation == "same_size_content_changed":
        (first / "same.txt").write_bytes(b"X" * len(b"same structural content"))
    elif mutation == "type_changed":
        (first / "same.txt").unlink()
        (first / "same.txt").mkdir()
    elif mutation == "empty_directory_changed":
        (first / "empty" / "new.txt").write_bytes(b"not empty")
    elif mutation == "newly_unprocessable":
        (first / "same.txt").unlink()
        (first / "same.txt").symlink_to("missing-target")
    elif mutation == "formerly_unprocessable_changed":
        (first / "unverified-link").unlink()
        (first / "unverified-link").symlink_to("different-target")
    else:
        unreadable = first / "empty"
        restore_permissions = (unreadable, unreadable.stat().st_mode)
        unreadable.chmod(0)

    destination = tmp_path / "must-not-exist"
    try:
        materialize = run_cli(
            tmp_path,
            "materialize",
            str(run_path),
            plan_id,
            "--destination",
            str(destination),
            "--yes",
        )
    finally:
        if restore_permissions is not None:
            path, mode = restore_permissions
            path.chmod(mode)

    assert materialize.returncode == 1
    assert "Structural Snapshot Revalidation failed" in materialize.stderr
    assert not destination.exists()


def test_materialization_revalidates_structural_evidence_once_after_plan_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, selected_root = _structural_plan(tmp_path)
    destination = tmp_path / "must-not-exist"
    original = materialization._revalidate_structural_snapshot
    calls = 0

    def mutate_before_post_approval_check(*args: object) -> set[str]:
        nonlocal calls
        calls += 1
        (selected_root / "first" / "same.txt").write_bytes(
            b"X" * len(b"same structural content")
        )
        return original(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(
        materialization,
        "_revalidate_structural_snapshot",
        mutate_before_post_approval_check,
    )
    with pytest.raises(
        materialization.MaterializationError,
        match="Structural Snapshot Revalidation failed",
    ):
        materialization.materialize_consolidation_plan(run_path, plan_id, destination)

    assert calls == 1
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        events = connection.execute(
            "SELECT event FROM materialization_events WHERE plan_id = ? ORDER BY seq",
            (plan_id,),
        ).fetchall()
    assert events == []
    assert not destination.exists()


def test_interrupted_normal_materialization_preserves_owned_partial_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, _selected_root = _structural_plan(tmp_path)
    original = materialization._revalidate_structural_snapshot
    calls = 0

    def count_structural_revalidations(*args: object) -> set[str]:
        nonlocal calls
        calls += 1
        return original(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(
        materialization,
        "_revalidate_structural_snapshot",
        count_structural_revalidations,
    )
    monkeypatch.setenv(materialization.CRASH_POINT_ENVIRONMENT, "postwritten")
    with pytest.raises(materialization._CrashSimulation):
        materialization.materialize_consolidation_plan(run_path, plan_id)
    monkeypatch.delenv(materialization.CRASH_POINT_ENVIRONMENT)

    resumed = materialization.materialize_consolidation_plan(run_path, plan_id)

    assert resumed["status"] == "materialized"
    assert calls == 2


def test_resumed_materialization_refuses_structural_drift(tmp_path: Path) -> None:
    run_path, plan_id, selected_root = _structural_plan(tmp_path)

    crashed = _materialize_cli(tmp_path, run_path, plan_id, crash_point="admission")
    assert crashed.returncode != 0
    (selected_root / "first" / "same.txt").write_bytes(b"changed structural content")

    resumed = _materialize_cli(tmp_path, run_path, plan_id)

    assert resumed.returncode == 1
    assert "Structural Snapshot Revalidation failed" in resumed.stderr


def test_materialization_journal_indexes_operation_event_lookup(tmp_path: Path) -> None:
    run_path, plan_id, _output_root, _selected_root = _scan_plan_finalize(tmp_path)

    materialize = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")

    assert materialize.returncode == 0, materialize.stderr
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        index_columns = {
            tuple(row[2] for row in connection.execute(f"PRAGMA index_info({name})"))
            for _seq, name, _unique, _origin, _partial in connection.execute(
                "PRAGMA index_list(materialization_events)"
            )
        }
    assert ("plan_id", "operation_index", "seq") in index_columns


def test_preflight_counts_multi_root_identity_component_as_one_structural_union(
    tmp_path: Path,
) -> None:
    from filesystem_organizer import materialization

    selected_root = tmp_path / "backup"
    for name in ("alpha", "beta", "gamma", "delta"):
        directory = selected_root / name
        directory.mkdir(parents=True)
        (directory / "same.txt").write_bytes(b"same")
        for region in ("shared-region-one", "shared-region-two"):
            (directory / region).mkdir()
            (directory / region / "anchor.txt").write_text(region)

    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_id = str(json.loads(plan.stdout)["plan_id"])
    finalized = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)
    assert finalized.returncode == 0, finalized.stderr

    preflight = materialization.preflight_materialization(run_path, plan_id)

    assert preflight.structural_union_count == 1


def test_materialize_rejects_draft_plan(tmp_path: Path) -> None:
    from tests.example_fixture import copy_prepared_source

    selected_root = copy_prepared_source(tmp_path / "selected-backup-root")
    output_root = tmp_path / "output"

    scan = run_cli(
        tmp_path, "scan", str(selected_root), "--output-root", str(output_root)
    )
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])

    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_id = str(json.loads(plan.stdout)["plan_id"])

    materialize = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")

    assert materialize.returncode == 1
    assert "not finalized" in materialize.stderr


def test_materialize_refuses_an_active_run_workspace_lock(tmp_path: Path) -> None:
    from filesystem_organizer import materialization
    from filesystem_organizer.linux_filesystem import nonblocking_lock

    run_path, plan_id, _output_root, _selected_root = _scan_simple_plan_finalize(tmp_path)

    with nonblocking_lock(run_path / ".filesystem-organizer.lock"), pytest.raises(
        materialization.MaterializationError, match="already holds"
    ):
        materialization.materialize_consolidation_plan(run_path, plan_id)


def test_normal_partial_destination_is_sibling_of_final_path(tmp_path: Path) -> None:
    from filesystem_organizer import materialization

    destination = tmp_path / "materialized" / "result"

    assert materialization._partial_destination(destination, "plan-1") == (
        destination.parent / ".result.partial-plan-1"
    )


def test_materialize_refuses_an_active_destination_lock(tmp_path: Path) -> None:
    from filesystem_organizer import materialization
    from filesystem_organizer.linux_filesystem import (
        destination_lock_path,
        nonblocking_lock,
    )

    run_path, plan_id, _output_root, _selected_root = _scan_simple_plan_finalize(tmp_path)
    preflight = materialization.preflight_materialization_for_execution(run_path, plan_id)

    with nonblocking_lock(destination_lock_path(Path(preflight.destination))), pytest.raises(
        materialization.MaterializationError, match="already holds"
    ):
        materialization.materialize_consolidation_plan(run_path, plan_id)


def test_plan_finalization_refuses_an_active_run_workspace_lock(tmp_path: Path) -> None:
    from filesystem_organizer.consolidation_plan import (
        ConsolidationPlanError,
        finalize_consolidation_plan,
    )
    from filesystem_organizer.linux_filesystem import nonblocking_lock

    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "one.txt").write_text("one")
    scan = run_cli(tmp_path, "scan", str(selected_root))
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    plan_id = str(json.loads(plan.stdout)["plan_id"])

    with nonblocking_lock(run_path / ".filesystem-organizer.lock"), pytest.raises(
        ConsolidationPlanError, match="already holds"
    ):
        finalize_consolidation_plan(run_path, plan_id)


def test_materialize_finalizes_draft_with_exact_revision(tmp_path: Path) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "one.txt").write_text("one")
    scan = run_cli(tmp_path, "scan", str(selected_root))
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    plan_id = str(json.loads(plan.stdout)["plan_id"])

    materialized = run_cli(
        tmp_path, "materialize", str(run_path), plan_id, "--revision", "0", "--yes"
    )

    assert materialized.returncode == 0, materialized.stderr
    assert json.loads(materialized.stdout)["status"] == "materialized"


def test_materialize_refuses_stale_draft_revision(tmp_path: Path) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "one.txt").write_text("one")
    scan = run_cli(tmp_path, "scan", str(selected_root))
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    plan_id = str(json.loads(plan.stdout)["plan_id"])

    materialized = run_cli(
        tmp_path, "materialize", str(run_path), plan_id, "--revision", "1", "--yes"
    )

    assert materialized.returncode == 1
    assert "Layout Revision" in materialized.stderr


def test_materialize_requires_yes_or_tty_for_headless_approval(tmp_path: Path) -> None:
    run_path, plan_id, _output_root, _selected_root = _scan_plan_finalize(tmp_path)

    materialize = run_cli(tmp_path, "materialize", str(run_path), plan_id)

    assert materialize.returncode == 1
    assert "requires --yes" in materialize.stderr


@pytest.mark.parametrize("occupied", [False, True])
def test_materialize_rejects_existing_destination(
    tmp_path: Path, occupied: bool
) -> None:
    run_path, plan_id, _output_root, _selected_root = _scan_plan_finalize(tmp_path)

    destination = tmp_path / "destination"
    destination.mkdir()
    if occupied:
        (destination / "stray.txt").write_bytes(b"already here")

    materialize = run_cli(
        tmp_path,
        "materialize",
        str(run_path),
        plan_id,
        "--destination",
        str(destination),
        "--yes",
    )

    assert materialize.returncode == 1
    assert "already exists" in materialize.stderr


def test_materialize_rejects_destination_inside_selected_backup_root(
    tmp_path: Path,
) -> None:
    run_path, plan_id, _output_root, selected_root = _scan_plan_finalize(tmp_path)

    unsafe_destination = selected_root / "materialized-here"

    materialize = run_cli(
        tmp_path,
        "materialize",
        str(run_path),
        plan_id,
        "--destination",
        str(unsafe_destination),
        "--yes",
    )

    assert materialize.returncode == 1
    assert "outside the Selected Backup Root" in materialize.stderr
    assert not unsafe_destination.exists()


def test_preflight_allows_cross_filesystem_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, _output_root, _selected_root = _scan_plan_finalize(tmp_path)
    monkeypatch.setattr(
        materialization, "_source_same_filesystem", lambda *_args: False
    )

    assert materialization.preflight_materialization(run_path, plan_id).plan_id == plan_id


def test_materialize_rejects_insufficient_free_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, _output_root, _selected_root = _scan_plan_finalize(tmp_path)

    monkeypatch.setattr(
        "shutil.disk_usage",
        lambda path: types.SimpleNamespace(total=0, used=0, free=0),
    )

    with pytest.raises(
        materialization.MaterializationError, match="Insufficient free space"
    ):
        materialization.materialize_consolidation_plan(run_path, plan_id)


def test_materialize_rejects_changed_source_before_publication(tmp_path: Path) -> None:
    run_path, plan_id, _output_root, selected_root = _scan_simple_plan_finalize(
        tmp_path
    )

    changed_file = selected_root / "Control" / "meeting-notes.txt"
    original_bytes = changed_file.read_bytes()
    changed_file.write_bytes(b"x" * len(original_bytes))

    materialize = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")

    assert materialize.returncode == 1
    assert "metadata" in materialize.stderr


def test_materialize_rejects_unsupported_content_identity_algorithm(
    tmp_path: Path,
) -> None:
    import sqlite3

    from filesystem_organizer import analysis_run

    run_path, plan_id, _output_root, _selected_root = _scan_plan_finalize(tmp_path)
    database_path = run_path / analysis_run.DATABASE_NAME
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE plan_operations SET algorithm = 'MD5' WHERE plan_id = ?",
            (plan_id,),
        )
        connection.commit()

    materialize = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")

    assert materialize.returncode == 1
    assert "unsupported content-identity algorithm" in materialize.stderr


def test_materialize_rejects_destination_that_is_a_regular_file(
    tmp_path: Path,
) -> None:
    run_path, plan_id, _output_root, _selected_root = _scan_plan_finalize(tmp_path)

    destination = tmp_path / "not-a-directory"
    destination.write_bytes(b"occupied")

    materialize = run_cli(
        tmp_path,
        "materialize",
        str(run_path),
        plan_id,
        "--destination",
        str(destination),
        "--yes",
    )

    assert materialize.returncode == 1
    assert "destination already exists" in materialize.stderr
    assert destination.read_bytes() == b"occupied"


def test_materialize_happy_path_custom_destination(tmp_path: Path) -> None:
    run_path, plan_id, _output_root, selected_root = _scan_plan_finalize(tmp_path)
    before = tree_digest(selected_root)
    custom_destination = tmp_path / "custom-materialized"

    materialize = run_cli(
        tmp_path,
        "materialize",
        str(run_path),
        plan_id,
        "--destination",
        str(custom_destination),
        "--yes",
    )
    assert materialize.returncode == 0, materialize.stderr
    result = json.loads(materialize.stdout)
    assert "Staging Materialized Consolidation:" in materialize.stderr
    assert result["status"] == "materialized"
    assert result["operation_count"] == 30
    assert Path(result["destination"]) == custom_destination.resolve()

    assert not (custom_destination / materialization_staging_name()).exists()
    for relative_path in _operation_output_paths(run_path, plan_id):
        published = custom_destination / relative_path
        source = selected_root / relative_path
        assert published.is_file(), f"missing published output: {relative_path}"
        assert published.read_bytes() == source.read_bytes()

    assert tree_digest(selected_root) == before


def test_materialize_happy_path_black_box_cli(tmp_path: Path) -> None:
    run_path, plan_id, _output_root, selected_root = _scan_plan_finalize(tmp_path)
    before = tree_digest(selected_root)

    plan_report = run_cli(tmp_path, "plan-report", str(run_path), "--plan-id", plan_id)
    assert plan_report.returncode == 0, plan_report.stderr

    materialize = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")
    assert materialize.returncode == 0, materialize.stderr
    result = json.loads(materialize.stdout)
    assert result["plan_id"] == plan_id
    assert result["status"] == "materialized"
    assert result["operation_count"] == 30

    destination = Path(result["destination"])
    assert destination.is_dir()
    assert not (destination / materialization_staging_name()).exists()

    for relative_path in _operation_output_paths(run_path, plan_id):
        published = destination / relative_path
        source = selected_root / relative_path
        assert published.is_file(), f"missing published output: {relative_path}"
        assert published.read_bytes() == source.read_bytes()

    assert tree_digest(selected_root) == before

    repeat = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")
    assert repeat.returncode == 1
    assert "destination already exists" in repeat.stderr
    assert not (destination / materialization_staging_name()).exists()
    for relative_path in _operation_output_paths(run_path, plan_id):
        assert (destination / relative_path).is_file()


def test_materialize_in_place_applies_finalized_projection_after_explicit_acknowledgement(
    tmp_path: Path,
) -> None:
    run_path, plan_id, _output_root, selected_root = _scan_plan_finalize(tmp_path)

    refused = run_cli(
        tmp_path, "materialize", str(run_path), plan_id, "--in-place", "--yes"
    )
    assert refused.returncode == 1
    assert "destructive acknowledgement" in refused.stderr

    materialize = run_cli(
        tmp_path,
        "materialize",
        str(run_path),
        plan_id,
        "--in-place",
        "--yes",
        "--acknowledge-destructive",
    )

    assert materialize.returncode == 0, materialize.stderr
    result = json.loads(materialize.stdout)
    assert "Preparing in-place materialization..." in materialize.stderr
    assert "Protecting originals:" in materialize.stderr
    assert "Publishing files:" in materialize.stderr
    assert result["status"] == "materialized-in-place"
    assert Path(result["destination"]) == selected_root
    assert not any(selected_root.glob(".filesystem-organizer-staging-*"))
    for relative_path in _operation_output_paths(run_path, plan_id):
        assert (selected_root / relative_path).is_file(), relative_path

    repeated = run_cli(
        tmp_path,
        "materialize",
        str(run_path),
        plan_id,
        "--in-place",
        "--yes",
        "--acknowledge-destructive",
    )
    assert repeated.returncode == 0, repeated.stderr
    assert json.loads(repeated.stdout)["status"] == "materialized-in-place"


def test_in_place_materializes_a_relocated_structural_union_without_self_induced_drift(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    for name in ("snapshot-2023", "snapshot-2025"):
        directory = selected_root / name / "Home" / "Desktop"
        directory.mkdir(parents=True)
        (directory / "same.txt").write_bytes(b"same structural content")
    (selected_root / "snapshot-2025" / "Home" / "Desktop" / "newer.txt").write_bytes(
        b"only in newer snapshot"
    )
    (
        selected_root / "snapshot-2025" / "Home" / "Desktop" / "unverified-link"
    ).symlink_to("same.txt")
    output_root = tmp_path / "output"

    scan = run_cli(
        tmp_path, "scan", str(selected_root), "--output-root", str(output_root)
    )
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_id = str(json.loads(plan.stdout)["plan_id"])
    layout_path = tmp_path / "layout.json"
    exported = run_cli(
        tmp_path,
        "plan-layout-export",
        str(run_path),
        plan_id,
        "--output",
        str(layout_path),
    )
    assert exported.returncode == 0, exported.stderr
    layout = json.loads(layout_path.read_text())
    layout["rules"] = [
        {"selector": {"subtree": "snapshot-2025"}, "action": {"exclude": True}},
        {
            "selector": {"subtree": "snapshot-2025/Home"},
            "action": {"place_under": "Home"},
        },
    ]
    layout["skipped_actions"] = [
        {
            "path": "snapshot-2025/Home/Desktop/unverified-link",
            "action": "exclude",
        }
    ]
    layout_path.write_text(json.dumps(layout))
    applied = run_cli(
        tmp_path,
        "plan-layout-apply",
        "--acknowledge-exclusions",
        str(run_path),
        plan_id,
        str(layout_path),
    )
    assert applied.returncode == 0, applied.stderr
    finalized = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)
    assert finalized.returncode == 0, finalized.stderr

    materialize = run_cli(
        tmp_path,
        "materialize",
        str(run_path),
        plan_id,
        "--in-place",
        "--yes",
        "--acknowledge-destructive",
    )

    assert materialize.returncode == 0, materialize.stderr
    assert json.loads(materialize.stdout)["status"] == "materialized-in-place"


def test_in_place_preflight_refuses_an_unowned_staging_directory(
    tmp_path: Path,
) -> None:
    run_path, plan_id, _output_root, selected_root = _scan_plan_finalize(tmp_path)
    (selected_root / f".filesystem-organizer-staging-{plan_id}").mkdir()

    preflight = run_cli(
        tmp_path, "materialize-preflight", str(run_path), plan_id, "--in-place"
    )

    assert preflight.returncode == 1
    assert "unexpected occupant" in preflight.stderr


def test_in_place_resumes_after_durable_protection_before_destructive_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, _output_root, selected_root = _scan_plan_finalize(tmp_path)
    monkeypatch.setenv(materialization.CRASH_POINT_ENVIRONMENT, "postprotect")

    with pytest.raises(materialization._CrashSimulation):
        materialization.materialize_consolidation_plan(run_path, plan_id, in_place=True)

    monkeypatch.delenv(materialization.CRASH_POINT_ENVIRONMENT)
    resumed = materialization.materialize_consolidation_plan(
        run_path, plan_id, in_place=True
    )

    assert resumed["status"] == "materialized-in-place"
    assert not any(selected_root.glob(".filesystem-organizer-staging-*"))
    assert sorted(
        int(index)
        for event, index, _detail in _journal_events(run_path, plan_id)
        if event == "FILE_COMPLETED" and index is not None
    ) == _operation_indexes(run_path, plan_id)


def test_materialize_preserves_source_mode_and_modification_time(
    tmp_path: Path,
) -> None:
    run_path, plan_id, _output_root, selected_root = _scan_simple_plan_finalize(
        tmp_path
    )
    source = selected_root / "Control" / "meeting-notes.txt"
    source.chmod(0o640)
    expected_mtime_ns = source.stat().st_mtime_ns

    materialize = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")

    assert materialize.returncode == 0, materialize.stderr
    destination = Path(json.loads(materialize.stdout)["destination"])
    published = destination / "Control" / "meeting-notes.txt"
    assert stat.S_IMODE(published.stat().st_mode) == 0o640
    assert published.stat().st_mtime_ns == expected_mtime_ns


def test_materialize_records_one_compact_outcome_per_file(tmp_path: Path) -> None:
    run_path, plan_id, _output_root, _selected_root = _scan_plan_finalize(tmp_path)

    materialize = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")

    assert materialize.returncode == 0, materialize.stderr
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        evidence_indexes = [
            int(row[0])
            for row in connection.execute(
                "SELECT entry_index FROM execution_file_evidence WHERE plan_id = ? "
                "ORDER BY entry_index",
                (plan_id,),
            )
        ]
    assert evidence_indexes == _operation_indexes(run_path, plan_id)
    assert _journal_events(run_path, plan_id) == []


def test_materialize_persists_a_source_preserving_execution_manifest(
    tmp_path: Path,
) -> None:
    run_path, plan_id, _output_root, _selected_root = _scan_plan_finalize(tmp_path)

    materialize = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")

    assert materialize.returncode == 0, materialize.stderr
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        manifest = connection.execute(
            "SELECT manifest_version, mode, run_id, canonical_destination "
            "FROM execution_manifests WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        entry_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM execution_manifest_entries AS entry "
                "JOIN execution_manifests AS manifest USING (attempt_id) "
                "WHERE manifest.plan_id = ?",
                (plan_id,),
            ).fetchone()[0]
        )
    assert manifest is not None
    assert manifest[0] == 3
    assert manifest[1] == "source-preserving"
    assert entry_count > 0


def test_normal_materialization_records_native_clone_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization
    from filesystem_organizer.linux_filesystem import CloneResult

    run_path, plan_id, _output_root, _selected_root = _scan_simple_plan_finalize(
        tmp_path
    )

    def clone(source: Path, destination: Path) -> CloneResult:
        os.link(source, destination)
        return CloneResult.CLONED

    monkeypatch.setattr(materialization, "try_native_clone", clone)
    result = materialization.materialize_consolidation_plan(run_path, plan_id)

    assert result["status"] == "materialized"
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        evidence = connection.execute(
            "SELECT transfer_kind, algorithm, digest FROM execution_file_evidence "
            "WHERE plan_id = ?",
            (plan_id,),
        ).fetchall()
        attempt = connection.execute(
            "SELECT cloned_file_count, streamed_file_count FROM materialization_attempts "
            "WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    assert evidence and all(row == ("native-clone", None, None) for row in evidence)
    assert attempt == (len(evidence), 0)


def test_materialize_interactive_confirmation_approved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from filesystem_organizer.__main__ import main

    run_path, plan_id, _output_root, _selected_root = _scan_plan_finalize(tmp_path)

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")

    exit_code = main(["materialize", str(run_path), plan_id])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert f"Plan ID: {plan_id}" in captured.out
    assert "Operation count:" in captured.out
    assert "Total bytes:" in captured.out
    assert "Free space at destination:" in captured.out
    result = json.loads(captured.out.splitlines()[-1])
    assert result["status"] == "materialized"


def test_materialize_interactive_confirmation_declined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from filesystem_organizer.__main__ import main

    run_path, plan_id, output_root, _selected_root = _scan_plan_finalize(tmp_path)

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "no")

    exit_code = main(["materialize", str(run_path), plan_id])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "not approved" in captured.err
    assert not (output_root / "materialized").exists()


def materialization_staging_name() -> str:
    from filesystem_organizer.materialization import STAGING_DIRECTORY_NAME

    return STAGING_DIRECTORY_NAME


def _operation_output_paths(run_path: Path, plan_id: str) -> list[str]:
    import sqlite3

    from filesystem_organizer import analysis_run

    database_path = run_path / analysis_run.DATABASE_NAME
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT output_relative_path FROM final_plan_entries WHERE plan_id = ? "
            "AND entry_kind = 'file' ORDER BY entry_index",
            (plan_id,),
        ).fetchall()
    return [str(row[0]) for row in rows]


def _materialize_cli(
    tmp_path: Path,
    run_path: Path,
    plan_id: str,
    *,
    crash_point: str | None = None,
    destination: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    arguments = ["materialize", str(run_path), plan_id]
    if destination is not None:
        arguments += ["--destination", str(destination)]
    arguments += ["--yes"]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    if crash_point is not None:
        environment["FSO_MATERIALIZATION_CRASH_POINT"] = crash_point
    return subprocess.run(
        [sys.executable, "-m", "filesystem_organizer", *arguments],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def _journal_events(
    run_path: Path, plan_id: str
) -> list[tuple[str, int | None, str | None]]:
    from filesystem_organizer import analysis_run

    database_path = run_path / analysis_run.DATABASE_NAME
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT event, operation_index, detail FROM materialization_events "
            "WHERE plan_id = ? ORDER BY seq",
            (plan_id,),
        ).fetchall()
    return [(str(row[0]), row[1], row[2]) for row in rows]


def _operation_indexes(run_path: Path, plan_id: str) -> list[int]:
    from filesystem_organizer import analysis_run

    database_path = run_path / analysis_run.DATABASE_NAME
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT entry_index FROM final_plan_entries WHERE plan_id = ? "
            "AND entry_kind = 'file' ORDER BY entry_index",
            (plan_id,),
        ).fetchall()
    return [int(row[0]) for row in rows]


def _intended_destination(run_path: Path, plan_id: str) -> str:
    from filesystem_organizer import analysis_run

    database_path = run_path / analysis_run.DATABASE_NAME
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT intended_destination FROM consolidation_plans WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    return str(row[0])


def _reconciliation_details(run_path: Path, plan_id: str) -> list[str]:
    return [
        str(detail)
        for event, _index, detail in _journal_events(run_path, plan_id)
        if event == "RECONCILIATION" and detail is not None
    ]


def _finish_and_assert(
    tmp_path: Path,
    run_path: Path,
    plan_id: str,
    selected_root: Path,
) -> Path:
    """Resume an interrupted materialization and assert idempotent completion."""
    resume = _materialize_cli(tmp_path, run_path, plan_id)
    assert resume.returncode == 0, resume.stderr
    result = json.loads(resume.stdout)
    assert result["status"] == "materialized"
    destination = Path(result["destination"])
    assert not (destination / materialization_staging_name()).exists()
    for relative_path in _operation_output_paths(run_path, plan_id):
        published = destination / relative_path
        source = selected_root / relative_path
        assert published.is_file(), f"missing published output: {relative_path}"
        assert published.read_bytes() == source.read_bytes()
    events = _journal_events(run_path, plan_id)
    assert any(event == "MATERIALIZATION_COMPLETE" for event, _i, _d in events)
    completed_indexes = sorted(
        int(index)
        for event, index, _d in events
        if event == "FILE_COMPLETED" and index is not None
    )
    assert completed_indexes == sorted(_operation_indexes(run_path, plan_id))
    return destination


def test_materialize_crash_at_admission_resumes(tmp_path: Path) -> None:
    run_path, plan_id, _output_root, selected_root = _scan_plan_finalize(tmp_path)
    before = tree_digest(selected_root)

    crashed = _materialize_cli(tmp_path, run_path, plan_id, crash_point="admission")
    assert crashed.returncode != 0
    destination = Path(_intended_destination(run_path, plan_id))
    assert not destination.exists()
    resumed = _materialize_cli(tmp_path, run_path, plan_id)
    assert resumed.returncode == 0, resumed.stderr
    assert tree_digest(selected_root) == before


def test_materialize_crash_during_copy_preserves_an_unpublished_partial_tree(
    tmp_path: Path,
) -> None:
    run_path, plan_id, _output_root, _selected_root = _scan_plan_finalize(tmp_path)

    crashed = _materialize_cli(tmp_path, run_path, plan_id, crash_point="copying")
    assert crashed.returncode != 0

    destination = Path(_intended_destination(run_path, plan_id))
    partial = destination.parent / f".{destination.name}.partial-{plan_id}"
    assert partial.exists()
    assert not destination.exists()
    resumed = _materialize_cli(tmp_path, run_path, plan_id)
    assert resumed.returncode == 0, resumed.stderr


def test_materialize_crash_after_staging_written_never_publishes_a_partial_tree(
    tmp_path: Path,
) -> None:
    run_path, plan_id, _output_root, _selected_root = _scan_plan_finalize(tmp_path)

    crashed = _materialize_cli(tmp_path, run_path, plan_id, crash_point="postwritten")
    assert crashed.returncode != 0

    destination = Path(_intended_destination(run_path, plan_id))
    partial = destination.parent / f".{destination.name}.partial-{plan_id}"
    assert partial.exists()
    assert not destination.exists()


def test_materialize_completion_records_compact_outcomes(
    tmp_path: Path,
) -> None:
    run_path, plan_id, _output_root, _selected_root = _scan_plan_finalize(tmp_path)

    materialized = _materialize_cli(tmp_path, run_path, plan_id)
    assert materialized.returncode == 0, materialized.stderr
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        attempt = connection.execute(
            "SELECT state, planned_file_count, cloned_file_count, streamed_file_count "
            "FROM materialization_attempts WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    assert attempt is not None
    assert attempt[0] == "COMPLETE"
    assert int(attempt[1]) == int(attempt[2]) + int(attempt[3])


def _crash_after_atomic_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, str, Path, Path]:
    """Leave a STAGING_DURABLE Attempt after rename and before PUBLISHED."""
    from filesystem_organizer import materialization
    from filesystem_organizer.linux_filesystem import publish_directory_no_replace

    run_path, plan_id, _output_root, selected_root = _scan_simple_plan_finalize(tmp_path)
    destination = tmp_path / "published-result"
    class InterruptedPublication(BaseException):
        pass

    def publish_then_interrupt(staging: Path, final: Path) -> None:
        publish_directory_no_replace(staging, final)
        raise InterruptedPublication

    with monkeypatch.context() as patch:
        patch.setattr(
            materialization, "publish_directory_no_replace", publish_then_interrupt
        )
        with pytest.raises(InterruptedPublication):
            materialization.materialize_consolidation_plan(
                run_path, plan_id, destination
            )
    return run_path, plan_id, selected_root, destination


def test_materialize_reconciles_published_staging_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, selected_root, destination = _crash_after_atomic_publication(
        tmp_path, monkeypatch
    )
    source_before = tree_digest(selected_root)
    identity = destination.stat()

    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        assert connection.execute(
            "SELECT attempts.state, manifests.staging_root_dev, "
            "manifests.staging_root_ino FROM materialization_attempts AS attempts "
            "JOIN execution_manifests AS manifests USING (attempt_id) "
            "WHERE attempts.plan_id = ?",
            (plan_id,),
        ).fetchone() == ("STAGING_DURABLE", identity.st_dev, identity.st_ino)

    resumed = materialization.materialize_consolidation_plan(
        run_path, plan_id, destination
    )

    assert resumed["status"] == "materialized"
    assert tree_digest(selected_root) == source_before
    assert tree_digest(destination) == source_before
    assert (destination.stat().st_dev, destination.stat().st_ino) == (
        identity.st_dev,
        identity.st_ino,
    )
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        assert connection.execute(
            "SELECT state FROM materialization_attempts WHERE plan_id = ?",
            (plan_id,),
        ).fetchone() == ("COMPLETE",)


def test_materialize_reconciles_published_attempt_after_published_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, _output_root, _selected_root = _scan_simple_plan_finalize(tmp_path)
    monkeypatch.setenv(materialization.CRASH_POINT_ENVIRONMENT, "published")
    with pytest.raises(materialization._CrashSimulation):
        materialization.materialize_consolidation_plan(run_path, plan_id)
    monkeypatch.delenv(materialization.CRASH_POINT_ENVIRONMENT)

    resumed = materialization.materialize_consolidation_plan(run_path, plan_id)

    assert resumed["status"] == "materialized"
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        assert connection.execute(
            "SELECT state FROM materialization_attempts WHERE plan_id = ?",
            (plan_id,),
        ).fetchone() == ("COMPLETE",)


def test_materialize_published_recovery_fsync_failure_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, selected_root, destination = _crash_after_atomic_publication(
        tmp_path, monkeypatch
    )
    source_before = tree_digest(selected_root)
    identity = destination.stat()
    original_fsync = materialization._fsync_directory

    def fail_final_parent(path: Path) -> None:
        if path == destination.parent:
            raise OSError("injected parent sync failure")
        original_fsync(path)

    monkeypatch.setattr(materialization, "_fsync_directory", fail_final_parent)
    with pytest.raises(materialization.MaterializationError, match="needs-attention"):
        materialization.materialize_consolidation_plan(run_path, plan_id, destination)

    assert tree_digest(selected_root) == source_before
    assert tree_digest(destination) == source_before
    assert (destination.stat().st_dev, destination.stat().st_ino) == (
        identity.st_dev,
        identity.st_ino,
    )
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        assert connection.execute(
            "SELECT state, failure_classification FROM materialization_attempts "
            "WHERE plan_id = ?",
            (plan_id,),
        ).fetchone() == ("FAILED", "io-integrity-manual-attention")


def test_materialize_published_recovery_refuses_different_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, selected_root, destination = _crash_after_atomic_publication(
        tmp_path, monkeypatch
    )
    source_before = tree_digest(selected_root)
    replacement = tmp_path / "moved-published-result"
    destination.rename(replacement)
    destination.mkdir()

    with pytest.raises(materialization.MaterializationError, match="recovery-ambiguous"):
        materialization.materialize_consolidation_plan(run_path, plan_id, destination)

    assert tree_digest(selected_root) == source_before
    assert destination.is_dir() and list(destination.iterdir()) == []
    assert tree_digest(replacement) == source_before


@pytest.mark.parametrize("damage", ["missing", "mismatched"])
def test_materialize_published_recovery_refuses_damaged_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    from filesystem_organizer import materialization

    run_path, plan_id, selected_root, destination = _crash_after_atomic_publication(
        tmp_path, monkeypatch
    )
    source_before = tree_digest(selected_root)
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        if damage == "missing":
            connection.execute("DELETE FROM execution_manifests WHERE plan_id = ?", (plan_id,))
        else:
            connection.execute(
                "UPDATE execution_manifests SET canonical_destination = ? WHERE plan_id = ?",
                (str(tmp_path / "different-destination"), plan_id),
            )
        connection.commit()

    with pytest.raises(materialization.MaterializationError, match="recovery-ambiguous"):
        materialization.materialize_consolidation_plan(run_path, plan_id, destination)

    assert tree_digest(selected_root) == source_before
    assert tree_digest(destination) == source_before


def test_materialize_source_drift_records_mismatch_and_latches_plan(
    tmp_path: Path,
) -> None:
    run_path, plan_id, _output_root, selected_root = _scan_simple_plan_finalize(
        tmp_path
    )
    before = tree_digest(selected_root)

    changed_file = selected_root / "Control" / "meeting-notes.txt"
    original_bytes = changed_file.read_bytes()
    original_stat = changed_file.stat()
    changed_file.write_bytes(b"x" * len(original_bytes))

    failed = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")
    assert failed.returncode == 1
    assert "metadata" in failed.stderr

    destination = Path(_intended_destination(run_path, plan_id))
    partial = destination.parent / f".{destination.name}.partial-{plan_id}"
    assert partial.exists()
    assert not destination.exists()

    changed_file.write_bytes(original_bytes)
    os.utime(changed_file, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert tree_digest(selected_root) == before

    resumed = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")
    assert resumed.returncode == 0, resumed.stderr


def test_materialize_missing_source_records_mismatch_and_stops(
    tmp_path: Path,
) -> None:
    run_path, plan_id, _output_root, selected_root = _scan_simple_plan_finalize(
        tmp_path
    )

    missing_file = selected_root / "Control" / "meeting-notes.txt"
    missing_file.unlink()

    failed = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")
    assert failed.returncode == 1
    assert "source file is missing" in failed.stderr

    destination = Path(_intended_destination(run_path, plan_id))
    assert not destination.exists()


def test_materialize_never_overwrites_unexpected_existing_final_path(
    tmp_path: Path,
) -> None:
    run_path, plan_id, _output_root, selected_root = _scan_plan_finalize(tmp_path)
    before = tree_digest(selected_root)

    crashed = _materialize_cli(tmp_path, run_path, plan_id, crash_point="admission")
    assert crashed.returncode != 0

    destination = Path(_intended_destination(run_path, plan_id))
    conflicting_path = destination / _operation_output_paths(run_path, plan_id)[0]
    conflicting_path.parent.mkdir(parents=True, exist_ok=True)
    conflicting_path.write_bytes(b"unexpected occupant")

    resumed = _materialize_cli(tmp_path, run_path, plan_id)
    assert resumed.returncode == 1
    assert "destination already exists" in resumed.stderr
    assert conflicting_path.read_bytes() == b"unexpected occupant"
    assert tree_digest(selected_root) == before


def test_materialize_missing_final_after_publication_stops_without_recreating(
    tmp_path: Path,
) -> None:
    run_path, plan_id, _output_root, selected_root = _scan_plan_finalize(tmp_path)
    before = tree_digest(selected_root)

    first = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")
    assert first.returncode == 0, first.stderr
    destination = Path(json.loads(first.stdout)["destination"])

    removed_path = destination / _operation_output_paths(run_path, plan_id)[-1]
    assert removed_path.is_file()
    removed_path.unlink()

    resumed = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")
    assert resumed.returncode == 1
    assert "destination already exists" in resumed.stderr
    assert not removed_path.exists()
    assert tree_digest(selected_root) == before


def test_materialize_corrupted_final_records_conflict_and_latches_plan(
    tmp_path: Path,
) -> None:
    run_path, plan_id, _output_root, selected_root = _scan_plan_finalize(tmp_path)
    before = tree_digest(selected_root)

    first = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")
    assert first.returncode == 0, first.stderr
    destination = Path(json.loads(first.stdout)["destination"])

    relative_paths = _operation_output_paths(run_path, plan_id)
    corrupted_path = destination / relative_paths[-1]
    corrupted_path.write_bytes(b"corrupted after publication")

    resumed = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")
    assert resumed.returncode == 1
    assert "destination already exists" in resumed.stderr
    assert corrupted_path.read_bytes() == b"corrupted after publication"
    assert tree_digest(selected_root) == before

    refused = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")
    assert refused.returncode == 1
    assert "destination already exists" in refused.stderr


def test_materialize_recovery_rejects_same_size_changed_final(tmp_path: Path) -> None:
    run_path, plan_id, _output_root, _selected_root = _scan_plan_finalize(tmp_path)
    first = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")
    assert first.returncode == 0, first.stderr
    destination = Path(json.loads(first.stdout)["destination"])
    changed = destination / _operation_output_paths(run_path, plan_id)[0]
    changed.write_bytes(b"x" * changed.stat().st_size)

    resumed = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")

    assert resumed.returncode == 1
    assert "destination already exists" in resumed.stderr
