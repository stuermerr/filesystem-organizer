from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast

import pytest

from filesystem_organizer import analysis_run, run_workspace, structural_relationships
from filesystem_organizer.analysis_run import (
    AnalysisRunError,
    create_analysis_run,
    resume_analysis_run,
)
from filesystem_organizer.consolidation_plan import create_consolidation_plan
from filesystem_organizer.content_identity import (
    ContentIdentity,
    FileObservation,
    hash_regular_file,
)


def test_scan_leaves_unique_files_as_metadata_plan_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "small.txt").write_bytes(b"one")
    (selected_root / "large.txt").write_bytes(b"two-two")
    hashed: list[str] = []
    def record_hash(
        path: Path, relative_path: str, observation: FileObservation
    ) -> ContentIdentity:
        hashed.append(relative_path)
        return hash_regular_file(path, relative_path, observation)

    monkeypatch.setattr(
        analysis_run, "hash_regular_file", record_hash, raising=False
    )
    result = create_analysis_run(selected_root, tmp_path / "output")
    run_path = Path(str(result["analysis_run"]))

    assert hashed == []
    plan = create_consolidation_plan(run_path)
    with sqlite3.connect(run_path / analysis_run.DATABASE_NAME) as connection:
        identities = connection.execute("SELECT relative_path FROM content_identities").fetchall()
        evidence = connection.execute(
            "SELECT source_relative_path, evidence_kind, algorithm, digest "
            "FROM plan_operations WHERE plan_id = ? ORDER BY source_relative_path",
            (plan["plan_id"],),
        ).fetchall()
    assert identities == []
    assert evidence == [
        ("large.txt", "metadata-observation", None, None),
        ("small.txt", "metadata-observation", None, None),
    ]


def test_scan_proves_empty_duplicate_group_without_reading_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    (selected_root / "first.empty").touch()
    (selected_root / "second.empty").touch()
    monkeypatch.setattr(
        analysis_run,
        "hash_regular_file",
        lambda *_args, **_kwargs: pytest.fail("empty files must not be read"),
    )

    result = create_analysis_run(selected_root, tmp_path / "output")
    with sqlite3.connect(Path(str(result["analysis_run"])) / analysis_run.DATABASE_NAME) as connection:
        identities = connection.execute(
            "SELECT relative_path, byte_size, read_outcome FROM content_identities "
            "ORDER BY relative_path"
        ).fetchall()
    assert identities == [
        ("first.empty", 0, "successful"),
        ("second.empty", 0, "successful"),
    ]


def test_scan_refuses_non_wal_run_output_root_and_removes_incomplete_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    output_root = tmp_path / "output"
    non_wal_connection = sqlite3.connect(":memory:", isolation_level=None)
    non_wal_connection.row_factory = sqlite3.Row
    monkeypatch.setattr(
        run_workspace,
        "open_read_write",
        lambda _database: non_wal_connection,
    )

    with pytest.raises(AnalysisRunError, match="failed WAL admission"):
        create_analysis_run(selected_root, output_root)

    assert list((output_root / "runs").iterdir()) == []


def test_resume_reuses_persisted_structural_schema_after_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    for name in ("first", "second"):
        directory = selected_root / name
        directory.mkdir()
        (directory / "same.txt").write_bytes(b"same")
    output_root = tmp_path / "output"
    original_analyze = getattr(  # noqa: B009 - private orchestration seam
        analysis_run, "analyze_structural_relationships"
    )

    def interrupted_analyze(*args: object) -> None:
        original_analyze(*args)
        raise RuntimeError("simulated interruption after structural persistence")

    monkeypatch.setattr(
        analysis_run, "analyze_structural_relationships", interrupted_analyze
    )
    with pytest.raises(RuntimeError, match="simulated interruption"):
        create_analysis_run(selected_root, output_root)

    monkeypatch.setattr(
        analysis_run, "analyze_structural_relationships", original_analyze
    )
    run_path = next((output_root / "runs").iterdir())
    resumed = resume_analysis_run(run_path)

    assert resumed["status"] == "complete"


