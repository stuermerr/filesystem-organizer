from __future__ import annotations

import json
import random
import sqlite3
from pathlib import Path

from filesystem_organizer.consolidation_plan.custom_layout import (
    _final_projection_fingerprint,
    _parse_layout,
    _resolve,
    _validate_occupancy,
    validate_layout,
)
from tests.test_cli import run_cli


def _scan_plan(tmp_path: Path) -> tuple[Path, str]:
    selected = tmp_path / "backup"
    for directory, name, contents in (
        ("documents", "note.txt", b"note"),
        ("documents", "draft.txt", b"draft"),
        ("images", "photo.jpg", b"photo"),
    ):
        target = selected / directory
        target.mkdir(parents=True, exist_ok=True)
        (target / name).write_bytes(contents)
    scan = run_cli(tmp_path, "scan", str(selected))
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    result = json.loads(plan.stdout)
    assert result["schema_version"] == 9
    return run_path, str(result["plan_id"])


def _export(
    tmp_path: Path, run_path: Path, plan_id: str, name: str
) -> tuple[Path, dict[str, object]]:
    layout_path = tmp_path / name
    exported = run_cli(
        tmp_path,
        "plan-layout-export",
        str(run_path),
        plan_id,
        "--output",
        str(layout_path),
    )
    assert exported.returncode == 0, exported.stderr
    response = json.loads(exported.stdout)
    assert "baseline_entries" not in response
    return layout_path, response


def test_successive_export_preserves_complete_authoring_and_semantic_noop(
    tmp_path: Path,
) -> None:
    run_path, plan_id = _scan_plan(tmp_path)
    layout_path, response = _export(tmp_path, run_path, plan_id, "first.json")
    layout = json.loads(layout_path.read_text())
    assert layout["layout_schema_version"] == 2
    assert layout["baseline_fingerprint"] == response["baseline_fingerprint"]
    layout["rules"] = [
        {
            "selector": {"subtree": "documents"},
            "action": {"place_under": "Notes"},
        }
    ]
    layout["entry_exceptions"] = []
    layout["directories"] = ["Inbox"]
    layout["skipped_actions"] = []
    layout_path.write_text(json.dumps(layout))
    applied = run_cli(
        tmp_path, "plan-layout-apply", str(run_path), plan_id, str(layout_path)
    )
    assert applied.returncode == 0, applied.stderr
    assert json.loads(applied.stdout)["revision"] == 1

    second_path, _ = _export(tmp_path, run_path, plan_id, "second.json")
    second = json.loads(second_path.read_text())
    assert second["base_revision"] == 1
    assert second["baseline_fingerprint"] == layout["baseline_fingerprint"]
    assert second["rules"] == layout["rules"]
    assert second["entry_exceptions"] == layout["entry_exceptions"]
    assert second["directories"] == layout["directories"]
    assert second["skipped_actions"] == layout["skipped_actions"]

    second["rules"] = list(reversed(second["rules"]))
    second_path.write_text(json.dumps(second, indent=7, sort_keys=False))
    noop = run_cli(
        tmp_path, "plan-layout-apply", str(run_path), plan_id, str(second_path)
    )
    assert noop.returncode == 0, noop.stderr
    assert json.loads(noop.stdout) == {
        **json.loads(noop.stdout),
        "no_change": True,
        "revision": 1,
    }
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM plan_layout_revisions WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()[0]
            == 2
        )


def test_authoring_change_with_no_resolved_entry_change_creates_sparse_revision(
    tmp_path: Path,
) -> None:
    run_path, plan_id = _scan_plan(tmp_path)
    layout_path, _ = _export(tmp_path, run_path, plan_id, "layout.json")
    layout = json.loads(layout_path.read_text())
    layout["directories"] = ["Empty"]
    layout_path.write_text(json.dumps(layout))
    applied = run_cli(
        tmp_path, "plan-layout-apply", str(run_path), plan_id, str(layout_path)
    )
    assert applied.returncode == 0, applied.stderr
    result = json.loads(applied.stdout)
    assert result["affected_entry_count"] == 0
    assert result["changed_entry_count"] == 0
    assert result["structure_delta"]["directories_added"] == ["Empty"]
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM plan_layout_entry_deltas WHERE plan_id = ? AND revision = 1",
                (plan_id,),
            ).fetchone()[0]
            == 0
        )


