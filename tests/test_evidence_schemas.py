from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from filesystem_organizer.analysis_run import create_analysis_run
from filesystem_organizer.consolidation_plan.custom_layout import (
    _final_projection_fingerprint,
)
from filesystem_organizer.materialization import materialize_consolidation_plan
from filesystem_organizer.run_workspace import (
    ATTEMPT_SCHEMA_VERSION,
    DATABASE_NAME,
    PLAN_SCHEMA_VERSION,
    SCHEMA_VERSION,
)
from tests.test_cli import run_cli
from tests.test_incremental_custom_layout import _export, _scan_plan
from tests.test_materialization import _scan_simple_plan_finalize


def test_analysis_schema_versions_tagged_candidates_and_identity_work(
    tmp_path: Path,
) -> None:
    selected = tmp_path / "selected"
    selected.mkdir()
    (selected / "one.txt").write_bytes(b"one")
    result = create_analysis_run(selected, tmp_path / "output")
    database = Path(str(result["analysis_run"])) / DATABASE_NAME

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        assert connection.execute(
            "SELECT schema_version FROM analysis_runs"
        ).fetchone() == (SCHEMA_VERSION,)
        progress = connection.execute(
            "SELECT phase, hashed_file_count, hashed_byte_count, max_batch_size, "
            "checkpoint_count FROM evidence_discovery_progress"
        ).fetchone()
        assert progress == ("complete", 0, 0, 2, 1)
        connection.execute(
            "INSERT INTO analysis_candidates "
            "(run_id, candidate_kind, candidate_key, byte_size, left_relative_path, "
            "right_relative_path, state) SELECT run_id, 'duplicate-size-group', "
            "'size:3', 3, NULL, NULL, 'pending' FROM analysis_runs"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO analysis_candidates "
                "(run_id, candidate_kind, candidate_key, byte_size, left_relative_path, "
                "right_relative_path, state) SELECT run_id, 'duplicate-size-group', "
                "'mixed', 3, 'one.txt', 'two.txt', 'pending' FROM analysis_runs"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO content_identity_work "
                "(run_id, relative_path, purpose, state) "
                "SELECT run_id, 'missing.txt', 'duplicate-proof', 'pending' FROM analysis_runs"
            )


