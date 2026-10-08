from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from tests.test_cli import run_cli, tree_digest


def _write_shared_regions(root: Path) -> None:
    for region in ("shared-region-one", "shared-region-two"):
        (root / region).mkdir(parents=True, exist_ok=True)
        (root / region / "anchor.txt").write_text(f"shared evidence for {region}\n")


def test_plan_refuses_incomplete_analysis_run(tmp_path: Path) -> None:
    from filesystem_organizer import analysis_run

    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "one.bin").write_bytes(b"one")
    output_root = tmp_path / "output"

    scan = run_cli(
        tmp_path, "scan", str(selected_root), "--output-root", str(output_root)
    )
    assert scan.returncode == 0, scan.stderr
    result = json.loads(scan.stdout)
    run_path = Path(result["analysis_run"])
    database_path = run_path / analysis_run.DATABASE_NAME
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE analysis_runs SET status = 'hashing' WHERE run_id = ?",
            (result["run_id"],),
        )
        connection.commit()

    plan = run_cli(tmp_path, "plan", str(run_path))

    assert plan.returncode == 1
    assert "Analysis Run is not complete" in plan.stderr


def test_plan_refuses_a_pre_m2_analysis_run_without_directory_evidence(
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
            "UPDATE analysis_runs SET schema_version = 3 WHERE run_id = ?",
            (result["run_id"],),
        )

    plan = run_cli(tmp_path, "plan", result["analysis_run"])

    assert plan.returncode == 1
    assert "does not contain directory evidence" in plan.stderr
    assert "create a new Analysis Run" in plan.stderr


def test_plan_report_refuses_a_legacy_analysis_run(tmp_path: Path) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "one.bin").write_bytes(b"one")
    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        connection.execute("UPDATE analysis_runs SET schema_version = 6")

    report = run_cli(tmp_path, "plan-report", str(run_path))

    assert report.returncode == 1
    assert "unsupported clean-cutover evidence schema" in report.stderr


def test_plan_does_not_group_unrelated_trees_from_one_shared_file(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    for name in ("alpha", "beta"):
        root = selected_root / name
        root.mkdir(parents=True)
        (root / "common.txt").write_bytes(b"same")
        (root / "Dockerfile").write_text(f"FROM {name}\n")

    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])

    report = run_cli(tmp_path, "report", str(run_path), "--detail", "full")
    assert report.returncode == 0, report.stderr
    assert "### `alpha` ↔ `beta`" not in report.stdout

    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_id = str(json.loads(plan.stdout)["plan_id"])
    plan_report = run_cli(
        tmp_path, "plan-report", str(run_path), "--plan-id", plan_id, "--detail", "full"
    )
    assert plan_report.returncode == 0, plan_report.stderr
    assert "## Lossless Conflict Projections" not in plan_report.stdout


@pytest.mark.parametrize("unreadable_roots", [("alpha",), ("alpha", "beta")])
def test_unreadable_files_do_not_strengthen_structural_evidence(
    tmp_path: Path, unreadable_roots: tuple[str, ...]
) -> None:
    selected_root = tmp_path / "backup"
    unreadable_paths = []
    for name in ("alpha", "beta"):
        root = selected_root / name
        _write_shared_regions(root)
        if name in unreadable_roots:
            for index in range(3):
                path = root / f"unreadable-{index}.txt"
                path.write_text(f"unproven content in {name}: {index}")
                path.chmod(0)
                unreadable_paths.append(path)
    try:
        scan = run_cli(tmp_path, "scan", str(selected_root))
        assert scan.returncode == 0, scan.stderr
        run_path = json.loads(scan.stdout)["analysis_run"]
        report = run_cli(tmp_path, "report", run_path, "--detail", "full")
        assert report.returncode == 0, report.stderr
        assert "unreadable-0.txt" in report.stdout
        assert "### `alpha` ↔ `beta`" not in report.stdout
        assert "**Member roots:** `alpha`, `beta`" not in report.stdout
        plan = run_cli(tmp_path, "plan", run_path)
        assert plan.returncode == 0, plan.stderr
        plan_report = run_cli(tmp_path, "plan-report", run_path, "--detail", "full")
        assert plan_report.returncode == 0, plan_report.stderr
        assert "**Participating Directory Trees:** `alpha`, `beta`" not in plan_report.stdout
    finally:
        for path in unreadable_paths:
            path.chmod(0o600)


def test_plan_does_not_group_shared_templates_without_independent_boundaries(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    for name in ("alpha", "beta"):
        root = selected_root / name
        root.mkdir(parents=True)
        for index in range(4):
            (root / f"template-{index}.txt").write_text(f"shared template {index}\n")
        (root / "version.txt").write_text(f"version from {name}\n")

    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])

    report = run_cli(tmp_path, "report", str(run_path), "--detail", "full")
    assert report.returncode == 0, report.stderr
    assert "### `alpha` ↔ `beta`" not in report.stdout

    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_id = str(json.loads(plan.stdout)["plan_id"])
    plan_report = run_cli(
        tmp_path, "plan-report", str(run_path), "--plan-id", plan_id, "--detail", "full"
    )
    assert plan_report.returncode == 0, plan_report.stderr
    assert "## Lossless Conflict Projections" not in plan_report.stdout


def test_plan_does_not_group_exact_flat_trees_without_boundary_evidence(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    for name in ("alpha", "beta"):
        root = selected_root / name
        (root / "records").mkdir(parents=True)
        (root / "metadata.txt").write_bytes(b"identical metadata")
        (root / "records" / ".keep").write_bytes(b"")

    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])

    report = run_cli(tmp_path, "report", str(run_path), "--detail", "full")
    assert report.returncode == 0, report.stderr
    assert "## Exact Directory Identity Components\n\nNone." in report.stdout

    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_id = str(json.loads(plan.stdout)["plan_id"])
    plan_report = run_cli(
        tmp_path, "plan-report", str(run_path), "--plan-id", plan_id, "--detail", "full"
    )
    assert plan_report.returncode == 0, plan_report.stderr
    assert "## Structural Unions" not in plan_report.stdout