def test_rebuild_verify_and_repair_are_explicit_and_finalization_refuses_mismatch(
    tmp_path: Path,
) -> None:
    run_path, plan_id = _scan_plan(tmp_path)
    layout_path, _ = _export(tmp_path, run_path, plan_id, "layout.json")
    layout = json.loads(layout_path.read_text())
    layout["rules"] = [
        {
            "selector": {"subtree": "documents"},
            "action": {"place_under": "Notes"},
        }
    ]
    layout_path.write_text(json.dumps(layout))
    applied = run_cli(
        tmp_path, "plan-layout-apply", str(run_path), plan_id, str(layout_path)
    )
    assert applied.returncode == 0, applied.stderr

    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        connection.execute(
            "UPDATE plan_layout_active_entries SET output_relative_path = 'corrupt/value' "
            "WHERE plan_id = ? AND entry_id = "
            "(SELECT entry_id FROM plan_layout_active_entries WHERE plan_id = ? LIMIT 1)",
            (plan_id, plan_id),
        )
        connection.commit()

    verify = run_cli(
        tmp_path,
        "plan-layout-rebuild-active",
        str(run_path),
        plan_id,
        "--verify",
    )
    assert verify.returncode == 1
    assert json.loads(verify.stdout)["consistent"] is False
    finalized = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)
    assert finalized.returncode == 1
    assert "active-projection-inconsistent" in finalized.stderr

    repaired = run_cli(
        tmp_path,
        "plan-layout-rebuild-active",
        str(run_path),
        plan_id,
        "--repair",
    )
    assert repaired.returncode == 0, repaired.stderr
    assert json.loads(repaired.stdout)["consistent"] is True
    finalized = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)
    assert finalized.returncode == 0, finalized.stderr
    result = json.loads(finalized.stdout)
    assert result["projection_fingerprint"]
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        metadata = connection.execute(
            "SELECT revision, entry_count, fingerprint FROM final_plan_projection WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        assert metadata == (1, 3, result["projection_fingerprint"])
        query_plan = " ".join(
            str(row[3])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT entry_kind, output_relative_path "
                "FROM final_plan_entries WHERE plan_id=? AND "
                "length(output_relative_path)-length(replace(output_relative_path, '/', ''))<=?",
                (plan_id, 1),
            )
        )
        assert "final_plan_entry_depth_lookup" in query_plan
        connection.row_factory = sqlite3.Row
        original = _final_projection_fingerprint(connection, plan_id, 1)
        assert original == result["projection_fingerprint"]
        connection.execute(
            "UPDATE plan_source_selections SET reason='alternate source choice' "
            "WHERE plan_id=? AND entry_id=(SELECT entry_id FROM plan_source_selections "
            "WHERE plan_id=? LIMIT 1)",
            (plan_id, plan_id),
        )
        assert _final_projection_fingerprint(connection, plan_id, 1) != original
        connection.rollback()
        connection.execute(
            "INSERT INTO final_plan_skipped_actions VALUES(?,?,?)",
            (plan_id, "skipped/example", "retain"),
        )
        assert _final_projection_fingerprint(connection, plan_id, 1) != original
        connection.rollback()


