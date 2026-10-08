from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from blake3 import blake3

from filesystem_organizer.progress import ProgressReporter

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def tree_digest(root: Path) -> str:
    digest = blake3()
    for path in sorted(root.rglob("*")):
        relative_path = path.relative_to(root).as_posix().encode()
        digest.update(relative_path)
        digest.update(b"\0")
        if path.is_file() and not path.is_symlink():
            try:
                digest.update(path.read_bytes())
            except PermissionError:
                digest.update(b"unreadable")
        digest.update(b"\0")
    return digest.hexdigest()


def run_cli(
    working_directory: Path, *arguments: str
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(PROJECT_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "filesystem_organizer", *arguments],
        cwd=working_directory,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_scan_and_report_persist_inventory_without_changing_selected_backup_root(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "historical backups"
    selected_root.mkdir()
    (selected_root / "notes.txt").write_bytes(b"remember this\n")
    unicode_directory = selected_root / "Steuererklärung"
    unicode_directory.mkdir()
    (unicode_directory / "Beleg €.txt").write_bytes(b"42")
    before = tree_digest(selected_root)
    output_root = tmp_path / "run output"

    scan = run_cli(
        tmp_path,
        "scan",
        str(selected_root),
        "--output-root",
        str(output_root),
    )

    assert scan.returncode == 0, scan.stderr
    result = json.loads(scan.stdout)
    assert "Scanning:" in scan.stderr
    assert "Structural analysis..." in scan.stderr
    assert "Structural analysis done" in scan.stderr
    assert "Computing canonical copies..." in scan.stderr
    assert "Computing canonical copies done" in scan.stderr
    assert result["status"] == "complete"
    assert result["selected_backup_root"] == str(selected_root.resolve())
    assert result["inventory_count"] == 2
    assert result["run_id"]
    assert result["snapshot_id"]
    run_path = Path(result["analysis_run"])
    assert run_path.parent.parent == output_root.resolve()
    assert (run_path / "analysis.sqlite3").is_file()

    report = run_cli(tmp_path, "report", str(run_path), "--detail", "full")

    assert report.returncode == 0, report.stderr
    assert "# Analysis Run Report" in report.stdout
    assert f"**Run ID:** `{result['run_id']}`" in report.stdout
    assert f"**Snapshot ID:** `{result['snapshot_id']}`" in report.stdout
    assert "| `notes.txt` | regular-file | 14 |" in report.stdout
    assert "| `Steuererklärung/Beleg €.txt` | regular-file | 2 |" in report.stdout
    assert "| successful |" in report.stdout
    assert tree_digest(selected_root) == before


def test_reports_are_concise_by_default_and_expose_bounded_full_evidence(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "historical backups"
    selected_root.mkdir()
    (selected_root / "first.txt").write_bytes(b"same")
    (selected_root / "second.txt").write_bytes(b"same")

    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    assert "Preparing plan..." in plan.stderr
    assert "Preparing plan done" in plan.stderr
    assert "Verifying retained files..." not in plan.stderr
    assert "Deriving plan operations..." in plan.stderr
    assert "Projecting directory structure..." in plan.stderr
    assert "Persisting plan..." in plan.stderr
    assert "Persisting plan done" in plan.stderr

    report = run_cli(tmp_path, "report", str(run_path))
    assert report.returncode == 0, report.stderr
    assert "**Readable regular files:** 2" in report.stdout
    assert "## Skipped Entry Findings" in report.stdout
    assert "## Persisted Inventory Evidence" not in report.stdout
    assert "## Exact Duplicate Groups" not in report.stdout

    full_report = run_cli(tmp_path, "report", str(run_path), "--detail", "full")
    assert full_report.returncode == 0, full_report.stderr
    assert "## Persisted Inventory Evidence" in full_report.stdout
    assert "## Exact Duplicate Groups" in full_report.stdout

    inventory_page = run_cli(
        tmp_path,
        "report",
        str(run_path),
        "--section",
        "inventory",
        "--offset",
        "1",
        "--limit",
        "1",
    )
    assert inventory_page.returncode == 0, inventory_page.stderr
    assert inventory_page.stdout.count("| regular-file |") == 1
    assert "`second.txt`" not in inventory_page.stdout

    plan_report = run_cli(tmp_path, "plan-report", str(run_path))
    assert plan_report.returncode == 0, plan_report.stderr
    assert "**Operation Count:** 1" in plan_report.stdout
    assert "## Consolidation Plan Operations" not in plan_report.stdout

    full_plan_report = run_cli(
        tmp_path, "plan-report", str(run_path), "--detail", "full"
    )
    assert full_plan_report.returncode == 0, full_plan_report.stderr
    assert "## Consolidation Plan Operations" in full_plan_report.stdout

    operations_page = run_cli(
        tmp_path,
        "plan-report",
        str(run_path),
        "--section",
        "operations",
        "--limit",
        "1",
    )
    assert operations_page.returncode == 0, operations_page.stderr
    assert operations_page.stdout.count("newest modification timestamp") == 1


def test_reports_bound_high_cardinality_findings_and_page_complete_evidence(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "kept.txt").write_text("content")
    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    scan_result = json.loads(scan.stdout)
    run_path = Path(scan_result["analysis_run"])
    run_id = scan_result["run_id"]
    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_id = json.loads(plan.stdout)["plan_id"]

    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        connection.executemany(
            "INSERT INTO skipped_entry_findings VALUES (?, ?, ?)",
            [
                (run_id, f"skipped-{index:02}.txt", "synthetic finding")
                for index in range(25)
            ],
        )
        connection.execute(
            "INSERT INTO directory_relationships VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, "left", "right", "conflicting", "left", "test", "", ""),
        )
        connection.executemany(
            "INSERT INTO relationship_conflicts VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id,
                    "left",
                    "right",
                    f"conflict-{index:02}.txt",
                    "left evidence",
                    "right evidence",
                )
                for index in range(25)
            ],
        )
        connection.execute(
            "INSERT INTO plan_projection_conflict_groups VALUES (?, ?, ?, ?)",
            (plan_id, 0, "left", "test"),
        )
        connection.executemany(
            "INSERT INTO plan_projection_conflict_group_roots VALUES (?, ?, ?)",
            [(plan_id, 0, "left"), (plan_id, 0, "right")],
        )
        connection.executemany(
            "INSERT INTO plan_projection_conflict_omitted_entries VALUES (?, ?, ?, ?)",
            [
                (plan_id, 0, f"skipped-{index:02}.txt", "omitted finding")
                for index in range(25)
            ],
        )
        connection.executemany(
            "INSERT INTO plan_conflict_projections VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    plan_id,
                    0,
                    index,
                    f"source-{index:02}.txt",
                    f"output-{index:02}.txt",
                    "file",
                    "variant",
                    "synthetic projection",
                )
                for index in range(25)
            ],
        )

    for detail in ((), ("--detail", "full")):
        report = run_cli(tmp_path, "report", str(run_path), *detail)
        assert report.returncode == 0, report.stderr
        assert "**Skipped entries:** 25" in report.stdout
        assert report.stdout.count("synthetic finding") == 20
        assert "Showing the first 20 of 25" in report.stdout

        plan_report = run_cli(tmp_path, "plan-report", str(run_path), *detail)
        assert plan_report.returncode == 0, plan_report.stderr
        assert plan_report.stdout.count("synthetic finding") == 20
        assert plan_report.stdout.count("synthetic projection") == 20
        assert "Showing the first 20 of 25" in plan_report.stdout
        if detail:
            assert "omitted finding" not in plan_report.stdout

    run_skipped_page = run_cli(
        tmp_path,
        "report",
        str(run_path),
        "--section",
        "skipped",
        "--offset",
        "20",
        "--limit",
        "5",
    )
    assert run_skipped_page.returncode == 0, run_skipped_page.stderr
    assert "**Total:** 25" in run_skipped_page.stdout
    assert "`skipped-20.txt`" in run_skipped_page.stdout
    assert "`skipped-19.txt`" not in run_skipped_page.stdout

    plan_skipped_page = run_cli(
        tmp_path,
        "plan-report",
        str(run_path),
        "--section",
        "skipped",
        "--offset",
        "20",
        "--limit",
        "5",
    )
    assert plan_skipped_page.returncode == 0, plan_skipped_page.stderr
    assert "**Total:** 25" in plan_skipped_page.stdout
    assert "`skipped-20.txt`" in plan_skipped_page.stdout
    assert "`skipped-19.txt`" not in plan_skipped_page.stdout

    run_conflict_page = run_cli(
        tmp_path,
        "report",
        str(run_path),
        "--section",
        "conflicts",
        "--offset",
        "20",
        "--limit",
        "5",
    )
    assert run_conflict_page.returncode == 0, run_conflict_page.stderr
    assert "**Total:** 25" in run_conflict_page.stdout
    assert "`conflict-20.txt`" in run_conflict_page.stdout
    assert "`conflict-19.txt`" not in run_conflict_page.stdout
    assert run_conflict_page.stdout.count("left evidence") == 5
    assert run_conflict_page.stdout.count("right evidence") == 5

    plan_conflict_page = run_cli(
        tmp_path,
        "plan-report",
        str(run_path),
        "--section",
        "conflicts",
        "--offset",
        "20",
        "--limit",
        "5",
    )
    assert plan_conflict_page.returncode == 0, plan_conflict_page.stderr
    assert "**Total:** 25" in plan_conflict_page.stdout
    assert "`source-20.txt`" in plan_conflict_page.stdout
    assert "`source-19.txt`" not in plan_conflict_page.stdout