def test_plan_projects_structural_union_as_files_and_explicit_directories(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    for name in ("first", "second"):
        root = selected_root / name
        (root / "empty").mkdir(parents=True)
        (root / "nested").mkdir()
        (root / "nested" / "shared.txt").write_bytes(b"same")
        _write_shared_regions(root)
    (selected_root / "second" / "nested" / "extra.txt").write_bytes(b"extra")

    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_id = str(json.loads(plan.stdout)["plan_id"])

    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        entries = connection.execute(
            "SELECT entry_kind, source_relative_path, output_relative_path FROM "
            "plan_output_entries WHERE plan_id = ? ORDER BY entry_index",
            (plan_id,),
        ).fetchall()

    assert ("directory", None, "second") in entries
    assert ("directory", None, "second/empty") in entries
    assert ("file", "first/nested/shared.txt", "second/nested/shared.txt") in entries
    assert ("file", "second/nested/extra.txt", "second/nested/extra.txt") in entries

    report = run_cli(
        tmp_path, "plan-report", str(run_path), "--plan-id", plan_id, "--detail", "full"
    )
    assert report.returncode == 0, report.stderr
    operations_section = report.stdout.split(
        "## Consolidation Plan Operations", maxsplit=1
    )[1].split("## Plan Output Entries", maxsplit=1)[0]
    assert (
        "| `first/nested/shared.txt` | `second/nested/shared.txt` |"
        in operations_section
    )
    assert (
        "| `first/nested/shared.txt` | `first/nested/shared.txt` |"
        not in operations_section
    )


def test_version_three_plan_persists_structural_union_projection(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    for name in ("first", "second"):
        root = selected_root / name
        (root / "nested").mkdir(parents=True)
        (root / "nested" / "shared.txt").write_bytes(b"same")
        _write_shared_regions(root)

    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_id = str(json.loads(plan.stdout)["plan_id"])

    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        union = connection.execute(
            "SELECT canonical_root, classification, canonical_reason "
            "FROM plan_projection_unions WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        roots = connection.execute(
            "SELECT root_relative_path FROM plan_projection_union_roots "
            "WHERE plan_id = ? ORDER BY root_relative_path",
            (plan_id,),
        ).fetchall()
        connection.execute(
            "DELETE FROM directory_relationships WHERE run_id = "
            "(SELECT run_id FROM consolidation_plans WHERE plan_id = ?)",
            (plan_id,),
        )
        connection.commit()

    assert union is not None
    assert union[0] == "first"
    assert union[1] == "exact-identity component"
    assert [row[0] for row in roots] == ["first", "second"]

    report = run_cli(
        tmp_path, "plan-report", str(run_path), "--plan-id", plan_id, "--detail", "full"
    )
    assert report.returncode == 0, report.stderr
    assert "**Plan Projection:** persisted" in report.stdout
    assert "### Structural Union: `first`" in report.stdout


def test_public_plan_paths_refuse_a_legacy_plan_schema(tmp_path: Path) -> None:
    selected_root = tmp_path / "backup"
    for name in ("first", "second"):
        (selected_root / name).mkdir(parents=True)
        (selected_root / name / "shared.txt").write_bytes(b"same")

    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_id = str(json.loads(plan.stdout)["plan_id"])
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        connection.execute(
            "UPDATE consolidation_plans SET schema_version = 3 WHERE plan_id = ?",
            (plan_id,),
        )
        connection.commit()

    report = run_cli(
        tmp_path, "plan-report", str(run_path), "--plan-id", plan_id, "--detail", "full"
    )
    finalize = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)

    assert report.returncode == 1
    assert "unsupported clean-cutover Consolidation Plan schema" in report.stderr
    assert finalize.returncode == 1
    assert "unsupported clean-cutover Consolidation Plan schema" in finalize.stderr


def test_plan_classifies_empty_directory_as_subset_of_populated_directory(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    first = selected_root / "first"
    second = selected_root / "second"
    (first / "nested").mkdir(parents=True)
    (second / "nested").mkdir(parents=True)
    (first / "shared.txt").write_bytes(b"same")
    (second / "shared.txt").write_bytes(b"same")
    _write_shared_regions(first)
    _write_shared_regions(second)
    (second / "nested" / "extra.txt").write_bytes(b"extra")

    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])

    report = run_cli(tmp_path, "report", str(run_path), "--detail", "full")
    assert report.returncode == 0, report.stderr
    relationship = report.stdout.split("### `first` ↔ `second`", maxsplit=1)[1]
    assert "**Classification:** strict-subset" in relationship
    assert "**Classification:** conflicting" not in relationship

    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_id = str(json.loads(plan.stdout)["plan_id"])
    finalized = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)
    assert finalized.returncode == 0, finalized.stderr
    materialized = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")
    assert materialized.returncode == 0, materialized.stderr

    destination = Path(json.loads(plan.stdout)["intended_destination"])
    assert (destination / "second" / "nested" / "extra.txt").read_bytes() == b"extra"
    assert not (destination / "first").exists()


def test_plan_projects_three_identical_directory_trees_as_one_component(
    tmp_path: Path,
) -> None:
    """Exact peers are one proven union, not three overlapping pairs."""
    selected_root = tmp_path / "backup"
    for name in ("gamma", "alpha", "beta"):
        root = selected_root / name
        (root / "empty").mkdir(parents=True)
        (root / "nested").mkdir()
        (root / "nested" / "same.txt").write_bytes(b"same")
        _write_shared_regions(root)

    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])

    report = run_cli(tmp_path, "report", str(run_path), "--detail", "full")
    assert report.returncode == 0, report.stderr
    assert "## Exact Directory Identity Components" in report.stdout
    assert "`alpha`, `beta`, `gamma`" in report.stdout
    assert report.stdout.count("`alpha`, `beta`, `gamma`") == 1
    assert "`alpha` ↔ `beta`" not in report.stdout
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        members = connection.execute(
            "SELECT digest, root_relative_path FROM directory_fingerprints "
            "WHERE root_relative_path IN ('alpha', 'beta', 'gamma') "
            "ORDER BY digest, root_relative_path"
        ).fetchall()
        candidate_count = connection.execute(
            "SELECT candidate_count FROM structural_analysis"
        ).fetchone()[0]
        exact_peer_relationships = connection.execute(
            "SELECT left_root, right_root FROM directory_relationships "
            "WHERE left_root IN ('alpha', 'beta', 'gamma') "
            "AND right_root IN ('alpha', 'beta', 'gamma')"
        ).fetchall()
    assert [member[1] for member in members] == ["alpha", "beta", "gamma"]
    assert len({member[0] for member in members}) == 1
    assert candidate_count == 9
    assert exact_peer_relationships == []

    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_id = str(json.loads(plan.stdout)["plan_id"])
    plan_report = run_cli(
        tmp_path, "plan-report", str(run_path), "--plan-id", plan_id, "--detail", "full"
    )
    assert plan_report.returncode == 0, plan_report.stderr
    assert "**Classification:** exact-identity component" in plan_report.stdout
    assert (
        "**Participating Directory Trees:** `alpha`, `beta`, `gamma`"
        in plan_report.stdout
    )
    assert "**Canonical Directory Root:** `alpha`" in plan_report.stdout
    assert (
        "**Canonical selection reason:** shallowest relative path, then lexical relative-path order"
        in plan_report.stdout
    )
    assert "`alpha/nested/same.txt` → `alpha/nested/same.txt`" in plan_report.stdout
    assert "- `alpha/empty`" in plan_report.stdout
    finalized = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)
    assert finalized.returncode == 0, finalized.stderr
    materialized = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")
    assert materialized.returncode == 0, materialized.stderr

    destination = Path(json.loads(plan.stdout)["intended_destination"])
    assert (destination / "alpha" / "empty").is_dir()
    assert (destination / "alpha" / "nested" / "same.txt").read_bytes() == b"same"
    assert not (destination / "beta").exists()
    assert not (destination / "gamma").exists()