def test_structural_candidate_evidence_commits_in_bounded_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    for index in range(501):
        (selected_root / f"directory-{index:03}").mkdir()

    def interrupt_after_first_candidate_batch(name: str) -> None:
        if name == "candidate-batch":
            raise RuntimeError("simulated interruption after first candidate batch")

    monkeypatch.setattr(
        structural_relationships, "_checkpoint", interrupt_after_first_candidate_batch
    )
    output_root = tmp_path / "output"
    with pytest.raises(RuntimeError, match="simulated interruption"):
        create_analysis_run(selected_root, output_root)

    run_path = next((output_root / "runs").iterdir())
    with sqlite3.connect(run_path / analysis_run.DATABASE_NAME) as connection:
        persisted_rows = connection.execute(
            "SELECT COUNT(*) FROM directory_fingerprints"
        ).fetchone()[0]
    assert persisted_rows == 500

    resumed_checkpoints: list[str] = []
    monkeypatch.setattr(
        structural_relationships, "_checkpoint", resumed_checkpoints.append
    )
    resumed = resume_analysis_run(run_path)

    assert resumed["status"] == "complete"
    assert resumed_checkpoints.count("candidate-batch") == 1
    with sqlite3.connect(run_path / analysis_run.DATABASE_NAME) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM directory_fingerprints"
            ).fetchone()[0]
            == 502
        )


def test_structural_evidence_batch_commits_before_exceeding_byte_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ConnectionProbe:
        def __init__(self) -> None:
            self.commits = 0

        def execute(
            self, _statement: str, _parameters: tuple[object, ...] = ()
        ) -> None:
            pass

        def commit(self) -> None:
            self.commits += 1

    checkpoints: list[str] = []
    monkeypatch.setattr(structural_relationships, "_checkpoint", checkpoints.append)
    connection = ConnectionProbe()
    batch = structural_relationships._StructuralEvidenceBatch(
        cast(sqlite3.Connection, connection), "candidate-batch"
    )
    largest_row = "x" * structural_relationships._STRUCTURAL_BATCH_MAX_PAYLOAD_BYTES

    batch.write("INSERT", (largest_row,))
    batch.write("INSERT", ("one",))
    batch.commit()

    assert connection.commits == 2
    assert checkpoints == ["candidate-batch", "candidate-batch"]


def test_resume_completes_conflicts_split_across_structural_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected_root = tmp_path / "backup"
    for root_name in ("first", "second"):
        directory = selected_root / root_name
        directory.mkdir(parents=True)
        for index in range(502):
            (directory / f"anchor-{index:03}.txt").write_bytes(b"same")
        for region in ("shared-region-one", "shared-region-two"):
            (directory / region).mkdir()
            (directory / region / "anchor.txt").write_bytes(b"same")
        for index in range(501):
            (directory / f"conflict-{index:03}.txt").write_text(root_name)

    relationship_batches = 0

    def interrupt_after_first_relationship_batch(name: str) -> None:
        nonlocal relationship_batches
        if name == "relationship-batch":
            relationship_batches += 1
            if relationship_batches == 1:
                raise RuntimeError("simulated interruption after relationship batch")

    monkeypatch.setattr(
        structural_relationships,
        "_checkpoint",
        interrupt_after_first_relationship_batch,
    )
    output_root = tmp_path / "output"
    with pytest.raises(RuntimeError, match="simulated interruption"):
        create_analysis_run(selected_root, output_root)

    run_path = next((output_root / "runs").iterdir())
    with sqlite3.connect(run_path / analysis_run.DATABASE_NAME) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM directory_relationships"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM relationship_conflicts"
            ).fetchone()[0]
            == 499
        )

    resumed_checkpoints: list[str] = []
    monkeypatch.setattr(
        structural_relationships, "_checkpoint", resumed_checkpoints.append
    )
    resume_analysis_run(run_path)

    assert resumed_checkpoints.count("relationship-batch") == 1
    with sqlite3.connect(run_path / analysis_run.DATABASE_NAME) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM relationship_conflicts"
            ).fetchone()[0]
            == 501
        )