def test_randomized_impact_compilation_matches_a_slow_complete_projection(
    tmp_path: Path,
) -> None:
    selected = tmp_path / "random-backup"
    baseline: list[str] = []
    for group in range(5):
        for item in range(6):
            relative = f"group-{group}/item-{item}.txt"
            target = selected / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"{group}:{item}")
            baseline.append(relative)
    scan = run_cli(tmp_path, "scan", str(selected))
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    plan = run_cli(tmp_path, "plan", str(run_path))
    plan_id = str(json.loads(plan.stdout)["plan_id"])
    rng = random.Random(94)

    for revision in range(10):
        layout_path, _ = _export(tmp_path, run_path, plan_id, f"random-{revision}.json")
        layout = json.loads(layout_path.read_text())
        targets = list(range(5))
        rng.shuffle(targets)
        excluded = {group for group in range(5) if rng.random() < 0.25}
        layout["rules"] = [
            {
                "selector": {"subtree": f"group-{group}"},
                "action": (
                    {"exclude": True}
                    if group in excluded
                    else {"place_under": f"target-{targets[group]}"}
                ),
            }
            for group in range(5)
        ]
        layout_path.write_text(json.dumps(layout))
        with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
            connection.row_factory = sqlite3.Row
            reference_findings: list[dict[str, object]] = []
            rules, exceptions, directories, _ = _parse_layout(
                connection, plan_id, layout, reference_findings
            )
            # Deliberately resolve every baseline row, independent of the
            # impact-scope selector used by the incremental compiler.
            full_resolution = [
                _resolve(row, rules, exceptions)
                for row in connection.execute(
                    "SELECT * FROM plan_baseline_entries WHERE plan_id=? ORDER BY entry_id",
                    (plan_id,),
                )
            ]
            _validate_occupancy(
                connection, plan_id, full_resolution, directories, reference_findings
            )
            reference_projection = {
                str(entry.baseline["entry_id"]): (
                    entry.disposition,
                    entry.output_relative_path,
                )
                for entry in full_resolution
            }
        validation = validate_layout(run_path, plan_id, layout_path)
        assert validation["findings"] == reference_findings == []
        assert validation["placed_entry_count"] == sum(
            disposition == "place" for disposition, _ in reference_projection.values()
        )
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
        with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
            actual_projection = {
                entry_id: (disposition, output_path)
                for entry_id, disposition, output_path in connection.execute(
                    "SELECT entry_id, disposition, output_relative_path FROM plan_layout_active_entries "
                    "WHERE plan_id=?",
                    (plan_id,),
                )
            }
        assert actual_projection == reference_projection
        structure = run_cli(
            tmp_path,
            "plan-report",
            str(run_path),
            "--plan-id",
            plan_id,
            "--section",
            "structure",
            "--depth",
            "99",
        )
        assert structure.returncode == 0, structure.stderr
        actual_directories = {
            line.split("`")[1]
            for line in structure.stdout.splitlines()
            if line.startswith("| `")
        }
        expected_directories = {
            f"target-{targets[group]}" for group in range(5) if group not in excluded
        }
        assert actual_directories == expected_directories
        assert structure.stdout.count("| 1 | 6 | 6 |") == len(expected_directories)


def test_v6_plan_workspace_can_be_redrafted_from_existing_analysis_run(
    tmp_path: Path,
) -> None:
    selected = tmp_path / "historical"
    selected.mkdir()
    (selected / "note.txt").write_text("note")
    scan = run_cli(tmp_path, "scan", str(selected))
    assert scan.returncode == 0, scan.stderr
    run_path = Path(json.loads(scan.stdout)["analysis_run"])
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        connection.executescript(
            """
            CREATE TABLE plan_layout_revisions (
                plan_id TEXT NOT NULL, revision INTEGER NOT NULL,
                authoring_json TEXT NOT NULL, validation_json TEXT NOT NULL,
                created_at TEXT NOT NULL, PRIMARY KEY(plan_id, revision));
            CREATE TABLE plan_layout_state (
                plan_id TEXT PRIMARY KEY, active_revision INTEGER NOT NULL,
                content_empty INTEGER NOT NULL DEFAULT 0);
            INSERT INTO plan_layout_revisions VALUES
                ('older-plan', 0, '{}', '{}', 'historical');
            INSERT INTO plan_layout_state VALUES ('older-plan', 0, 0);
            """
        )
    plan = run_cli(tmp_path, "plan", str(run_path))
    assert plan.returncode == 0, plan.stderr
    assert json.loads(plan.stdout)["schema_version"] == 9
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT authoring_json FROM legacy_plan_layout_revisions "
                "WHERE plan_id='older-plan'"
            ).fetchone()[0]
            == "{}"
        )
    assert run_cli(tmp_path, "status", str(run_path)).returncode == 0