def test_full_plan_report_bounds_union_omissions(tmp_path: Path) -> None:
    selected_root = tmp_path / "backup"
    for name in ("alpha", "beta"):
        root = selected_root / name
        for region in ("one", "two"):
            (root / region).mkdir(parents=True)
            (root / region / "anchor.txt").write_text(region)
        for index in range(30):
            (root / f"skipped-{index:02}").symlink_to("missing")
    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    run_path = json.loads(scan.stdout)["analysis_run"]
    plan = run_cli(tmp_path, "plan", run_path)
    assert plan.returncode == 0, plan.stderr
    report = run_cli(tmp_path, "plan-report", run_path, "--detail", "full")
    assert report.returncode == 0, report.stderr
    assert report.stdout.count(": symbolic-link") == 20
    assert "Showing the first 20 of 60" in report.stdout
    page = run_cli(
        tmp_path, "plan-report", run_path, "--section", "skipped",
        "--offset", "55", "--limit", "5",
    )
    assert page.returncode == 0, page.stderr
    assert "`beta/skipped-29`" in page.stdout


def test_default_output_creates_distinct_analysis_runs(tmp_path: Path) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "one.bin").write_bytes(b"one")

    first = run_cli(tmp_path, "scan", str(selected_root))
    second = run_cli(tmp_path, "scan", str(selected_root))

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    first_result = json.loads(first.stdout)
    second_result = json.loads(second.stdout)
    assert first_result["run_id"] != second_result["run_id"]
    assert first_result["snapshot_id"] != second_result["snapshot_id"]
    assert Path(first_result["analysis_run"]).parent.parent == tmp_path / "output"