def test_scan_coalesces_exact_component_before_candidate_expansion(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    for name in ("first", "second"):
        (selected_root / name).mkdir(parents=True)
        (selected_root / name / "shared.txt").write_bytes(b"same")
        (selected_root / name / "component.txt").write_bytes(b"component")
        _write_shared_regions(selected_root / name)
    (selected_root / "peer").mkdir()
    (selected_root / "peer" / "shared.txt").write_bytes(b"same")
    (selected_root / "peer" / "additional.txt").write_bytes(b"additional")

    scan = run_cli(tmp_path, "scan", str(selected_root))

    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        candidate_count = connection.execute(
            "SELECT candidate_count FROM structural_analysis"
        ).fetchone()[0]
        relationships = connection.execute(
            "SELECT left_root, right_root FROM directory_relationships "
            "ORDER BY left_root, right_root"
        ).fetchall()

    # Candidate evidence is now retained for the metadata pairs that triggered
    # proof, including bounded sibling-tree pairs.  It is not a Cartesian
    # expansion of every structurally similar directory tree.
    assert candidate_count == 6
    assert relationships == []


def test_plan_keeps_single_anchor_compatible_candidates_separate(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    for name, unique in (
        ("alpha", "alpha.txt"),
        ("beta", "beta.txt"),
        ("gamma", "gamma.txt"),
    ):
        root = selected_root / name
        root.mkdir(parents=True)
        (root / "shared.txt").write_bytes(b"same")
        (root / unique).write_bytes(name.encode())

    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])

    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_id = str(json.loads(plan.stdout)["plan_id"])
    report = run_cli(
        tmp_path, "plan-report", str(run_path), "--plan-id", plan_id, "--detail", "full"
    )
    assert report.returncode == 0, report.stderr
    assert "**Classification:** multi-tree union" not in report.stdout
    assert "## Structural Unions" not in report.stdout

    finalized = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)
    assert finalized.returncode == 0, finalized.stderr
    materialized = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")
    assert materialized.returncode == 0, materialized.stderr

    destination = Path(json.loads(plan.stdout)["intended_destination"])
    assert (destination / "alpha" / "alpha.txt").read_bytes() == b"alpha"
    assert (destination / "beta" / "beta.txt").read_bytes() == b"beta"
    assert (destination / "gamma" / "gamma.txt").read_bytes() == b"gamma"
    assert len(list(destination.rglob("shared.txt"))) == 1
    assert (destination / "beta").is_dir()
    assert (destination / "gamma").is_dir()


def test_lossless_conflict_projection_keeps_unique_newest_at_conventional_path(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    older_root = selected_root / "older-tree"
    newer_root = selected_root / "newer-tree"
    for root in (older_root, newer_root):
        root.mkdir(parents=True)
        (root / "anchor.txt").write_bytes(b"shared anchor")
        (root / "second-anchor.txt").write_bytes(b"second shared anchor")
        _write_shared_regions(root)
    older = older_root / "settings.json"
    newer = newer_root / "settings.json"
    older.write_bytes(b"old settings")
    newer.write_bytes(b"new settings")
    os.utime(older, ns=(1_700_000_000_000_000_000,) * 2)
    os.utime(newer, ns=(1_800_000_000_000_000_000,) * 2)

    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_result = json.loads(plan.stdout)
    plan_id = str(plan_result["plan_id"])

    report = run_cli(
        tmp_path, "plan-report", str(run_path), "--plan-id", plan_id, "--detail", "full"
    )
    assert report.returncode == 0, report.stderr
    assert "## Lossless Conflict Projections" in report.stdout
    assert "`newer-tree/settings.json` → `newer-tree/settings.json`" in report.stdout
    assert (
        "`older-tree/settings.json` → "
        "`__fso-conflicts__/older-tree/settings.json`" in report.stdout
    )
    assert "uniquely newest modification timestamp" in report.stdout
    assert "source provenance: older-tree/settings.json" in report.stdout

    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        mappings = connection.execute(
            "SELECT source_relative_path, output_relative_path, disposition, reason "
            "FROM plan_conflict_projections WHERE plan_id = ? "
            "ORDER BY source_relative_path",
            (plan_id,),
        ).fetchall()
    assert mappings == [
        (
            "newer-tree/settings.json",
            "newer-tree/settings.json",
            "primary",
            "uniquely newest modification timestamp",
        ),
        (
            "older-tree/settings.json",
            "__fso-conflicts__/older-tree/settings.json",
            "variant",
            "older conflicting variant; source provenance: older-tree/settings.json",
        ),
    ]

    finalized = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)
    assert finalized.returncode == 0, finalized.stderr
    materialized = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")
    assert materialized.returncode == 0, materialized.stderr
    destination = Path(plan_result["intended_destination"])
    assert (
        destination / "newer-tree" / "settings.json"
    ).read_bytes() == b"new settings"
    assert (destination / "newer-tree" / "anchor.txt").read_bytes() == b"shared anchor"
    assert not (
        destination / "__fso-conflicts__" / "older-tree" / "anchor.txt"
    ).exists()
    assert (
        destination / "__fso-conflicts__" / "older-tree" / "settings.json"
    ).read_bytes() == b"old settings"
    assert not (destination / "older-tree").exists()


