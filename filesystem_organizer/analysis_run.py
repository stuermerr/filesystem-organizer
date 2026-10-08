from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import wraps
from itertools import pairwise
from pathlib import Path

from blake3 import blake3

from .content_identity import (
    CONTENT_IDENTITY_ALGORITHM,
    CONTENT_IDENTITY_ALGORITHM_VERSION,
    ContentIdentity,
    FileObservation,
    hash_regular_file,
    select_identity_rows,
)
from .exact_duplicate_groups import (
    READ_OUTCOME_SUCCESSFUL,
    derive_exact_duplicate_groups,
)
from .progress import NullProgressReporter, ProgressReporter
from .run_workspace import (
    DATABASE_NAME,
    SCHEMA_VERSION,
    RunWorkspaceError,
    is_within,
    open_wal_read_write,
    require_outside_selected_root,
    resolve_run_directory,
)
from .structural_relationships import analyze as analyze_structural_relationships

__all__ = ["DATABASE_NAME", "SCHEMA_VERSION"]

BATCH_SIZE = 500
DIRECTORY_EVIDENCE_VERSION = 1
DIRECTORY_EVIDENCE_SCHEMA_VERSION = 4
STRUCTURAL_RELATIONSHIPS_SCHEMA_VERSION = 9


class AnalysisRunError(Exception):
    """A safe, user-facing refusal of an Analysis Run operation."""


def _run_workspace_refusal[**P, R](
    function: Callable[P, R],
) -> Callable[P, R]:
    """Raise a Run Workspace refusal as this domain's own error type."""

    @wraps(function)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return function(*args, **kwargs)
        except RunWorkspaceError as error:
            raise AnalysisRunError(str(error)) from error

    return wrapper


@dataclass(frozen=True)
class InventoryEntry:
    relative_path: str
    kind: str
    observed_byte_size: int | None
    modified_ns: int | None
    read_outcome: str
    skipped_reason: str | None = None
    is_empty: bool | None = None
    content_identity: ContentIdentity | None = None
    file_observation: FileObservation | None = None