def test_scan_persists_content_identities_without_a_separate_hash_checkpoint(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "first.txt").write_bytes(b"same content")
    (selected_root / "second.txt").write_bytes(b"same content")

    scan = run_cli(tmp_path, "scan", str(selected_root))

    assert scan.returncode == 0, scan.stderr
    result = json.loads(scan.stdout)
    status = run_cli(tmp_path, "status", result["analysis_run"])
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["hash_checkpoint_relative_path"] is None
    with sqlite3.connect(
        Path(result["analysis_run"]) / "analysis.sqlite3"
    ) as connection:
        identities = connection.execute(
            "SELECT relative_path, algorithm, algorithm_version, byte_size, digest, "
            "read_outcome FROM content_identities ORDER BY relative_path"
        ).fetchall()
    digest = blake3(b"same content").hexdigest()
    assert identities == [
        ("first.txt", "BLAKE3-256", 1, 12, digest, "successful"),
        ("second.txt", "BLAKE3-256", 1, 12, digest, "successful"),
    ]


def test_scan_records_metadata_observation_without_reading_unique_file(
    tmp_path: Path,
) -> None:
    from filesystem_organizer import analysis_run

    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    source = selected_root / "source.bin"
    source.write_bytes(b"original")
    original_collect_entries = analysis_run._collect_entries

    def replace_after_observation(root: Path) -> list[analysis_run.InventoryEntry]:
        entries = original_collect_entries(root)
        original_metadata = source.lstat()
        replacement = selected_root / "replacement.bin"
        replacement.write_bytes(b"replaced")
        os.utime(
            replacement,
            ns=(original_metadata.st_atime_ns, original_metadata.st_mtime_ns),
        )
        os.replace(replacement, source)
        assert source.lstat().st_ino != original_metadata.st_ino
        return entries

    analysis_run._collect_entries = replace_after_observation
    try:
        result = analysis_run.create_analysis_run(selected_root, tmp_path / "output")
    finally:
        analysis_run._collect_entries = original_collect_entries

    assert result["inventory_count"] == 1
    assert result["skipped_count"] == 0
    database_path = Path(str(result["analysis_run"])) / "analysis.sqlite3"
    with sqlite3.connect(database_path) as connection:
        identity = connection.execute(
            "SELECT byte_size, digest, read_outcome FROM content_identities "
            "WHERE relative_path = 'source.bin'"
        ).fetchone()
    assert identity is None


def test_custom_layout_cli_exports_validates_and_applies_a_subtree_rule(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    (selected_root / "documents").mkdir(parents=True)
    (selected_root / "documents" / "note.txt").write_bytes(b"note")
    (selected_root / "images").mkdir()
    (selected_root / "images" / "photo.jpg").write_bytes(b"photo")

    scan = run_cli(tmp_path, "scan", str(selected_root))
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
    assert layout["plan_id"] == plan_id
    assert layout["base_revision"] == 0
    layout["rules"] = [
        {
            "selector": {"subtree": "documents"},
            "action": {"place_under": "Notes"},
        }
    ]
    layout["directories"] = ["Empty folder"]
    layout_path.write_text(json.dumps(layout))

    validated = run_cli(
        tmp_path, "plan-layout-validate", str(run_path), plan_id, str(layout_path)
    )
    assert validated.returncode == 0, validated.stderr
    validation = json.loads(validated.stdout)
    assert validation["valid"] is True
    assert validation["placed_content_entry_count"] == 2

    applied = run_cli(
        tmp_path, "plan-layout-apply", str(run_path), plan_id, str(layout_path)
    )
    assert applied.returncode == 0, applied.stderr
    assert json.loads(applied.stdout)["revision"] == 1

    structure = run_cli(
        tmp_path,
        "plan-report",
        str(run_path),
        "--plan-id",
        plan_id,
        "--section",
        "structure",
        "--depth",
        "3",
    )
    assert structure.returncode == 0, structure.stderr
    assert "**Layout Revision:** 1" in structure.stdout
    assert "**Directory Levels:** 3" in structure.stdout
    assert "| `Notes` | 1 | 1 | 1 |" in structure.stdout
    assert "note.txt" not in structure.stdout

    overview = run_cli(
        tmp_path,
        "plan-report",
        str(run_path),
        "--plan-id",
        plan_id,
        "--section",
        "structure",
        "--depth",
        "1",
    )
    assert overview.returncode == 0, overview.stderr
    assert "| `Notes` | 1 | 1 | 1 |" in overview.stdout
    assert "| `images` | 1 | 1 | 1 |" in overview.stdout
    assert "| `Empty folder` | 1 | 0 | 0 |" in overview.stdout
    assert "note.txt" not in overview.stdout

    finalized = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)
    assert finalized.returncode == 0, finalized.stderr
    finalized_overview = run_cli(
        tmp_path,
        "plan-report",
        str(run_path),
        "--plan-id",
        plan_id,
        "--section",
        "structure",
        "--depth",
        "1",
    )
    assert finalized_overview.returncode == 0, finalized_overview.stderr
    assert "| `Notes` | 1 | 1 | 1 |" in finalized_overview.stdout
    assert "| `images` | 1 | 1 | 1 |" in finalized_overview.stdout
    assert "| `Empty folder` | 1 | 0 | 0 |" in finalized_overview.stdout
    materialized = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")
    assert materialized.returncode == 0, materialized.stderr
    destination = Path(json.loads(plan.stdout)["intended_destination"])
    assert (destination / "Notes" / "note.txt").read_bytes() == b"note"
    assert (destination / "images" / "photo.jpg").read_bytes() == b"photo"
    assert (destination / "Empty folder").is_dir()