def test_lossless_conflict_projection_keeps_tied_variants_only_in_namespace(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    for name, contents in (("alpha", b"alpha"), ("beta", b"beta")):
        root = selected_root / name
        root.mkdir(parents=True)
        (root / "anchor.txt").write_bytes(b"shared anchor")
        (root / "second-anchor.txt").write_bytes(b"second shared anchor")
        _write_shared_regions(root)
        conflict = root / "settings.json"
        conflict.write_bytes(contents)
        os.utime(conflict, ns=(1_800_000_000_000_000_000,) * 2)

    scan = run_cli(tmp_path, "scan", str(selected_root))
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_result = json.loads(plan.stdout)
    plan_id = str(plan_result["plan_id"])

    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        mappings = connection.execute(
            "SELECT source_relative_path, output_relative_path, disposition "
            "FROM plan_conflict_projections WHERE plan_id = ? "
            "ORDER BY source_relative_path",
            (plan_id,),
        ).fetchall()
    assert mappings == [
        ("alpha/settings.json", "__fso-conflicts__/alpha/settings.json", "variant"),
        ("beta/settings.json", "__fso-conflicts__/beta/settings.json", "variant"),
    ]

    report = run_cli(
        tmp_path, "plan-report", str(run_path), "--plan-id", plan_id, "--detail", "full"
    )
    assert "without unique newest timestamp" in report.stdout
    assert (
        run_cli(
            tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id
        ).returncode
        == 0
    )
    materialized = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")
    assert materialized.returncode == 0, materialized.stderr
    destination = Path(plan_result["intended_destination"])
    assert not (destination / "alpha" / "settings.json").exists()
    assert (
        destination / "__fso-conflicts__" / "alpha" / "settings.json"
    ).read_bytes() == b"alpha"
    assert (
        destination / "__fso-conflicts__" / "beta" / "settings.json"
    ).read_bytes() == b"beta"


def test_lossless_conflict_projection_does_not_choose_primary_without_timestamp(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    for name, contents in (("alpha", b"alpha"), ("beta", b"beta")):
        root = selected_root / name
        root.mkdir(parents=True)
        (root / "anchor.txt").write_bytes(b"shared anchor")
        (root / "second-anchor.txt").write_bytes(b"second shared anchor")
        _write_shared_regions(root)
        (root / "settings.json").write_bytes(contents)

    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        connection.execute(
            "UPDATE inventory_entries SET modified_ns = NULL "
            "WHERE relative_path = 'beta/settings.json'"
        )

    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_id = str(json.loads(plan.stdout)["plan_id"])
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        mappings = connection.execute(
            "SELECT output_relative_path, disposition FROM plan_conflict_projections "
            "WHERE plan_id = ? ORDER BY output_relative_path",
            (plan_id,),
        ).fetchall()
    assert mappings == [
        ("__fso-conflicts__/alpha/settings.json", "variant"),
        ("__fso-conflicts__/beta/settings.json", "variant"),
    ]

    report = run_cli(
        tmp_path, "plan-report", str(run_path), "--plan-id", plan_id, "--detail", "full"
    )
    assert report.returncode == 0, report.stderr
    assert "without unique newest timestamp" in report.stdout


def test_lossless_conflict_projection_namespaces_file_directory_variants(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    alpha = selected_root / "alpha"
    beta = selected_root / "beta"
    alpha.mkdir(parents=True)
    (beta / "node" / "nested").mkdir(parents=True)
    for root in (alpha, beta):
        (root / "anchor.txt").write_bytes(b"shared anchor")
        (root / "second-anchor.txt").write_bytes(b"second shared anchor")
        _write_shared_regions(root)
    (alpha / "node").write_bytes(b"file variant")
    (beta / "node" / "nested" / "child.txt").write_bytes(b"directory variant")

    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))

    assert plan.returncode == 0, plan.stderr
    plan_result = json.loads(plan.stdout)
    plan_id = str(plan_result["plan_id"])
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        mappings = connection.execute(
            "SELECT source_relative_path, output_relative_path, entry_kind, disposition "
            "FROM plan_conflict_projections WHERE plan_id = ? "
            "ORDER BY source_relative_path",
            (plan_id,),
        ).fetchall()
    assert mappings == [
        ("alpha/node", "__fso-conflicts__/alpha/node", "file", "variant"),
        ("beta/node", "__fso-conflicts__/beta/node", "directory", "variant"),
        (
            "beta/node/nested",
            "__fso-conflicts__/beta/node/nested",
            "directory",
            "variant",
        ),
        (
            "beta/node/nested/child.txt",
            "__fso-conflicts__/beta/node/nested/child.txt",
            "file",
            "variant",
        ),
    ]
    report = run_cli(
        tmp_path, "plan-report", str(run_path), "--plan-id", plan_id, "--detail", "full"
    )
    assert "file-versus-directory conflicting variant" in report.stdout

    assert (
        run_cli(
            tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id
        ).returncode
        == 0
    )
    materialized = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")
    assert materialized.returncode == 0, materialized.stderr
    destination = Path(plan_result["intended_destination"])
    assert not (destination / "alpha" / "node").exists()
    assert (
        destination / "__fso-conflicts__" / "alpha" / "node"
    ).read_bytes() == b"file variant"
    assert (
        destination / "__fso-conflicts__" / "beta" / "node" / "nested" / "child.txt"
    ).read_bytes() == b"directory variant"


def test_lossless_conflict_projection_validates_complete_compatible_bridge(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    for name in ("alpha", "bridge", "gamma"):
        root = selected_root / name
        root.mkdir(parents=True)
        (root / "anchor.txt").write_bytes(b"shared anchor")
        (root / "second-anchor.txt").write_bytes(b"second shared anchor")
        _write_shared_regions(root)
    alpha_settings = selected_root / "alpha" / "settings.json"
    gamma_settings = selected_root / "gamma" / "settings.json"
    alpha_settings.write_bytes(b"alpha settings")
    gamma_settings.write_bytes(b"gamma settings")
    os.utime(alpha_settings, ns=(1_700_000_000_000_000_000,) * 2)
    os.utime(gamma_settings, ns=(1_800_000_000_000_000_000,) * 2)

    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        relationships = connection.execute(
            "SELECT left_root, right_root, classification FROM directory_relationships "
            "WHERE left_root IN ('alpha', 'bridge', 'gamma') "
            "AND right_root IN ('alpha', 'bridge', 'gamma') "
            "ORDER BY left_root, right_root"
        ).fetchall()
        connection.execute(
            "DELETE FROM relationship_conflicts "
            "WHERE left_root = 'alpha' AND right_root = 'gamma'"
        )
        connection.execute(
            "DELETE FROM directory_relationships "
            "WHERE left_root = 'alpha' AND right_root = 'gamma'"
        )
    assert [row[2] for row in relationships] == [
        "strict-superset",
        "conflicting",
        "strict-subset",
    ]

    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_id = str(json.loads(plan.stdout)["plan_id"])
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        roots = connection.execute(
            "SELECT root.root_relative_path "
            "FROM plan_projection_conflict_group_roots AS root "
            "JOIN plan_projection_conflict_groups AS projection "
            "ON projection.plan_id = root.plan_id "
            "AND projection.group_index = root.group_index "
            "WHERE root.plan_id = ? "
            "ORDER BY root.root_relative_path",
            (plan_id,),
        ).fetchall()
        mappings = connection.execute(
            "SELECT source_relative_path, output_relative_path, disposition "
            "FROM plan_conflict_projections WHERE plan_id = ? "
            "ORDER BY source_relative_path",
            (plan_id,),
        ).fetchall()
    assert roots == [("alpha",), ("bridge",), ("gamma",)]
    assert mappings == [
        ("alpha/settings.json", "__fso-conflicts__/alpha/settings.json", "variant"),
        ("gamma/settings.json", "alpha/settings.json", "primary"),
    ]

    report = run_cli(
        tmp_path,
        "plan-report",
        str(run_path),
        "--plan-id",
        plan_id,
        "--detail",
        "full",
    )
    assert (
        "**Participating Directory Trees:** `alpha`, `bridge`, `gamma`" in report.stdout
    )
    assert "### Lossless Conflict Projection: `alpha`" in report.stdout
    assert "**Classification:** lossless conflict projection" not in report.stdout


def test_lossless_conflict_projection_collapses_identical_variants_before_ranking(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    for name, contents, modified_ns in (
        ("alpha", b"new settings", 1_800_000_000_000_000_000),
        ("beta", b"new settings", 1_800_000_000_000_000_000),
        ("gamma", b"old settings", 1_700_000_000_000_000_000),
    ):
        root = selected_root / name
        root.mkdir(parents=True)
        (root / "anchor.txt").write_bytes(b"shared anchor")
        (root / "second-anchor.txt").write_bytes(b"second shared anchor")
        _write_shared_regions(root)
        settings = root / "settings.json"
        settings.write_bytes(contents)
        os.utime(settings, ns=(modified_ns, modified_ns))

    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))

    assert plan.returncode == 0, plan.stderr
    plan_result = json.loads(plan.stdout)
    plan_id = str(plan_result["plan_id"])
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        mappings = connection.execute(
            "SELECT source_relative_path, output_relative_path, disposition "
            "FROM plan_conflict_projections WHERE plan_id = ? "
            "ORDER BY source_relative_path",
            (plan_id,),
        ).fetchall()
    assert mappings == [
        ("alpha/settings.json", "alpha/settings.json", "primary"),
        ("beta/settings.json", "alpha/settings.json", "collapsed"),
        ("gamma/settings.json", "__fso-conflicts__/gamma/settings.json", "variant"),
    ]

    report = run_cli(tmp_path, "plan-report", str(run_path), "--plan-id", plan_id)
    assert (
        "identical full content identity collapsed to retained variant" in report.stdout
    )
    assert (
        run_cli(
            tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id
        ).returncode
        == 0
    )
    materialized = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")
    assert materialized.returncode == 0, materialized.stderr
    destination = Path(plan_result["intended_destination"])
    assert (destination / "alpha" / "settings.json").read_bytes() == b"new settings"
    assert (
        destination / "__fso-conflicts__" / "gamma" / "settings.json"
    ).read_bytes() == b"old settings"
    assert not (destination / "__fso-conflicts__" / "beta" / "settings.json").exists()


def test_lossless_conflict_projection_chooses_collision_safe_reserved_namespace(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    for name, contents in (("alpha", b"old"), ("beta", b"new")):
        root = selected_root / name
        root.mkdir(parents=True)
        (root / "anchor.txt").write_bytes(b"shared anchor")
        (root / "second-anchor.txt").write_bytes(b"second shared anchor")
        _write_shared_regions(root)
        (root / "settings.json").write_bytes(contents)
    occupied = selected_root / "__fso-conflicts__" / "alpha"
    occupied.mkdir(parents=True)
    (occupied / "settings.json").write_bytes(b"user-owned namespace")

    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))

    assert plan.returncode == 0, plan.stderr
    plan_result = json.loads(plan.stdout)
    plan_id = str(plan_result["plan_id"])
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        variant_paths = connection.execute(
            "SELECT output_relative_path FROM plan_conflict_projections "
            "WHERE plan_id = ? AND disposition = 'variant' ORDER BY output_relative_path",
            (plan_id,),
        ).fetchall()
    assert variant_paths
    assert all(path[0].startswith("__fso-conflicts__-1/") for path in variant_paths)

    assert (
        run_cli(
            tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id
        ).returncode
        == 0
    )
    materialized = run_cli(tmp_path, "materialize", str(run_path), plan_id, "--yes")
    assert materialized.returncode == 0, materialized.stderr
    destination = Path(plan_result["intended_destination"])
    assert (
        destination / "__fso-conflicts__" / "alpha" / "settings.json"
    ).read_bytes() == b"user-owned namespace"
    assert any(
        candidate.read_bytes() in {b"old", b"new"}
        for candidate in (destination / "__fso-conflicts__-1").rglob("settings.json")
    )