def test_finalized_entries_have_exactly_one_tagged_evidence_variant(
    tmp_path: Path,
) -> None:
    run_path, plan_id, _output_root, _selected = _scan_simple_plan_finalize(tmp_path)

    with sqlite3.connect(run_path / DATABASE_NAME) as connection:
        connection.row_factory = sqlite3.Row
        plan_schema = connection.execute(
            "SELECT schema_version FROM consolidation_plans WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()[0]
        evidence = connection.execute(
            "SELECT entry_kind, evidence_kind, evidence_entry_type, "
            "evidence_mtime_ns, evidence_directory_path, evidence_layout_revision, "
            "algorithm, algorithm_version, expected_byte_size, digest "
            "FROM final_plan_entries WHERE plan_id = ? ORDER BY entry_index",
            (plan_id,),
        ).fetchall()
        assert plan_schema == PLAN_SCHEMA_VERSION
        assert {str(row["evidence_kind"]) for row in evidence} == {
            "metadata-observation"
        }

        original = _final_projection_fingerprint(connection, plan_id, 0)
        connection.execute(
            "CREATE TEMP TABLE fingerprint_before_execution(value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO fingerprint_before_execution VALUES (?)", (original,)
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE final_plan_entries SET evidence_kind = 'content-identity' "
                "WHERE plan_id = ? AND entry_index = 0",
                (plan_id,),
            )
        connection.execute(
            "UPDATE final_plan_entries SET evidence_kind = 'metadata-observation', "
            "evidence_entry_type = 'regular-file', evidence_mtime_ns = 1 "
            "WHERE plan_id = ? AND entry_index = 0",
            (plan_id,),
        )
        assert _final_projection_fingerprint(connection, plan_id, 0) != original
        connection.rollback()


def test_layout_owned_directory_evidence_binds_path_and_revision(tmp_path: Path) -> None:
    run_path, plan_id = _scan_plan(tmp_path)
    layout_path, _response = _export(tmp_path, run_path, plan_id, "layout.json")
    layout = json.loads(layout_path.read_text())
    layout["directories"] = ["Empty"]
    layout_path.write_text(json.dumps(layout))
    applied = run_cli(
        tmp_path, "plan-layout-apply", str(run_path), plan_id, str(layout_path)
    )
    assert applied.returncode == 0, applied.stderr
    revision = int(json.loads(applied.stdout)["revision"])
    finalized = run_cli(
        tmp_path, "plan-finalize", str(run_path), "--plan-id", plan_id
    )
    assert finalized.returncode == 0, finalized.stderr

    with sqlite3.connect(run_path / DATABASE_NAME) as connection:
        row = connection.execute(
            "SELECT evidence_kind, evidence_directory_path, evidence_layout_revision "
            "FROM final_plan_entries WHERE plan_id = ? AND output_relative_path = 'Empty'",
            (plan_id,),
        ).fetchone()
    assert row == ("layout-owned-directory", "Empty", revision)


def test_execution_schema_binds_attempt_manifest_and_file_evidence(
    tmp_path: Path,
) -> None:
    run_path, plan_id, _output_root, _selected = _scan_simple_plan_finalize(tmp_path)
    materialized = materialize_consolidation_plan(run_path, plan_id)
    assert materialized["status"] == "materialized"

    with sqlite3.connect(run_path / DATABASE_NAME) as connection:
        connection.row_factory = sqlite3.Row
        attempt = connection.execute(
            "SELECT * FROM materialization_attempts WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        assert attempt is not None
        assert attempt["attempt_version"] == ATTEMPT_SCHEMA_VERSION
        assert attempt["state"] == "COMPLETE"
        manifest = connection.execute(
            "SELECT * FROM execution_manifests WHERE attempt_id = ?",
            (attempt["attempt_id"],),
        ).fetchone()
        assert manifest is not None
        assert manifest["canonical_destination"] == materialized["destination"]
        assert manifest["staging_owner_attempt_id"] == attempt["attempt_id"]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE materialization_attempts SET state = 'FAILED' "
                "WHERE attempt_id = ?",
                (attempt["attempt_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE execution_manifests SET staging_root_dev = 1, "
                "staging_root_ino = NULL WHERE attempt_id = ?",
                (attempt["attempt_id"],),
            )

        fingerprint = connection.execute(
            "SELECT fingerprint FROM final_plan_projection WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()[0]
        entry = connection.execute(
            "SELECT entry_index, source_relative_path, expected_byte_size "
            "FROM final_plan_entries WHERE plan_id = ? AND entry_kind = 'file' "
            "ORDER BY entry_index LIMIT 1",
            (plan_id,),
        ).fetchone()
        connection.execute(
            "DELETE FROM execution_file_evidence WHERE attempt_id = ? AND entry_index = ?",
            (attempt["attempt_id"], entry["entry_index"]),
        )
        connection.execute(
            "INSERT INTO execution_file_evidence "
            "(attempt_id, plan_id, run_id, entry_index, source_relative_path, observed_byte_size, "
            "observed_mtime_ns, transfer_kind, algorithm, algorithm_version, digest) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'streamed', 'BLAKE3-256', 1, 'abcd')",
            (
                attempt["attempt_id"],
                plan_id,
                manifest["run_id"],
                entry["entry_index"],
                entry["source_relative_path"],
                entry["expected_byte_size"],
                connection.execute(
                    "SELECT modified_ns FROM inventory_entries WHERE run_id = ? "
                    "AND relative_path = ?",
                    (manifest["run_id"], entry["source_relative_path"]),
                ).fetchone()[0],
            ),
        )
        assert connection.execute(
            "SELECT fingerprint FROM final_plan_projection WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()[0] == fingerprint
        connection.execute(
            "DELETE FROM execution_file_evidence WHERE attempt_id = ?",
            (attempt["attempt_id"],),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO execution_file_evidence "
                "(attempt_id, plan_id, run_id, entry_index, source_relative_path, observed_byte_size, "
                "observed_mtime_ns, transfer_kind, algorithm, algorithm_version, digest) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'native-clone', 'BLAKE3-256', 1, 'mixed')",
                (
                    attempt["attempt_id"],
                    plan_id,
                    manifest["run_id"],
                    entry["entry_index"],
                    entry["source_relative_path"],
                    entry["expected_byte_size"],
                    connection.execute(
                        "SELECT modified_ns FROM inventory_entries WHERE run_id = ? "
                        "AND relative_path = ?",
                        (manifest["run_id"], entry["source_relative_path"]),
                    ).fetchone()[0],
                ),
            )