def test_custom_layout_reports_all_findings_and_refuses_stale_or_unacknowledged_apply(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    for name in ("one", "two"):
        (selected_root / name).mkdir(parents=True)
        (selected_root / name / "item.txt").write_bytes(name.encode())
    scan = run_cli(tmp_path, "scan", str(selected_root))
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    plan_id = str(json.loads(plan.stdout)["plan_id"])
    layout_path = tmp_path / "layout.json"
    assert (
        run_cli(
            tmp_path,
            "plan-layout-export",
            str(run_path),
            plan_id,
            "--output",
            str(layout_path),
        ).returncode
        == 0
    )
    layout = json.loads(layout_path.read_text())
    layout["rules"] = [
        {"selector": {"subtree": "one"}, "action": {"place_under": "same"}},
        {"selector": {"subtree": "two"}, "action": {"place_under": "same"}},
    ]
    layout_path.write_text(json.dumps(layout))
    invalid = run_cli(
        tmp_path, "plan-layout-validate", str(run_path), plan_id, str(layout_path)
    )
    assert invalid.returncode == 0
    findings = json.loads(invalid.stdout)["findings"]
    assert any(finding["code"] == "output-collision" for finding in findings)

    layout["rules"] = [{"selector": {"subtree": "one"}, "action": {"exclude": True}}]
    layout_path.write_text(json.dumps(layout))
    unacknowledged = run_cli(
        tmp_path, "plan-layout-apply", str(run_path), plan_id, str(layout_path)
    )
    assert unacknowledged.returncode == 1
    assert "acknowledge-exclusions" in unacknowledged.stderr
    applied = run_cli(
        tmp_path,
        "plan-layout-apply",
        str(run_path),
        plan_id,
        str(layout_path),
        "--acknowledge-exclusions",
        "--acknowledge-content-empty",
    )
    assert applied.returncode == 0, applied.stderr
    stale = run_cli(
        tmp_path, "plan-layout-validate", str(run_path), plan_id, str(layout_path)
    )
    assert stale.returncode == 0
    assert any(
        finding["code"] == "stale-revision"
        for finding in json.loads(stale.stdout)["findings"]
    )
    assert json.loads(stale.stdout)["placed_content_entry_count"] == 1


def test_custom_layout_validation_reports_a_content_empty_result(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    (selected_root / "documents").mkdir(parents=True)
    (selected_root / "documents" / "note.txt").write_bytes(b"note")
    scan = run_cli(tmp_path, "scan", str(selected_root))
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    plan_id = str(json.loads(plan.stdout)["plan_id"])
    layout_path = tmp_path / "layout.json"
    assert (
        run_cli(
            tmp_path,
            "plan-layout-export",
            str(run_path),
            plan_id,
            "--output",
            str(layout_path),
        ).returncode
        == 0
    )
    layout = json.loads(layout_path.read_text())
    layout["rules"] = [
        {"selector": {"subtree": "documents"}, "action": {"exclude": True}}
    ]
    layout_path.write_text(json.dumps(layout))

    validated = run_cli(
        tmp_path, "plan-layout-validate", str(run_path), plan_id, str(layout_path)
    )

    assert validated.returncode == 0, validated.stderr
    validation = json.loads(validated.stdout)
    assert validation["valid"] is True
    assert validation["placed_content_entry_count"] == 0
    assert validation["acknowledgements_required"] == {
        "exclusions": True,
        "content_empty": True,
    }
    refused = run_cli(
        tmp_path,
        "plan-layout-apply",
        str(run_path),
        plan_id,
        str(layout_path),
        "--acknowledge-exclusions",
    )
    assert refused.returncode == 1
    assert "acknowledge-content-empty" in refused.stderr
    applied = run_cli(
        tmp_path,
        "plan-layout-apply",
        str(run_path),
        plan_id,
        str(layout_path),
        "--acknowledge-exclusions",
        "--acknowledge-content-empty",
    )
    assert applied.returncode == 0, applied.stderr
    assert (
        run_cli(
            tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id
        ).returncode
        == 0
    )
    materialize = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")
    assert materialize.returncode == 1
    assert "acknowledge-content-empty" in materialize.stderr


def test_custom_layout_validation_does_not_classify_a_directory_only_baseline_as_content_empty(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    (selected_root / "empty").mkdir(parents=True)
    scan = run_cli(tmp_path, "scan", str(selected_root))
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    plan_id = str(json.loads(plan.stdout)["plan_id"])
    layout_path = tmp_path / "layout.json"
    assert (
        run_cli(
            tmp_path,
            "plan-layout-export",
            str(run_path),
            plan_id,
            "--output",
            str(layout_path),
        ).returncode
        == 0
    )

    validated = run_cli(
        tmp_path, "plan-layout-validate", str(run_path), plan_id, str(layout_path)
    )

    assert validated.returncode == 0, validated.stderr
    validation = json.loads(validated.stdout)
    assert validation["valid"] is True
    assert validation["placed_content_entry_count"] == 0
    assert validation["acknowledgements_required"] == {
        "exclusions": False,
        "content_empty": False,
    }


def test_custom_layout_requires_an_explicit_action_for_each_skipped_entry(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    documents = selected_root / "documents"
    documents.mkdir(parents=True)
    (documents / "kept.txt").write_bytes(b"kept")
    (documents / "unverified-link").symlink_to("kept.txt")
    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    plan_id = str(json.loads(plan.stdout)["plan_id"])
    layout_path = tmp_path / "layout.json"
    assert (
        run_cli(
            tmp_path,
            "plan-layout-export",
            str(run_path),
            plan_id,
            "--output",
            str(layout_path),
        ).returncode
        == 0
    )
    missing = run_cli(
        tmp_path, "plan-layout-validate", str(run_path), plan_id, str(layout_path)
    )
    assert json.loads(missing.stdout)["valid"] is True
    layout = json.loads(layout_path.read_text())
    layout["skipped_actions"] = [
        {"path": "documents/unverified-link", "action": "retain"}
    ]
    layout_path.write_text(json.dumps(layout))
    validated = run_cli(
        tmp_path, "plan-layout-validate", str(run_path), plan_id, str(layout_path)
    )
    assert json.loads(validated.stdout)["valid"] is True


def test_in_place_custom_layout_exclusion_removes_the_proven_identity_after_completion(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    documents = selected_root / "documents"
    documents.mkdir(parents=True)
    (documents / "keep.txt").write_bytes(b"keep")
    (documents / "exclude.txt").write_bytes(b"exclude")
    scan = run_cli(tmp_path, "scan", str(selected_root))
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    plan_id = str(json.loads(plan.stdout)["plan_id"])
    layout_path = tmp_path / "layout.json"
    assert (
        run_cli(
            tmp_path,
            "plan-layout-export",
            str(run_path),
            plan_id,
            "--output",
            str(layout_path),
        ).returncode
        == 0
    )
    layout = json.loads(layout_path.read_text())
    layout["entry_exceptions"] = []
    layout["rules"] = [
        {"selector": {"subtree": "documents/exclude.txt"}, "action": {"exclude": True}}
    ]
    layout_path.write_text(json.dumps(layout))
    assert (
        run_cli(
            tmp_path,
            "plan-layout-apply",
            str(run_path),
            plan_id,
            str(layout_path),
            "--acknowledge-exclusions",
        ).returncode
        == 0
    )
    assert (
        run_cli(
            tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id
        ).returncode
        == 0
    )
    materialized = run_cli(
        tmp_path,
        "materialize",
        str(run_path),
        plan_id,
        "--in-place",
        "--yes",
        "--acknowledge-destructive",
    )
    assert materialized.returncode == 0, materialized.stderr
    assert not (selected_root / "documents" / "exclude.txt").exists()
    assert (selected_root / "documents" / "keep.txt").read_bytes() == b"keep"


def test_scan_rejects_output_inside_selected_backup_root_without_creating_it(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "one.bin").write_bytes(b"one")
    unsafe_output = selected_root / "analysis-output"
    before = tree_digest(selected_root)

    scan = run_cli(
        tmp_path,
        "scan",
        str(selected_root),
        "--output-root",
        str(unsafe_output),
    )

    assert scan.returncode == 1
    assert scan.stdout == ""
    assert "Run Output Root must be outside" in scan.stderr
    assert not unsafe_output.exists()
    assert tree_digest(selected_root) == before


def test_scan_rejects_analysis_run_path_inside_selected_backup_root(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "output"
    selected_root = output_root / "runs"
    selected_root.mkdir(parents=True)
    (selected_root / "one.bin").write_bytes(b"one")
    before = tree_digest(selected_root)

    scan = run_cli(
        tmp_path,
        "scan",
        str(selected_root),
        "--output-root",
        str(output_root),
    )

    assert scan.returncode == 1
    assert "Analysis Run must be outside" in scan.stderr
    assert tree_digest(selected_root) == before


def test_scan_rejects_runs_symlink_that_escapes_run_output_root(tmp_path: Path) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "one.bin").write_bytes(b"one")
    output_root = tmp_path / "output"
    output_root.mkdir()
    redirected_runs = tmp_path / "redirected-runs"
    redirected_runs.mkdir()
    (output_root / "runs").symlink_to(redirected_runs, target_is_directory=True)

    scan = run_cli(
        tmp_path,
        "scan",
        str(selected_root),
        "--output-root",
        str(output_root),
    )

    assert scan.returncode == 1
    assert "Analysis Run must remain under the Run Output Root" in scan.stderr
    assert list(redirected_runs.iterdir()) == []


def test_scan_records_a_symlink_without_following_it(tmp_path: Path) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    outside = tmp_path / "outside-secret.txt"
    outside.write_bytes(b"must not be inventoried")
    (selected_root / "link.txt").symlink_to(outside)

    scan = run_cli(tmp_path, "scan", str(selected_root))

    assert scan.returncode == 0, scan.stderr
    result = json.loads(scan.stdout)
    assert result["inventory_count"] == 0
    assert result["skipped_count"] == 1
    report = run_cli(tmp_path, "report", result["analysis_run"])
    assert report.returncode == 0, report.stderr
    assert "| `link.txt` | symbolic-link |" in report.stdout
    skipped_section = report.stdout.split("## Skipped Entry Findings", maxsplit=1)[1]
    assert "| `link.txt` | symbolic-link |" in skipped_section
    assert "outside-secret" not in report.stdout


def test_report_refuses_analysis_run_with_ambiguous_run_identity(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "one.bin").write_bytes(b"one")
    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    result = json.loads(scan.stdout)
    database = Path(result["analysis_run"]) / "analysis.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO analysis_runs VALUES "
            "('other-run', 'other-snapshot', 1, ?, 'complete', ?, ?, NULL, NULL, NULL)",
            (
                str(selected_root),
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )

    report = run_cli(tmp_path, "report", result["analysis_run"])

    assert report.returncode == 1
    assert "exactly one run identity" in report.stderr


def test_scan_and_report_produce_complete_deterministic_example_inventory(
    tmp_path: Path,
) -> None:
    from tests.example_fixture import SOURCE_ROOT, copy_prepared_source

    source_before = tree_digest(SOURCE_ROOT)
    selected_root = copy_prepared_source(tmp_path / "selected-backup-root")
    before = tree_digest(selected_root)
    output_root = tmp_path / "output"

    first = run_cli(
        tmp_path, "scan", str(selected_root), "--output-root", str(output_root)
    )
    assert first.returncode == 0, first.stderr
    first_result = json.loads(first.stdout)
    first_report = run_cli(
        tmp_path, "report", first_result["analysis_run"], "--detail", "full"
    )
    assert first_report.returncode == 0, first_report.stderr

    second_output_root = tmp_path / "output-2"
    second = run_cli(
        tmp_path, "scan", str(selected_root), "--output-root", str(second_output_root)
    )
    assert second.returncode == 0, second.stderr
    second_result = json.loads(second.stdout)
    second_report = run_cli(
        tmp_path, "report", second_result["analysis_run"], "--detail", "full"
    )
    assert second_report.returncode == 0, second_report.stderr

    assert first_result["inventory_count"] == second_result["inventory_count"]
    assert first_result["skipped_count"] == second_result["skipped_count"]

    def strip_identity(body: str) -> str:
        lines = body.splitlines()
        return "\n".join(
            line
            for line in lines
            if not line.startswith("**Run ID:**")
            and not line.startswith("**Snapshot ID:**")
        )

    assert strip_identity(first_report.stdout) == strip_identity(second_report.stdout)

    report_body = first_report.stdout
    assert "| `snapshot-2025-07-01/.DS_Store` |" in report_body
    assert (
        "| `snapshot-2025-07-01/Home/Desktop/empty-file.txt` | regular-file | 0 |"
        in report_body
    )
    assert "| `snapshot-2025-07-01/Home/Desktop/same-size-alpha.txt` |" in report_body
    assert "| `snapshot-2025-07-01/Home/Desktop/same-size-bravo.txt` |" in report_body
    assert (
        "| `snapshot-2025-07-01/Home/Desktop/link-to-annual-tax-statement` | symbolic-link |"
        in report_body
    )
    skipped_section = report_body.split("## Skipped Entry Findings", maxsplit=1)[1]
    assert (
        "| `snapshot-2025-07-01/Home/Desktop/link-to-annual-tax-statement` | symbolic-link |"
        in skipped_section
    )
    assert "| `snapshot-2025-07-01/Home/Desktop/unreadable.txt` |" in skipped_section

    duplicate_groups_section = report_body.split(
        "## Exact Duplicate Groups", maxsplit=1
    )[1]
    assert "None." not in duplicate_groups_section.splitlines()[0:2]

    recovery_group = next(
        block
        for block in duplicate_groups_section.split("### ")[1:]
        if "snapshot-2023-11-01/Home/Projects/ai-assistant/docs/recovery-checklist.txt"
        in block
    )
    assert (
        "**Canonical Copy:** `snapshot-2023-11-01/Home/Projects/ai-assistant/docs/recovery-checklist.txt`"
        in recovery_group
    )
    assert "newest modification timestamp" in recovery_group
    canonical_row = next(
        line
        for line in recovery_group.splitlines()
        if line.startswith(
            "| `snapshot-2023-11-01/Home/Projects/ai-assistant/docs/recovery-checklist.txt` |"
        )
    )
    assert canonical_row.rstrip().endswith("| yes |")
    assert (
        "snapshot-2025-07-01/Home/Desktop/recovery-checklist (copy).txt"
        in recovery_group
    )
    assert (
        "snapshot-2025-07-01/Home/Projects/ai-assistant/docs/recovery-checklist.txt"
        in recovery_group
    )

    tax_group = next(
        block
        for block in duplicate_groups_section.split("### ")[1:]
        if "annual_tax_statement_2024" in block
    )
    assert (
        "**Canonical Copy:** `snapshot-2025-07-01/Home/Desktop/annual_tax_statement_2024.pdf`"
        in tax_group
    )
    assert (
        "snapshot-2025-07-01/Home/Desktop/annual_tax_statement_2024 (copy).pdf"
        in tax_group
    )
    assert "newest modification timestamp" in tax_group

    assert not any(
        "snapshot-2025-07-01/Home/Desktop/same-size-alpha.txt" in block
        and "snapshot-2025-07-01/Home/Desktop/same-size-bravo.txt" in block
        for block in duplicate_groups_section.split("### ")[1:]
    )

    assert tree_digest(selected_root) == before
    assert tree_digest(SOURCE_ROOT) == source_before


def test_resume_continues_interrupted_scan_from_checkpoint(tmp_path: Path) -> None:
    from filesystem_organizer import analysis_run

    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "empty").mkdir()
    nested_root = selected_root / "folder"
    nested_root.mkdir()
    (nested_root / "nested-empty").mkdir()
    for index in range(6):
        (selected_root / f"file-{index}.bin").write_bytes(f"payload-{index}".encode())
    output_root = tmp_path / "output"

    original_batch_size = analysis_run.BATCH_SIZE
    original_run_scan_batches = analysis_run._run_scan_batches
    call_count = {"value": 0}

    def flaky_run_scan_batches(
        connection: sqlite3.Connection,
        run_id: str,
        selected_root: Path,
        resume_after: str | None,
        progress: ProgressReporter,
    ) -> None:
        call_count["value"] += 1
        if call_count["value"] == 1:
            entries = analysis_run._collect_entries(selected_root)
            if resume_after is not None:
                entries = [e for e in entries if e.relative_path > resume_after]
            first_batch = entries[: analysis_run.BATCH_SIZE]
            analysis_run._persist_scan_batch(
                connection, run_id, selected_root, first_batch
            )
            raise RuntimeError("simulated interruption after first batch")
        original_run_scan_batches(
            connection, run_id, selected_root, resume_after, progress
        )

    analysis_run.BATCH_SIZE = 3
    analysis_run._run_scan_batches = flaky_run_scan_batches
    try:
        try:
            analysis_run.create_analysis_run(selected_root, output_root)
            raise AssertionError("expected simulated interruption to raise")
        except RuntimeError:
            pass
    finally:
        analysis_run._run_scan_batches = original_run_scan_batches
        analysis_run.BATCH_SIZE = original_batch_size

    runs_directory = output_root / "runs"
    run_id = next(runs_directory.iterdir()).name
    analysis_run_path = runs_directory / run_id
    database_path = analysis_run_path / "analysis.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM analysis_runs").fetchone()
        assert row["status"] == "scanning"
        assert row["writer_lease"] is None
        assert row["checkpoint_relative_path"] is not None
        checkpointed_files = connection.execute(
            "SELECT relative_path FROM inventory_entries "
            "WHERE entry_kind = 'regular-file' ORDER BY relative_path"
        ).fetchall()
        checkpointed_identities = connection.execute(
            "SELECT relative_path FROM content_identities ORDER BY relative_path"
        ).fetchall()
        assert checkpointed_files
        assert checkpointed_identities == []

        connection.execute(
            "UPDATE analysis_runs SET writer_lease = 'stale-competing-lease' WHERE run_id = ?",
            (run_id,),
        )
        connection.commit()

    contended = run_cli(tmp_path, "resume", str(analysis_run_path))
    assert contended.returncode == 1
    assert "active writer lease" in contended.stderr

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE analysis_runs SET writer_lease = NULL WHERE run_id = ?", (run_id,)
        )
        connection.commit()

    resumed = run_cli(tmp_path, "resume", str(analysis_run_path))
    assert resumed.returncode == 0, resumed.stderr
    resumed_result = json.loads(resumed.stdout)
    assert resumed_result["inventory_count"] == 6
    assert resumed_result["skipped_count"] == 0

    control_output_root = tmp_path / "output-control"
    control = run_cli(
        tmp_path, "scan", str(selected_root), "--output-root", str(control_output_root)
    )
    assert control.returncode == 0, control.stderr
    control_result = json.loads(control.stdout)
    assert control_result["inventory_count"] == resumed_result["inventory_count"]
    assert control_result["skipped_count"] == resumed_result["skipped_count"]

    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT relative_path, entry_kind, read_outcome FROM inventory_entries "
            "ORDER BY relative_path"
        ).fetchall()
        relative_paths = [row[0] for row in rows]
        assert relative_paths == sorted(relative_paths)
        assert len(relative_paths) == len(set(relative_paths))
        assert relative_paths == [
            ".",
            "empty",
            *[f"file-{index}.bin" for index in range(6)],
            "folder",
            "folder/nested-empty",
        ]
        directory_evidence = connection.execute(
            "SELECT relative_path, entry_kind, is_empty, read_outcome "
            "FROM directory_evidence ORDER BY relative_path"
        ).fetchall()
        assert directory_evidence == [
            (".", "directory", 0, "successful"),
            ("empty", "directory", 1, "successful"),
            ("folder", "directory", 0, "successful"),
            ("folder/nested-empty", "directory", 1, "successful"),
        ]

    with sqlite3.connect(
        Path(control_result["analysis_run"]) / "analysis.sqlite3"
    ) as connection:
        control_directory_evidence = connection.execute(
            "SELECT relative_path, entry_kind, is_empty, read_outcome "
            "FROM directory_evidence ORDER BY relative_path"
        ).fetchall()
        control_identities = connection.execute(
            "SELECT relative_path, algorithm, algorithm_version, byte_size, digest, "
            "read_outcome FROM content_identities ORDER BY relative_path"
        ).fetchall()
    assert directory_evidence == control_directory_evidence
    with sqlite3.connect(database_path) as connection:
        resumed_identities = connection.execute(
            "SELECT relative_path, algorithm, algorithm_version, byte_size, digest, "
            "read_outcome FROM content_identities ORDER BY relative_path"
        ).fetchall()
    assert resumed_identities == control_identities

    second_resume = run_cli(tmp_path, "resume", str(analysis_run_path))
    assert second_resume.returncode == 1
    assert "already complete" in second_resume.stderr


def test_resume_refuses_a_retired_hashing_phase_run(tmp_path: Path) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "first.txt").write_bytes(b"same content")
    (selected_root / "second.txt").write_bytes(b"same content")
    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    analysis_run_path = Path(json.loads(scan.stdout)["analysis_run"])
    with sqlite3.connect(analysis_run_path / "analysis.sqlite3") as connection:
        connection.execute(
            "UPDATE analysis_runs SET schema_version = 6, status = 'hashing'"
        )

    resumed = run_cli(tmp_path, "resume", str(analysis_run_path))

    assert resumed.returncode == 1
    assert "unsupported clean-cutover evidence schema" in resumed.stderr
    report = run_cli(tmp_path, "report", str(analysis_run_path))
    assert report.returncode == 1
    assert "unsupported clean-cutover evidence schema" in report.stderr