def test_plan_report_explains_newly_empty_wrapper_pruning(tmp_path: Path) -> None:
    selected_root = tmp_path / "backup"
    for root in (selected_root / "wrapper" / "first", selected_root / "second"):
        root.mkdir(parents=True)
        (root / "same.txt").write_bytes(b"same")
        _write_shared_regions(root)

    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr

    report = run_cli(
        tmp_path,
        "plan-report",
        str(run_path),
        "--plan-id",
        json.loads(plan.stdout)["plan_id"],
        "--detail",
        "full",
    )
    assert report.returncode == 0, report.stderr
    assert "Redundant peer roots omitted" in report.stdout
    assert "`wrapper/first`" in report.stdout
    assert (
        "Newly empty wrapper ancestors omitted after peer pruning: `wrapper`."
        in report.stdout
    )


def test_plan_keeps_identical_empty_directories_separate_without_context(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    for name in ("alpha", "beta", "gamma"):
        (selected_root / name).mkdir(parents=True)

    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    report = run_cli(tmp_path, "report", str(run_path), "--detail", "full")
    assert report.returncode == 0, report.stderr
    assert "## Exact Directory Identity Components\n\nNone." in report.stdout

    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_id = str(json.loads(plan.stdout)["plan_id"])
    plan_report = run_cli(
        tmp_path, "plan-report", str(run_path), "--plan-id", plan_id, "--detail", "full"
    )
    assert plan_report.returncode == 0, plan_report.stderr
    assert "## Structural Unions" not in plan_report.stdout


def test_plan_report_shows_complete_structural_approval_evidence(
    tmp_path: Path,
) -> None:
    selected_root = tmp_path / "backup"
    for name in ("first", "second"):
        root = selected_root / name
        (root / "empty").mkdir(parents=True)
        (root / "nested").mkdir()
        (root / "nested" / "same.txt").write_bytes(b"same")
        _write_shared_regions(root)
        if name == "first":
            (root / "unverified-link").symlink_to("nested/same.txt")
            (root / "unverified-two").symlink_to("nested/same.txt")
    (selected_root / "second" / "nested" / "extra.txt").write_bytes(b"extra")
    for name, contents in (("conflict-left", b"left"), ("conflict-right", b"right")):
        root = selected_root / name
        root.mkdir()
        (root / "same-name.txt").write_bytes(contents)
        (root / "shared.txt").write_bytes(b"shared")
        (root / "second-shared.txt").write_bytes(b"second shared")
        _write_shared_regions(root)
        (root / "bad-link").symlink_to("shared.txt")

    scan = run_cli(tmp_path, "scan", str(selected_root))
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_id = str(json.loads(plan.stdout)["plan_id"])

    draft = run_cli(
        tmp_path, "plan-report", str(run_path), "--plan-id", plan_id, "--detail", "full"
    )
    assert draft.returncode == 0, draft.stderr
    assert "## Structural Unions" in draft.stdout
    assert "**Classification:** strict-subset" in draft.stdout
    assert "**Participating Directory Trees:** `first`, `second`" in draft.stdout
    assert "**Canonical Directory Root:** `second`" in draft.stdout
    assert "**Canonical selection reason:** strict superset" in draft.stdout
    assert "### Retained Copyable Descendants" in draft.stdout
    assert "`first/nested/same.txt`" in draft.stdout
    assert "`second/nested/extra.txt`" in draft.stdout
    assert "### Explicit Empty Directories" in draft.stdout
    assert "`second/empty`" in draft.stdout
    assert "### Wrapper Pruning" in draft.stdout
    assert "`first`" in draft.stdout
    assert "## Evidence Qualifications and Omitted Entries" in draft.stdout
    assert "`first/unverified-link`" in draft.stdout
    assert "copyable counterpart: second/unverified-link" in draft.stdout
    assert (
        "**Persisted qualification paths:** `unverified-link`, `unverified-two`"
        in draft.stdout
    )
    assert "`first/unverified-two`" in draft.stdout
    assert "## Lossless Conflict Projections" in draft.stdout
    assert "## Structural Conflicts Left Separate" not in draft.stdout
    assert "`conflict-left`" in draft.stdout
    assert "`conflict-right`" in draft.stdout
    assert "`conflict-left/bad-link`" in draft.stdout
    assert "`conflict-right/bad-link`" in draft.stdout

    finalized = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)
    assert finalized.returncode == 0, finalized.stderr
    final = run_cli(
        tmp_path, "plan-report", str(run_path), "--plan-id", plan_id, "--detail", "full"
    )
    assert final.returncode == 0, final.stderr
    assert (
        final.stdout.replace("**Status:** finalized", "**Status:** draft")
        == draft.stdout
    )