def test_changed_candidate_collision_with_unchanged_entry_rolls_back(
    tmp_path: Path,
) -> None:
    run_path, plan_id = _scan_plan(tmp_path)
    path, _ = _export(tmp_path, run_path, plan_id, "collision.json")
    layout = json.loads(path.read_text())
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        entry_id = connection.execute(
            "SELECT entry_id FROM plan_baseline_entries "
            "WHERE plan_id=? AND baseline_path='documents/note.txt'",
            (plan_id,),
        ).fetchone()[0]
    layout["entry_exceptions"] = [
        {
            "entry_id": entry_id,
            "action": {"place_at": "images/photo.jpg"},
        }
    ]
    path.write_text(json.dumps(layout))
    validation = run_cli(
        tmp_path, "plan-layout-validate", str(run_path), plan_id, str(path)
    )
    assert validation.returncode == 0
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        connection.row_factory = sqlite3.Row
        parse_findings: list[dict[str, object]] = []
        rules, exceptions, _, _ = _parse_layout(
            connection, plan_id, layout, parse_findings
        )
        assert not parse_findings
        output_counts: dict[str, int] = {}
        for baseline_entry in connection.execute(
            "SELECT * FROM plan_baseline_entries WHERE plan_id=?", (plan_id,)
        ):
            resolved = _resolve(baseline_entry, rules, exceptions)
            if resolved.disposition == "place" and resolved.output_relative_path:
                output_counts[resolved.output_relative_path] = (
                    output_counts.get(resolved.output_relative_path, 0) + 1
                )
        reference_collisions = {
            ("output-collision", path)
            for path, count in output_counts.items()
            if count > 1
        }
    actual_collisions = {
        (finding["code"], finding["path"])
        for finding in json.loads(validation.stdout)["findings"]
        if finding["code"] == "output-collision"
    }
    assert (
        actual_collisions
        == reference_collisions
        == {("output-collision", "images/photo.jpg")}
    )
    applied = run_cli(tmp_path, "plan-layout-apply", str(run_path), plan_id, str(path))
    assert applied.returncode == 1
    verify = run_cli(
        tmp_path, "plan-layout-rebuild-active", str(run_path), plan_id, "--verify"
    )
    assert verify.returncode == 0
    assert json.loads(verify.stdout)["revision"] == 0
    assert json.loads(verify.stdout)["consistent"] is True


def test_verify_detects_and_explicit_repair_restores_corrupted_aggregates(
    tmp_path: Path,
) -> None:
    run_path, plan_id = _scan_plan(tmp_path)
    with sqlite3.connect(run_path / "analysis.sqlite3") as connection:
        connection.execute(
            "UPDATE plan_layout_state SET total_placed_bytes=-1 WHERE plan_id=?",
            (plan_id,),
        )
    verify = run_cli(
        tmp_path, "plan-layout-rebuild-active", str(run_path), plan_id, "--verify"
    )
    assert verify.returncode == 1
    assert json.loads(verify.stdout)["consistent"] is False
    finalize = run_cli(tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id)
    assert finalize.returncode == 1
    assert "active-projection-inconsistent" in finalize.stderr
    repair = run_cli(
        tmp_path, "plan-layout-rebuild-active", str(run_path), plan_id, "--repair"
    )
    assert repair.returncode == 0, repair.stderr
    assert json.loads(repair.stdout)["repaired"] is True
    assert (
        run_cli(
            tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id
        ).returncode
        == 0
    )