def test_scan_records_root_and_empty_directory_evidence_in_the_report(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "empty").mkdir()
    non_empty = selected_root / "non-empty"
    non_empty.mkdir()
    (non_empty / "nested-empty").mkdir()
    (non_empty / "note.txt").write_text("evidence", encoding="utf-8")

    scan = run_cli(tmp_path, "scan", str(selected_root))

    assert scan.returncode == 0, scan.stderr
    result = json.loads(scan.stdout)
    database = Path(result["analysis_run"]) / "analysis.sqlite3"
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT relative_path, entry_kind, is_empty, read_outcome "
            "FROM directory_evidence ORDER BY relative_path"
        ).fetchall()

    assert rows == [
        (".", "directory", 0, "successful"),
        ("empty", "directory", 1, "successful"),
        ("non-empty", "directory", 0, "successful"),
        ("non-empty/nested-empty", "directory", 1, "successful"),
    ]

    report = run_cli(tmp_path, "report", result["analysis_run"], "--detail", "full")

    assert report.returncode == 0, report.stderr
    assert "**Structural evidence version:** 1" in report.stdout
    assert "**Directories:** 4" in report.stdout
    assert "**Empty directories:** 2" in report.stdout
    assert "| `.` | directory | no | successful |" in report.stdout
    assert "| `empty` | directory | yes | successful |" in report.stdout