def test_plan_and_plan_report_on_example_fixture(tmp_path: Path) -> None:
    from filesystem_organizer import analysis_run
    from tests.example_fixture import copy_prepared_source

    selected_root = copy_prepared_source(tmp_path / "selected-backup-root")
    before = tree_digest(selected_root)
    output_root = tmp_path / "output"

    scan = run_cli(
        tmp_path, "scan", str(selected_root), "--output-root", str(output_root)
    )
    assert scan.returncode == 0, scan.stderr
    scan_result = json.loads(scan.stdout)
    run_path = Path(scan_result["analysis_run"])

    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    plan_result = json.loads(plan.stdout)
    assert plan_result["run_id"] == scan_result["run_id"]
    assert plan_result["snapshot_id"] == scan_result["snapshot_id"]
    assert plan_result["schema_version"] == 9
    assert plan_result["plan_id"]
    assert plan_result["operation_count"] == 30
    expected_destination = str(
        output_root.resolve() / "materialized" / scan_result["run_id"]
    )
    assert plan_result["intended_destination"] == expected_destination

    database_path = run_path / analysis_run.DATABASE_NAME
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        successful_entries = {
            row["relative_path"]
            for row in connection.execute(
                "SELECT relative_path FROM inventory_entries "
                "WHERE entry_kind = 'regular-file' AND read_outcome = 'successful'"
            ).fetchall()
        }
        hashed_entries = {
            row["relative_path"]
            for row in connection.execute(
                "SELECT relative_path FROM content_identities "
                "WHERE read_outcome = 'successful'"
            ).fetchall()
        }
        # The Plan only consumes scan evidence; it never adds identities.
        assert hashed_entries <= successful_entries

        operations = connection.execute(
            "SELECT source_relative_path, output_relative_path, canonical_reason "
            "FROM plan_operations WHERE plan_id = ? ORDER BY operation_index",
            (plan_result["plan_id"],),
        ).fetchall()

    retained_paths = [row[0] for row in operations]
    assert retained_paths == sorted(retained_paths)
    assert retained_paths == sorted(set(retained_paths))
    assert all(source == output for source, output, _ in operations)

    excluded = {
        "snapshot-2025-07-01/Home/Desktop/link-to-annual-tax-statement",
        "snapshot-2025-07-01/Home/Desktop/unreadable.txt",
    }
    assert not excluded & set(retained_paths)
    assert "snapshot-2025-07-01/Home/Desktop/same-size-alpha.txt" in retained_paths
    assert "snapshot-2025-07-01/Home/Desktop/same-size-bravo.txt" in retained_paths
    assert (
        "snapshot-2023-11-01/Home/Projects/ai-assistant/docs/recovery-checklist.txt"
        in retained_paths
    )
    assert (
        "snapshot-2025-07-01/Home/Desktop/annual_tax_statement_2024.pdf"
        in retained_paths
    )

    reasons_by_path = {row[0]: row[2] for row in operations}
    assert (
        reasons_by_path[
            "snapshot-2023-11-01/Home/Projects/ai-assistant/docs/recovery-checklist.txt"
        ]
        == "newest modification timestamp"
    )
    assert (
        reasons_by_path[
            "snapshot-2025-07-01/Home/Desktop/annual_tax_statement_2024.pdf"
        ]
        == "newest modification timestamp"
    )
    assert (
        reasons_by_path["snapshot-2025-07-01/Home/Desktop/same-size-alpha.txt"] is None
    )
    assert (
        reasons_by_path["snapshot-2025-07-01/Home/Desktop/same-size-bravo.txt"] is None
    )

    plan_report = run_cli(tmp_path, "plan-report", str(run_path), "--detail", "full")
    assert plan_report.returncode == 0, plan_report.stderr
    assert f"**Plan ID:** `{plan_result['plan_id']}`" in plan_report.stdout
    assert f"**Run ID:** `{plan_result['run_id']}`" in plan_report.stdout
    assert "**Operation Count:** 30" in plan_report.stdout
    assert (
        "| `snapshot-2023-11-01/Home/Projects/ai-assistant/docs/recovery-checklist.txt` | `snapshot-2025-07-01/Home/Projects/ai-assistant/docs/recovery-checklist.txt` |"
        in (plan_report.stdout)
    )
    assert "Structural Union (strict-subset)" in plan_report.stdout

    explicit_plan_report = run_cli(
        tmp_path,
        "plan-report",
        str(run_path),
        "--plan-id",
        plan_result["plan_id"],
        "--detail",
        "full",
    )
    assert explicit_plan_report.returncode == 0, explicit_plan_report.stderr
    assert explicit_plan_report.stdout == plan_report.stdout

    assert tree_digest(selected_root) == before