def _resolved_directory(path: Path, label: str) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except FileNotFoundError as error:
        raise AnalysisRunError(f"{label} does not exist: {path}") from error
    if not resolved.is_dir():
        raise AnalysisRunError(f"{label} is not a directory: {resolved}")
    return resolved


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE analysis_runs (
          run_id TEXT PRIMARY KEY,
          snapshot_id TEXT NOT NULL UNIQUE,
          schema_version INTEGER NOT NULL,
          selected_backup_root TEXT NOT NULL,
          status TEXT NOT NULL,
          started_at TEXT NOT NULL,
          completed_at TEXT,
          writer_lease TEXT,
          checkpoint_relative_path TEXT,
          hash_checkpoint_relative_path TEXT
        );

        CREATE TABLE inventory_entries (
          run_id TEXT NOT NULL,
          relative_path TEXT NOT NULL,
          entry_kind TEXT NOT NULL,
          observed_byte_size INTEGER,
          modified_ns INTEGER,
          read_outcome TEXT NOT NULL,
          PRIMARY KEY (run_id, relative_path),
          UNIQUE (run_id, relative_path, observed_byte_size, modified_ns),
          CHECK (entry_kind != 'regular-file' OR observed_byte_size IS NOT NULL),
          CHECK (observed_byte_size IS NULL OR observed_byte_size >= 0),
          FOREIGN KEY (run_id) REFERENCES analysis_runs(run_id)
        );

        CREATE TABLE skipped_entry_findings (
          run_id TEXT NOT NULL,
          relative_path TEXT NOT NULL,
          reason TEXT NOT NULL,
          PRIMARY KEY (run_id, relative_path),
          FOREIGN KEY (run_id) REFERENCES analysis_runs(run_id)
        );

        CREATE TABLE directory_evidence (
          run_id TEXT NOT NULL,
          relative_path TEXT NOT NULL,
          entry_kind TEXT NOT NULL,
          is_empty INTEGER,
          read_outcome TEXT NOT NULL,
          PRIMARY KEY (run_id, relative_path),
          FOREIGN KEY (run_id) REFERENCES analysis_runs(run_id)
        );

        CREATE TABLE content_identities (
          run_id TEXT NOT NULL,
          relative_path TEXT NOT NULL,
          algorithm TEXT NOT NULL,
          algorithm_version INTEGER NOT NULL,
          byte_size INTEGER NOT NULL,
          digest TEXT,
          read_outcome TEXT NOT NULL,
          PRIMARY KEY (run_id, relative_path),
          CHECK (byte_size >= 0),
          CHECK (
            (read_outcome = 'successful' AND digest IS NOT NULL)
            OR (read_outcome != 'successful' AND digest IS NULL)
          ),
          FOREIGN KEY (run_id, relative_path)
            REFERENCES inventory_entries(run_id, relative_path)
        );

        CREATE TABLE evidence_discovery_progress (
          run_id TEXT PRIMARY KEY,
          phase TEXT NOT NULL CHECK (phase IN (
            'inventory', 'duplicate-candidates', 'structural-candidates',
            'hashing', 'relationships', 'complete'
          )),
          cursor TEXT,
          emitted_candidate_count INTEGER NOT NULL DEFAULT 0
            CHECK (emitted_candidate_count >= 0),
          evaluated_candidate_count INTEGER NOT NULL DEFAULT 0
            CHECK (evaluated_candidate_count >= 0),
          hashed_file_count INTEGER NOT NULL DEFAULT 0
            CHECK (hashed_file_count >= 0),
          hashed_byte_count INTEGER NOT NULL DEFAULT 0
            CHECK (hashed_byte_count >= 0),
          reused_identity_count INTEGER NOT NULL DEFAULT 0
            CHECK (reused_identity_count >= 0),
          max_batch_size INTEGER NOT NULL DEFAULT 0 CHECK (max_batch_size >= 0),
          checkpoint_count INTEGER NOT NULL DEFAULT 0 CHECK (checkpoint_count >= 0),
          FOREIGN KEY (run_id) REFERENCES analysis_runs(run_id)
        );

        CREATE TABLE analysis_candidates (
          run_id TEXT NOT NULL,
          candidate_kind TEXT NOT NULL CHECK (candidate_kind IN (
            'duplicate-size-group', 'structural-pair'
          )),
          candidate_key TEXT NOT NULL,
          byte_size INTEGER,
          left_relative_path TEXT,
          right_relative_path TEXT,
          state TEXT NOT NULL CHECK (state IN ('pending', 'evaluated')),
          PRIMARY KEY (run_id, candidate_kind, candidate_key),
          CHECK (
            (candidate_kind = 'duplicate-size-group' AND byte_size IS NOT NULL
              AND byte_size >= 0 AND left_relative_path IS NULL
              AND right_relative_path IS NULL)
            OR (candidate_kind = 'structural-pair' AND byte_size IS NULL
              AND left_relative_path IS NOT NULL AND right_relative_path IS NOT NULL
              AND left_relative_path < right_relative_path)
          ),
          FOREIGN KEY (run_id) REFERENCES analysis_runs(run_id)
        );

        CREATE TABLE content_identity_work (
          run_id TEXT NOT NULL,
          relative_path TEXT NOT NULL,
          purpose TEXT NOT NULL CHECK (purpose IN (
            'duplicate-proof', 'structural-proof'
          )),
          state TEXT NOT NULL CHECK (state IN ('pending', 'complete')),
          PRIMARY KEY (run_id, relative_path, purpose),
          FOREIGN KEY (run_id, relative_path)
            REFERENCES inventory_entries(run_id, relative_path)
        );

        CREATE TABLE canonical_copies (
          run_id TEXT NOT NULL,
          algorithm TEXT NOT NULL,
          algorithm_version INTEGER NOT NULL,
          byte_size INTEGER NOT NULL,
          digest TEXT NOT NULL,
          canonical_relative_path TEXT NOT NULL,
          canonical_reason TEXT NOT NULL,
          PRIMARY KEY (run_id, algorithm, algorithm_version, byte_size, digest),
          FOREIGN KEY (run_id) REFERENCES analysis_runs(run_id)
        );
        """
    )


def _collect_entries(root: Path) -> list[InventoryEntry]:
    entries: list[InventoryEntry] = []
    traversal_errors: list[OSError] = []
    directory_has_children: dict[str, bool] = {}
    for directory, child_directories, child_names in os.walk(
        root, topdown=True, onerror=traversal_errors.append, followlinks=False
    ):
        child_directories.sort()
        child_names.sort()
        directory_relative_path = Path(directory).relative_to(root).as_posix() or "."
        directory_has_children[directory_relative_path] = bool(
            child_directories or child_names
        )
        if directory_relative_path == ".":
            metadata = Path(directory).lstat()
            entries.append(
                InventoryEntry(
                    ".",
                    "directory",
                    None,
                    metadata.st_mtime_ns,
                    READ_OUTCOME_SUCCESSFUL,
                )
            )
        for name in [*child_directories, *child_names]:
            path = Path(directory, name)
            relative_path = path.relative_to(root).as_posix()
            try:
                metadata = path.lstat()
            except OSError:
                entries.append(
                    InventoryEntry(
                        relative_path, "unknown", None, None, "unreadable", "unreadable"
                    )
                )
                continue
            if stat.S_ISLNK(metadata.st_mode):
                entries.append(
                    InventoryEntry(
                        relative_path,
                        "symbolic-link",
                        None,
                        metadata.st_mtime_ns,
                        "not-read",
                        "symbolic-link",
                    )
                )
            elif stat.S_ISDIR(metadata.st_mode):
                entries.append(
                    InventoryEntry(
                        relative_path,
                        "directory",
                        None,
                        metadata.st_mtime_ns,
                        READ_OUTCOME_SUCCESSFUL,
                    )
                )
            elif stat.S_ISREG(metadata.st_mode):
                observation = FileObservation.captured(metadata)
                entries.append(
                    InventoryEntry(
                        relative_path,
                        "regular-file",
                        observation.byte_size,
                        observation.modified_ns,
                        READ_OUTCOME_SUCCESSFUL,
                        file_observation=observation,
                    )
                )
            elif not stat.S_ISDIR(metadata.st_mode):
                entries.append(
                    InventoryEntry(
                        relative_path,
                        "special-entry",
                        None,
                        metadata.st_mtime_ns,
                        "not-read",
                        "special-entry",
                    )
                )

    unreadable_directories: set[str] = set()
    for error in traversal_errors:
        error_path = Path(error.filename) if error.filename else root
        try:
            relative_path = error_path.relative_to(root).as_posix()
        except ValueError:
            relative_path = "."
        unreadable_directories.add(relative_path)
        if not any(entry.relative_path == relative_path for entry in entries):
            entries.append(
                InventoryEntry(
                    relative_path, "directory", None, None, "unreadable", "unreadable"
                )
            )
    finalized_entries = [
        InventoryEntry(
            entry.relative_path,
            entry.kind,
            entry.observed_byte_size,
            entry.modified_ns,
            "unreadable"
            if entry.relative_path in unreadable_directories
            else entry.read_outcome,
            "unreadable"
            if entry.relative_path in unreadable_directories
            else entry.skipped_reason,
            (
                None
                if entry.relative_path in unreadable_directories
                else not directory_has_children.get(entry.relative_path, False)
            )
            if entry.kind == "directory"
            else None,
            entry.content_identity,
            entry.file_observation,
        )
        for entry in entries
    ]
    finalized_entries.sort(key=lambda entry: entry.relative_path)
    return finalized_entries


def _persist_inventory_entry(
    connection: sqlite3.Connection, run_id: str, entry: InventoryEntry
) -> None:
    connection.execute(
        "INSERT INTO inventory_entries VALUES (?, ?, ?, ?, ?, ?)",
        (
            run_id,
            entry.relative_path,
            entry.kind,
            entry.observed_byte_size,
            entry.modified_ns,
            entry.read_outcome,
        ),
    )
    if entry.kind == "directory":
        connection.execute(
            "INSERT INTO directory_evidence VALUES (?, ?, ?, ?, ?)",
            (
                run_id,
                entry.relative_path,
                entry.kind,
                None if entry.is_empty is None else int(entry.is_empty),
                entry.read_outcome,
            ),
        )
    if entry.content_identity is not None:
        identity = entry.content_identity
        connection.execute(
            "INSERT INTO content_identities VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                identity.relative_path,
                CONTENT_IDENTITY_ALGORITHM,
                CONTENT_IDENTITY_ALGORITHM_VERSION,
                identity.byte_size,
                identity.digest,
                identity.read_outcome,
            ),
        )
    if entry.skipped_reason is not None:
        connection.execute(
            "INSERT INTO skipped_entry_findings VALUES (?, ?, ?)",
            (run_id, entry.relative_path, entry.skipped_reason),
        )


def _persist_scan_batch(
    connection: sqlite3.Connection,
    run_id: str,
    _selected_root: Path,
    batch: list[InventoryEntry],
) -> None:
    """Atomically persist one metadata-inventory checkpoint."""
    connection.execute("BEGIN IMMEDIATE")
    for entry in batch:
        _persist_inventory_entry(connection, run_id, entry)
    connection.execute(
        "UPDATE analysis_runs SET checkpoint_relative_path = ? WHERE run_id = ?",
        (batch[-1].relative_path, run_id),
    )
    connection.execute(
        "UPDATE evidence_discovery_progress SET cursor = ?, "
        "max_batch_size = MAX(max_batch_size, ?), checkpoint_count = checkpoint_count + 1 "
        "WHERE run_id = ?",
        (batch[-1].relative_path, len(batch), run_id),
    )
    connection.commit()


def _run_scan_batches(
    connection: sqlite3.Connection,
    run_id: str,
    selected_root: Path,
    resume_after: str | None,
    progress: ProgressReporter,
) -> None:
    """Insert observations in bounded, checkpointed, atomic batches.

    Each batch commits its inventory entries, content identities, Skipped Entry
    Findings, and the advanced checkpoint together. An interruption between
    batches leaves only the prior, already-committed checkpoint durable.
    """
    entries = _collect_entries(selected_root)
    if resume_after is not None:
        entries = [entry for entry in entries if entry.relative_path > resume_after]

    progress.start("Scanning", len(entries), unit="entries")
    for start in range(0, len(entries), BATCH_SIZE):
        batch = entries[start : start + BATCH_SIZE]
        _persist_scan_batch(connection, run_id, selected_root, batch)
        progress.advance(len(batch))
    progress.complete()


def _prepare_duplicate_identity_work(
    connection: sqlite3.Connection, run_id: str
) -> None:
    """Persist byte-size collision groups and cheap empty-file identities."""
    groups = connection.execute(
        "SELECT observed_byte_size, COUNT(*) FROM inventory_entries "
        "WHERE run_id = ? AND entry_kind = 'regular-file' AND read_outcome = ? "
        "GROUP BY observed_byte_size HAVING COUNT(*) > 1 OR observed_byte_size = 0 "
        "ORDER BY observed_byte_size",
        (run_id, READ_OUTCOME_SUCCESSFUL),
    ).fetchall()
    connection.execute("BEGIN IMMEDIATE")
    for byte_size, _count in groups:
        size = int(byte_size)
        connection.execute(
            "INSERT OR IGNORE INTO analysis_candidates "
            "(run_id, candidate_kind, candidate_key, byte_size, "
            "left_relative_path, right_relative_path, state) "
            "VALUES (?, 'duplicate-size-group', ?, ?, NULL, NULL, 'pending')",
            (run_id, str(size), size),
        )
        paths = connection.execute(
            "SELECT relative_path FROM inventory_entries WHERE run_id = ? "
            "AND entry_kind = 'regular-file' AND read_outcome = ? "
            "AND observed_byte_size = ? ORDER BY relative_path",
            (run_id, READ_OUTCOME_SUCCESSFUL, size),
        ).fetchall()
        for (relative_path,) in paths:
            path = str(relative_path)
            connection.execute(
                "INSERT OR IGNORE INTO content_identity_work VALUES "
                "(?, ?, 'duplicate-proof', 'pending')",
                (run_id, path),
            )
            if size == 0:
                connection.execute(
                    "INSERT OR IGNORE INTO content_identities VALUES (?, ?, ?, ?, 0, ?, ?)",
                    (
                        run_id,
                        path,
                        CONTENT_IDENTITY_ALGORITHM,
                        CONTENT_IDENTITY_ALGORITHM_VERSION,
                        blake3().hexdigest(),
                        READ_OUTCOME_SUCCESSFUL,
                    ),
                )
                connection.execute(
                    "UPDATE content_identity_work SET state = 'complete' "
                    "WHERE run_id = ? AND relative_path = ?",
                    (run_id, path),
                )
    connection.execute(
        "UPDATE analysis_candidates SET state = 'evaluated' WHERE run_id = ? "
        "AND candidate_kind = 'duplicate-size-group' AND byte_size = 0",
        (run_id,),
    )
    connection.execute(
        "UPDATE evidence_discovery_progress SET phase = 'hashing', cursor = NULL "
        "WHERE run_id = ?",
        (run_id,),
    )
    connection.commit()


def _run_identity_work(
    connection: sqlite3.Connection,
    run_id: str,
    selected_root: Path,
    progress: ProgressReporter,
) -> None:
    """Hash pending proof work once in bounded, restart-safe batches."""
    pending = connection.execute(
        "SELECT work.relative_path, inventory.observed_byte_size, inventory.modified_ns "
        "FROM content_identity_work AS work JOIN inventory_entries AS inventory "
        "ON inventory.run_id = work.run_id AND inventory.relative_path = work.relative_path "
        "WHERE work.run_id = ? AND work.state = 'pending' "
        "ORDER BY work.relative_path, work.purpose",
        (run_id,),
    ).fetchall()
    progress.start("Establishing required content identity", len(pending), unit="files")
    for start in range(0, len(pending), BATCH_SIZE):
        batch = pending[start : start + BATCH_SIZE]
        connection.execute("BEGIN IMMEDIATE")
        newly_hashed_files = 0
        newly_hashed_bytes = 0
        reused = 0
        for relative_path, observed_byte_size, modified_ns in batch:
            path = str(relative_path)
            established = connection.execute(
                "SELECT 1 FROM content_identities WHERE run_id = ? AND relative_path = ?",
                (run_id, path),
            ).fetchone()
            if established is not None:
                reused += 1
            else:
                identity = hash_regular_file(
                    selected_root / path,
                    path,
                    FileObservation.persisted(int(observed_byte_size), modified_ns),
                )
                connection.execute(
                    "INSERT INTO content_identities VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        path,
                        CONTENT_IDENTITY_ALGORITHM,
                        CONTENT_IDENTITY_ALGORITHM_VERSION,
                        identity.byte_size,
                        identity.digest,
                        identity.read_outcome,
                    ),
                )
                newly_hashed_files += 1
                if identity.read_outcome == READ_OUTCOME_SUCCESSFUL:
                    newly_hashed_bytes += identity.byte_size
                else:
                    connection.execute(
                        "UPDATE inventory_entries SET read_outcome = ? "
                        "WHERE run_id = ? AND relative_path = ?",
                        (identity.read_outcome, run_id, path),
                    )
                    connection.execute(
                        "INSERT OR REPLACE INTO skipped_entry_findings VALUES (?, ?, ?)",
                        (run_id, path, identity.read_outcome),
                    )
            connection.execute(
                "UPDATE content_identity_work SET state = 'complete' "
                "WHERE run_id = ? AND relative_path = ?",
                (run_id, path),
            )
        connection.execute(
            "UPDATE evidence_discovery_progress SET cursor = ?, "
            "hashed_file_count = hashed_file_count + ?, "
            "hashed_byte_count = hashed_byte_count + ?, "
            "reused_identity_count = reused_identity_count + ?, "
            "max_batch_size = MAX(max_batch_size, ?), "
            "checkpoint_count = checkpoint_count + 1 WHERE run_id = ?",
            (
                str(batch[-1][0]),
                newly_hashed_files,
                newly_hashed_bytes,
                reused,
                len(batch),
                run_id,
            ),
        )
        connection.commit()
        progress.advance(len(batch))
    progress.complete()
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "UPDATE analysis_candidates SET state = 'evaluated' WHERE run_id = ? "
        "AND candidate_kind = 'duplicate-size-group'",
        (run_id,),
    )
    connection.execute(
        "UPDATE evidence_discovery_progress SET phase = 'structural-candidates', "
        "cursor = NULL WHERE run_id = ?",
        (run_id,),
    )
    connection.commit()


def _prepare_structural_identity_work(
    connection: sqlite3.Connection, run_id: str
) -> None:
    """Schedule proof only for Directory Trees with metadata overlap.

    Same relative descendant path and byte size is candidate evidence, not a
    relationship conclusion. It identifies the bounded set of tree roots whose
    regular files need full identities before structural classification.
    """
    shared: dict[tuple[str, int], set[str]] = {}
    for relative_path, byte_size in connection.execute(
        "SELECT relative_path, observed_byte_size FROM inventory_entries "
        "WHERE run_id = ? AND entry_kind = 'regular-file' AND read_outcome = ? "
        "ORDER BY relative_path",
        (run_id, READ_OUTCOME_SUCCESSFUL),
    ):
        path = str(relative_path)
        parts = path.split("/")
        for length in range(1, len(parts)):
            root = "/".join(parts[:length])
            descendant = "/".join(parts[length:])
            shared.setdefault((descendant, int(byte_size)), set()).add(root)

    candidate_roots: set[str] = set()
    connection.execute("BEGIN IMMEDIATE")
    for roots in shared.values():
        ordered = sorted(roots)
        if len(ordered) < 2:
            continue
        candidate_roots.update(ordered)
        for left, right in pairwise(ordered):
            key = blake3(f"{len(left)}:{left}{len(right)}:{right}".encode()).hexdigest()
            connection.execute(
                "INSERT OR IGNORE INTO analysis_candidates "
                "(run_id, candidate_kind, candidate_key, byte_size, "
                "left_relative_path, right_relative_path, state) "
                "VALUES (?, 'structural-pair', ?, NULL, ?, ?, 'pending')",
                (run_id, key, left, right),
            )
    if candidate_roots:
        for (relative_path,) in connection.execute(
            "SELECT relative_path FROM inventory_entries WHERE run_id = ? "
            "AND entry_kind = 'regular-file' AND read_outcome = ? ORDER BY relative_path",
            (run_id, READ_OUTCOME_SUCCESSFUL),
        ):
            path = str(relative_path)
            if any(path.startswith(f"{root}/") for root in candidate_roots):
                connection.execute(
                    "INSERT OR IGNORE INTO content_identity_work VALUES "
                    "(?, ?, 'structural-proof', 'pending')",
                    (run_id, path),
                )
    connection.commit()


def _compute_and_persist_canonical_copies(
    connection: sqlite3.Connection, run_id: str
) -> None:
    """Persist one Canonical Copy decision for each proven Exact Duplicate group.

    Only equal complete content identities (matching algorithm, algorithm
    version, byte size, and digest, each with a successful read outcome)
    form a group. The default choice is the newest modification timestamp;
    a tie or unavailable timestamp falls back to the lexicographically
    smallest relative path, and that fallback is recorded in the reason.
    Recomputing is idempotent so it can safely rerun after a resume.
    """
    connection.execute("DELETE FROM canonical_copies WHERE run_id = ?", (run_id,))
    identity_rows = [
        row
        for row in select_identity_rows(connection, run_id)
        if row.read_outcome == READ_OUTCOME_SUCCESSFUL
        and row.algorithm == CONTENT_IDENTITY_ALGORITHM
        and row.algorithm_version == CONTENT_IDENTITY_ALGORITHM_VERSION
    ]
    groups = derive_exact_duplicate_groups(identity_rows)
    for group in groups:
        if len(group.member_relative_paths) == 1:
            continue
        connection.execute(
            "INSERT INTO canonical_copies VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                group.key.algorithm,
                group.key.algorithm_version,
                group.key.byte_size,
                group.key.digest,
                group.canonical_relative_path,
                group.canonical_reason,
            ),
        )


def _completion_counts(connection: sqlite3.Connection, run_id: str) -> tuple[int, int]:
    inventory_count = connection.execute(
        f"SELECT COUNT(*) FROM inventory_entries "
        f"WHERE run_id = ? AND entry_kind = 'regular-file' "
        f"AND read_outcome = '{READ_OUTCOME_SUCCESSFUL}'",
        (run_id,),
    ).fetchone()[0]
    skipped_count = connection.execute(
        "SELECT COUNT(*) FROM skipped_entry_findings WHERE run_id = ?", (run_id,)
    ).fetchone()[0]
    return int(inventory_count), int(skipped_count)


def _complete_evidence_discovery(connection: sqlite3.Connection, run_id: str) -> None:
    """Close the schema-versioned discovery counters for the current analyzer."""
    structural = connection.execute(
        "SELECT candidate_count, comparison_count FROM structural_analysis "
        "WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    connection.execute(
        "UPDATE evidence_discovery_progress SET phase = 'complete', cursor = NULL, "
        "emitted_candidate_count = ?, evaluated_candidate_count = ? WHERE run_id = ?",
        (
            int(structural[0]) if structural is not None else 0,
            int(structural[1]) if structural is not None else 0,
            run_id,
        ),
    )


def _release_writer_lease(
    connection: sqlite3.Connection, run_id: str, lease_token: str
) -> None:
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "UPDATE analysis_runs SET writer_lease = NULL "
        "WHERE run_id = ? AND writer_lease = ?",
        (run_id, lease_token),
    )
    connection.commit()


@_run_workspace_refusal
def create_analysis_run(
    selected_backup_root: Path,
    output_root: Path,
    *,
    progress: ProgressReporter | None = None,
) -> dict[str, object]:
    reporter = progress or NullProgressReporter()
    selected_root = _resolved_directory(selected_backup_root, "Selected Backup Root")
    resolved_output_root = output_root.expanduser().resolve(strict=False)
    require_outside_selected_root(
        resolved_output_root, "Run Output Root", selected_root
    )

    run_id = str(uuid.uuid4())
    snapshot_id = str(uuid.uuid4())
    analysis_run = (resolved_output_root / "runs" / run_id).resolve(strict=False)
    if not is_within(analysis_run, resolved_output_root):
        raise AnalysisRunError(
            f"Analysis Run must remain under the Run Output Root: {analysis_run}"
        )
    require_outside_selected_root(analysis_run, "Analysis Run", selected_root)

    analysis_run.mkdir(parents=True, exist_ok=False)
    database_path = analysis_run / DATABASE_NAME
    connection: sqlite3.Connection | None = None
    run_initialized = False
    run_completed = False
    lease_token = str(uuid.uuid4())
    try:
        connection = open_wal_read_write(database_path)
        _create_schema(connection)
        now = datetime.now(UTC).isoformat()
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO analysis_runs VALUES "
            "(?, ?, ?, ?, 'scanning', ?, NULL, ?, NULL, NULL)",
            (run_id, snapshot_id, SCHEMA_VERSION, str(selected_root), now, lease_token),
        )
        connection.execute(
            "INSERT INTO evidence_discovery_progress (run_id, phase) "
            "VALUES (?, 'inventory')",
            (run_id,),
        )
        connection.commit()
        run_initialized = True

        _run_scan_batches(
            connection, run_id, selected_root, resume_after=None, progress=reporter
        )

        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE analysis_runs SET status = 'hashing' WHERE run_id = ?", (run_id,)
        )
        connection.execute(
            "UPDATE evidence_discovery_progress SET phase = 'duplicate-candidates', "
            "cursor = NULL WHERE run_id = ?",
            (run_id,),
        )
        connection.commit()
        _prepare_duplicate_identity_work(connection, run_id)
        _run_identity_work(connection, run_id, selected_root, reporter)
        _prepare_structural_identity_work(connection, run_id)
        _run_identity_work(connection, run_id, selected_root, reporter)

        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE analysis_runs SET status = 'structural-analysis' WHERE run_id = ?",
            (run_id,),
        )
        connection.execute(
            "UPDATE evidence_discovery_progress SET phase = 'structural-candidates', "
            "cursor = NULL WHERE run_id = ?",
            (run_id,),
        )
        connection.commit()
        reporter.start("Structural analysis")
        analyze_structural_relationships(connection, run_id, str(selected_root))
        reporter.complete()

        connection.execute("BEGIN IMMEDIATE")
        reporter.start("Computing canonical copies")
        _compute_and_persist_canonical_copies(connection, run_id)
        reporter.complete()
        _complete_evidence_discovery(connection, run_id)
        completed_at = datetime.now(UTC).isoformat()
        connection.execute(
            "UPDATE analysis_runs SET status = 'complete', completed_at = ?, "
            "writer_lease = NULL WHERE run_id = ?",
            (completed_at, run_id),
        )
        connection.commit()
        run_completed = True
        inventory_count, skipped_count = _completion_counts(connection, run_id)
    except BaseException:
        if connection is not None and connection.in_transaction:
            connection.rollback()
        raise
    finally:
        if connection is not None:
            if run_initialized and not run_completed:
                _release_writer_lease(connection, run_id, lease_token)
            connection.close()
        if not run_initialized:
            shutil.rmtree(analysis_run)

    return {
        "analysis_run": str(analysis_run),
        "inventory_count": inventory_count,
        "run_id": run_id,
        "selected_backup_root": str(selected_root),
        "skipped_count": skipped_count,
        "snapshot_id": snapshot_id,
        "status": "complete",
    }


@_run_workspace_refusal
def resume_analysis_run(
    analysis_run: Path, *, progress: ProgressReporter | None = None
) -> dict[str, object]:
    reporter = progress or NullProgressReporter()
    run_path, database_path = resolve_run_directory(analysis_run)

    connection = open_wal_read_write(database_path)
    lease_token: str | None = None
    run_completed = False
    run_id = ""
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute("SELECT * FROM analysis_runs").fetchall()
        if len(rows) != 1:
            connection.rollback()
            raise AnalysisRunError(
                f"Analysis Run directory must contain exactly one run identity: {run_path}"
            )
        row = rows[0]
        run_id = row["run_id"]
        if int(row["schema_version"]) != SCHEMA_VERSION:
            connection.rollback()
            raise AnalysisRunError(
                "Analysis Run uses unsupported clean-cutover evidence schema; "
                f"create a new Analysis Run: {run_path}"
            )
        if row["status"] == "complete":
            connection.rollback()
            raise AnalysisRunError(f"Analysis Run is already complete: {run_path}")
        if row["writer_lease"] is not None:
            connection.rollback()
            raise AnalysisRunError(
                f"Analysis Run already has an active writer lease: {run_path}"
            )
        lease_token = str(uuid.uuid4())
        connection.execute(
            "UPDATE analysis_runs SET writer_lease = ? WHERE run_id = ?",
            (lease_token, run_id),
        )
        connection.commit()

        selected_root = Path(row["selected_backup_root"])
        if row["status"] == "scanning":
            _run_scan_batches(
                connection,
                run_id,
                selected_root,
                resume_after=row["checkpoint_relative_path"],
                progress=reporter,
            )
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE analysis_runs SET status = 'hashing' WHERE run_id = ?",
                (run_id,),
            )
            connection.execute(
                "UPDATE evidence_discovery_progress SET phase = 'duplicate-candidates', "
                "cursor = NULL WHERE run_id = ?",
                (run_id,),
            )
            connection.commit()
        if row["status"] in {"scanning", "hashing"}:
            _prepare_duplicate_identity_work(connection, run_id)
            _run_identity_work(connection, run_id, selected_root, reporter)
            _prepare_structural_identity_work(connection, run_id)
            _run_identity_work(connection, run_id, selected_root, reporter)

        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE analysis_runs SET status = 'structural-analysis' WHERE run_id = ?",
            (run_id,),
        )
        connection.execute(
            "UPDATE evidence_discovery_progress SET phase = 'structural-candidates', "
            "cursor = NULL WHERE run_id = ?",
            (run_id,),
        )
        connection.commit()
        reporter.start("Structural analysis")
        analyze_structural_relationships(connection, run_id, str(selected_root))
        reporter.complete()

        connection.execute("BEGIN IMMEDIATE")
        reporter.start("Computing canonical copies")
        _compute_and_persist_canonical_copies(connection, run_id)
        reporter.complete()
        _complete_evidence_discovery(connection, run_id)
        completed_at = datetime.now(UTC).isoformat()
        connection.execute(
            "UPDATE analysis_runs SET status = 'complete', completed_at = ?, "
            "writer_lease = NULL WHERE run_id = ?",
            (completed_at, run_id),
        )
        connection.commit()
        run_completed = True
        inventory_count, skipped_count = _completion_counts(connection, run_id)
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        if lease_token is not None and not run_completed:
            _release_writer_lease(connection, run_id, lease_token)
        connection.close()

    return {
        "analysis_run": str(run_path),
        "inventory_count": inventory_count,
        "run_id": run_id,
        "selected_backup_root": row["selected_backup_root"],
        "skipped_count": skipped_count,
        "snapshot_id": row["snapshot_id"],
        "status": "complete",
    }