def test_report_persists_and_explains_identical_directory_components(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    first = selected_root / "first-copy"
    second = selected_root / "second-copy"
    first.mkdir(parents=True)
    second.mkdir()
    for root in (first, second):
        (root / "nested").mkdir()
        (root / "nested" / "record.txt").write_bytes(b"proven content")
        (root / "empty").mkdir()
        for region in ("shared-region-one", "shared-region-two"):
            (root / region).mkdir()
            (root / region / "anchor.txt").write_text(region)

    scan = run_cli(tmp_path, "scan", str(selected_root))

    assert scan.returncode == 0, scan.stderr
    report = run_cli(
        tmp_path, "report", json.loads(scan.stdout)["analysis_run"], "--detail", "full"
    )
    assert report.returncode == 0, report.stderr
    assert "**Structural candidates:**" in report.stdout
    assert "**Completed structural comparisons:**" in report.stdout
    assert "## Exact Directory Identity Components" in report.stdout
    assert "**Classification:** exact-identity component" in report.stdout
    assert "**Member roots:** `first-copy`, `second-copy`" in report.stdout
    assert "**Canonical Directory Root:** `first-copy`" in report.stdout
    assert "shallowest relative path, then lexical relative-path order" in report.stdout


def test_structural_report_classifies_containment_compatibility_and_conflict(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    trees = {name: selected_root / name for name in ("a", "b", "c", "d")}
    for tree in trees.values():
        tree.mkdir()
        for region in ("shared-region-one", "shared-region-two"):
            (tree / region).mkdir()
            (tree / region / "anchor.txt").write_text(region)
    (trees["a"] / "shared.txt").write_bytes(b"shared")
    (trees["b"] / "shared.txt").write_bytes(b"shared")
    (trees["b"] / "common.txt").write_bytes(b"common")
    (trees["c"] / "common.txt").write_bytes(b"common")
    (trees["d"] / "common.txt").write_bytes(b"common")
    (trees["b"] / "only-b.txt").write_bytes(b"only b")
    (trees["c"] / "shared.txt").write_bytes(b"shared")
    (trees["c"] / "only-c.txt").write_bytes(b"only c")
    (trees["d"] / "shared.txt").write_bytes(b"different")

    scan = run_cli(tmp_path, "scan", str(selected_root))

    assert scan.returncode == 0, scan.stderr
    report = run_cli(
        tmp_path, "report", json.loads(scan.stdout)["analysis_run"], "--detail", "full"
    )
    assert report.returncode == 0, report.stderr
    assert "strict-subset" in report.stdout
    assert "union-compatible" in report.stdout
    assert "conflicting" in report.stdout
    assert "**Structural Conflicts:**" in report.stdout
    assert "`shared.txt`" in report.stdout