def test_repeated_planning_produces_equivalent_ordered_operations(
    tmp_path: Path,
) -> None:
    from filesystem_organizer import analysis_run
    from tests.example_fixture import copy_prepared_source

    selected_root = copy_prepared_source(tmp_path / "selected-backup-root")
    output_root = tmp_path / "output"

    scan = run_cli(
        tmp_path, "scan", str(selected_root), "--output-root", str(output_root)
    )
    assert scan.returncode == 0, scan.stderr
    scan_result = json.loads(scan.stdout)
    run_path = Path(scan_result["analysis_run"])

    first = run_cli(tmp_path, "plan", str(run_path))
    second = run_cli(tmp_path, "plan", str(run_path))
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    first_result = json.loads(first.stdout)
    second_result = json.loads(second.stdout)
    assert first_result["plan_id"] != second_result["plan_id"]
    assert first_result["operation_count"] == second_result["operation_count"]

    database_path = run_path / analysis_run.DATABASE_NAME
    with sqlite3.connect(database_path) as connection:

        def operations_for(
            plan_id: str,
        ) -> list[tuple[str, str, int, str, str, str | None]]:
            rows = connection.execute(
                "SELECT source_relative_path, output_relative_path, expected_byte_size, "
                "algorithm, digest, canonical_reason FROM plan_operations "
                "WHERE plan_id = ? ORDER BY operation_index",
                (plan_id,),
            ).fetchall()
            return [tuple(row) for row in rows]

        first_operations = operations_for(first_result["plan_id"])
        second_operations = operations_for(second_result["plan_id"])

    assert first_operations == second_operations


def _scan_and_plan(tmp_path: Path) -> tuple[Path, dict[str, object]]:
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
    plan_result = json.loads(plan.stdout)
    return run_path, plan_result


def test_plan_override_replaces_canonical_choice_while_draft(tmp_path: Path) -> None:
    from filesystem_organizer import analysis_run

    run_path, plan_result = _scan_and_plan(tmp_path)
    plan_id = str(plan_result["plan_id"])

    override = run_cli(
        tmp_path,
        "plan-override",
        str(run_path),
        "snapshot-2025-07-01/Home/Projects/ai-assistant/docs/recovery-checklist.txt",
        "operator prefers the project copy",
        "--plan-id",
        plan_id,
    )
    assert override.returncode == 0, override.stderr
    override_result = json.loads(override.stdout)
    assert override_result["plan_id"] == plan_id
    assert override_result["source_relative_path"] == (
        "snapshot-2025-07-01/Home/Projects/ai-assistant/docs/recovery-checklist.txt"
    )
    assert (
        override_result["prior_relative_path"]
        == "snapshot-2023-11-01/Home/Projects/ai-assistant/docs/recovery-checklist.txt"
    )
    assert override_result["reason"] == "operator prefers the project copy"

    database_path = run_path / analysis_run.DATABASE_NAME
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        operations = {
            row["source_relative_path"]
            for row in connection.execute(
                "SELECT source_relative_path FROM plan_operations WHERE plan_id = ?",
                (plan_id,),
            ).fetchall()
        }
        assert (
            "snapshot-2025-07-01/Home/Projects/ai-assistant/docs/recovery-checklist.txt"
            in operations
        )
        assert (
            "snapshot-2023-11-01/Home/Projects/ai-assistant/docs/recovery-checklist.txt"
            not in operations
        )

        overridden_operation = connection.execute(
            "SELECT output_relative_path, canonical_reason FROM plan_operations "
            "WHERE plan_id = ? AND source_relative_path = ?",
            (
                plan_id,
                "snapshot-2025-07-01/Home/Projects/ai-assistant/docs/recovery-checklist.txt",
            ),
        ).fetchone()
        assert overridden_operation["output_relative_path"] == (
            "snapshot-2025-07-01/Home/Projects/ai-assistant/docs/recovery-checklist.txt"
        )
        assert (
            "operator prefers the project copy"
            in overridden_operation["canonical_reason"]
        )

        override_rows = connection.execute(
            "SELECT selected_relative_path, prior_relative_path, reason "
            "FROM plan_overrides WHERE plan_id = ?",
            (plan_id,),
        ).fetchall()
        assert len(override_rows) == 1
        assert override_rows[0]["selected_relative_path"] == (
            "snapshot-2025-07-01/Home/Projects/ai-assistant/docs/recovery-checklist.txt"
        )
        assert (
            override_rows[0]["prior_relative_path"]
            == "snapshot-2023-11-01/Home/Projects/ai-assistant/docs/recovery-checklist.txt"
        )
        assert override_rows[0]["reason"] == "operator prefers the project copy"

    plan_report = run_cli(
        tmp_path, "plan-report", str(run_path), "--plan-id", plan_id, "--detail", "full"
    )
    assert plan_report.returncode == 0, plan_report.stderr
    assert "## Canonical Copy Overrides" in plan_report.stdout
    assert (
        "snapshot-2025-07-01/Home/Projects/ai-assistant/docs/recovery-checklist.txt"
        in plan_report.stdout
    )