@pytest.mark.parametrize(
    "checkpoint", ["tree-index-batch", "candidate-batch", "relationship-batch"]
)
def test_resume_preserves_committed_structural_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, checkpoint: str
) -> None:
    def prepare(root: Path) -> None:
        for name in ("first", "second", "third"):
            directory = root / name
            directory.mkdir(parents=True)
            (directory / "same.txt").write_bytes(b"same")
            (directory / "also-same.txt").write_bytes(b"also same")
            for region in ("shared-region-one", "shared-region-two"):
                (directory / region).mkdir()
                (directory / region / "anchor.txt").write_bytes(b"same")
            (directory / "conflict.txt").write_bytes(name.encode("utf-8"))
        (root / "first" / "unverified-link").symlink_to("same.txt")

    interrupted_source = tmp_path / "interrupted-source"
    uninterrupted_source = tmp_path / "uninterrupted-source"
    prepare(interrupted_source)
    prepare(uninterrupted_source)

    def interrupt_after_commit(name: str) -> None:
        if name == checkpoint:
            raise RuntimeError(f"simulated interruption after {checkpoint}")

    monkeypatch.setattr(structural_relationships, "_checkpoint", interrupt_after_commit)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        create_analysis_run(interrupted_source, tmp_path / "interrupted-output")
    monkeypatch.setattr(structural_relationships, "_checkpoint", lambda _name: None)

    interrupted_run = next((tmp_path / "interrupted-output" / "runs").iterdir())
    resume_analysis_run(interrupted_run)
    uninterrupted = create_analysis_run(
        uninterrupted_source, tmp_path / "uninterrupted-output"
    )

    def graph_rows(run_path: Path) -> tuple[list[tuple[object, ...]], ...]:
        with sqlite3.connect(run_path / analysis_run.DATABASE_NAME) as connection:
            return tuple(
                connection.execute(query).fetchall()
                for query in (
                    (
                        "SELECT root_relative_path, fingerprint_version, algorithm, digest "
                        "FROM directory_fingerprints ORDER BY root_relative_path"
                    ),
                    (
                        "SELECT component_id, root_relative_path "
                        "FROM actionable_exact_component_members "
                        "ORDER BY component_id, root_relative_path"
                    ),
                    (
                        "SELECT root_relative_path, descendant_relative_path, entry_kind, identity "
                        "FROM structural_shared_descendants "
                        "ORDER BY root_relative_path, descendant_relative_path"
                    ),
                    (
                        "SELECT left_root, right_root, classification, canonical_root, qualifications "
                        "FROM directory_relationships ORDER BY left_root, right_root"
                    ),
                    (
                        "SELECT left_root, right_root, descendant_relative_path, left_evidence, right_evidence "
                        "FROM relationship_conflicts ORDER BY left_root, right_root, descendant_relative_path"
                    ),
                )
            )

    assert graph_rows(interrupted_run) == graph_rows(
        Path(str(uninterrupted["analysis_run"]))
    )


def test_recompute_candidates_matches_fresh_derivation_after_candidate_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    for name in ("first", "second", "third"):
        directory = source / name
        directory.mkdir(parents=True)
        (directory / "same.txt").write_bytes(b"same")
        (directory / "conflict.txt").write_bytes(name.encode("utf-8"))
    output_root = tmp_path / "output"

    def interrupt_after_candidates(name: str) -> None:
        if name == "candidate-phase":
            raise RuntimeError("simulated interruption after candidate phase")

    monkeypatch.setattr(
        structural_relationships, "_checkpoint", interrupt_after_candidates
    )
    with pytest.raises(RuntimeError, match="simulated interruption"):
        create_analysis_run(source, output_root)
    monkeypatch.setattr(structural_relationships, "_checkpoint", lambda _name: None)

    run_path = next((output_root / "runs").iterdir())
    with sqlite3.connect(run_path / analysis_run.DATABASE_NAME) as connection:
        run_id = str(
            connection.execute("SELECT run_id FROM structural_analysis").fetchone()[0]
        )
        phase = str(
            connection.execute(
                "SELECT phase FROM structural_analysis WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
        )
        assert phase == "relationships"
        recomputed = structural_relationships._recompute_candidates(connection, run_id)
        fresh = structural_relationships._derive_candidates(
            connection, run_id, source.name
        )
        assert recomputed == fresh
        stored_count = int(
            connection.execute(
                "SELECT candidate_count FROM structural_analysis WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
        )
        assert len(recomputed) == stored_count