def test_plan_override_rejects_invalid_group_member(tmp_path: Path) -> None:
    run_path, plan_result = _scan_and_plan(tmp_path)
    plan_id = str(plan_result["plan_id"])

    not_a_duplicate = run_cli(
        tmp_path,
        "plan-override",
        str(run_path),
        "snapshot-2025-07-01/Home/Desktop/meeting-notes.txt",
        "trying to override a unique file",
        "--plan-id",
        plan_id,
    )
    assert not_a_duplicate.returncode == 1
    assert "not part of an Exact Duplicate group" in not_a_duplicate.stderr

    unknown_path = run_cli(
        tmp_path,
        "plan-override",
        str(run_path),
        "does/not/exist.txt",
        "trying to override an unknown path",
        "--plan-id",
        plan_id,
    )
    assert unknown_path.returncode == 1
    assert "Not a proven Exact Duplicate group member" in unknown_path.stderr


def test_plan_override_rejects_missing_reason(tmp_path: Path) -> None:
    run_path, plan_result = _scan_and_plan(tmp_path)
    plan_id = str(plan_result["plan_id"])

    missing_reason = run_cli(
        tmp_path,
        "plan-override",
        str(run_path),
        "snapshot-2025-07-01/Home/Projects/ai-assistant/docs/recovery-checklist.txt",
        "   ",
        "--plan-id",
        plan_id,
    )
    assert missing_reason.returncode == 1
    assert "Override reason is required" in missing_reason.stderr


def test_plan_finalize_freezes_plan_and_rejects_further_changes(tmp_path: Path) -> None:
    from filesystem_organizer import analysis_run

    run_path, plan_result = _scan_and_plan(tmp_path)
    plan_id = str(plan_result["plan_id"])

    database_path = run_path / analysis_run.DATABASE_NAME
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        operations_before = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM plan_operations WHERE plan_id = ? ORDER BY operation_index",
                (plan_id,),
            ).fetchall()
        ]

    finalize = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)
    assert finalize.returncode == 0, finalize.stderr
    finalize_result = json.loads(finalize.stdout)
    assert finalize_result["plan_id"] == plan_id
    assert finalize_result["status"] == "finalized"
    assert finalize_result["finalized_at"]

    double_finalize = run_cli(
        tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id
    )
    assert double_finalize.returncode == 1
    assert "already finalized" in double_finalize.stderr

    rejected_override = run_cli(
        tmp_path,
        "plan-override",
        str(run_path),
        "snapshot-2025-07-01/Home/Projects/ai-assistant/docs/recovery-checklist.txt",
        "too late, already finalized",
        "--plan-id",
        plan_id,
    )
    assert rejected_override.returncode == 1
    assert "is not a draft" in rejected_override.stderr

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        plan_row = connection.execute(
            "SELECT status, finalized_at FROM consolidation_plans WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        assert plan_row["status"] == "finalized"
        assert plan_row["finalized_at"] == finalize_result["finalized_at"]

        operations_after = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM plan_operations WHERE plan_id = ? ORDER BY operation_index",
                (plan_id,),
            ).fetchall()
        ]
    assert operations_after == operations_before

    plan_report = run_cli(tmp_path, "plan-report", str(run_path), "--plan-id", plan_id)
    assert plan_report.returncode == 0, plan_report.stderr
    assert "**Status:** finalized" in plan_report.stdout


def test_changing_choice_after_finalization_requires_new_plan(tmp_path: Path) -> None:
    from filesystem_organizer import analysis_run

    run_path, plan_result = _scan_and_plan(tmp_path)
    first_plan_id = str(plan_result["plan_id"])

    finalize = run_cli(
        tmp_path, "plan-finalize", str(run_path), "--plan-id", first_plan_id
    )
    assert finalize.returncode == 0, finalize.stderr

    database_path = run_path / analysis_run.DATABASE_NAME
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        first_plan_operations_before = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM plan_operations WHERE plan_id = ? ORDER BY operation_index",
                (first_plan_id,),
            ).fetchall()
        ]

    second_plan = run_cli(tmp_path, "plan", str(run_path))
    assert second_plan.returncode == 0, second_plan.stderr
    second_plan_result = json.loads(second_plan.stdout)
    second_plan_id = str(second_plan_result["plan_id"])
    assert second_plan_id != first_plan_id
    assert second_plan_result["status"] == "draft"

    override = run_cli(
        tmp_path,
        "plan-override",
        str(run_path),
        "snapshot-2025-07-01/Home/Projects/ai-assistant/docs/recovery-checklist.txt",
        "want the project copy in the new plan",
        "--plan-id",
        second_plan_id,
    )
    assert override.returncode == 0, override.stderr

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        first_plan_status = connection.execute(
            "SELECT status FROM consolidation_plans WHERE plan_id = ?", (first_plan_id,)
        ).fetchone()["status"]
        first_plan_operations_after = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM plan_operations WHERE plan_id = ? ORDER BY operation_index",
                (first_plan_id,),
            ).fetchall()
        ]
        second_plan_operations = {
            row["source_relative_path"]
            for row in connection.execute(
                "SELECT source_relative_path FROM plan_operations WHERE plan_id = ?",
                (second_plan_id,),
            ).fetchall()
        }

    assert first_plan_status == "finalized"
    assert first_plan_operations_after == first_plan_operations_before
    assert (
        "snapshot-2025-07-01/Home/Projects/ai-assistant/docs/recovery-checklist.txt"
        in second_plan_operations
    )
    assert (
        "snapshot-2023-11-01/Home/Projects/ai-assistant/docs/recovery-checklist.txt"
        not in second_plan_operations
    )
