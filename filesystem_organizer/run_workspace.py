from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

DATABASE_NAME = "analysis.sqlite3"
SCHEMA_VERSION = 10
PLAN_SCHEMA_VERSION = 9
ATTEMPT_SCHEMA_VERSION = 1

_JOURNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS materialization_events (
  plan_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  event TEXT NOT NULL,
  operation_index INTEGER,
  detail TEXT,
  recorded_at TEXT NOT NULL,
  PRIMARY KEY (plan_id, seq),
  FOREIGN KEY (plan_id) REFERENCES consolidation_plans(plan_id)
);
CREATE INDEX IF NOT EXISTS materialization_event_operation_lookup
  ON materialization_events (plan_id, operation_index, seq);

CREATE TABLE IF NOT EXISTS materialization_attempts (
  attempt_id TEXT PRIMARY KEY,
  attempt_version INTEGER NOT NULL CHECK (attempt_version = 1),
  plan_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN (
    'ADMITTED', 'STAGING_STARTED', 'STAGING_DURABLE',
    'PUBLISHED', 'COMPLETE', 'FAILED'
  )),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT,
  planned_file_count INTEGER NOT NULL CHECK (planned_file_count >= 0),
  planned_byte_count INTEGER NOT NULL CHECK (planned_byte_count >= 0),
  cloned_file_count INTEGER NOT NULL DEFAULT 0 CHECK (cloned_file_count >= 0),
  cloned_byte_count INTEGER NOT NULL DEFAULT 0 CHECK (cloned_byte_count >= 0),
  streamed_file_count INTEGER NOT NULL DEFAULT 0 CHECK (streamed_file_count >= 0),
  streamed_byte_count INTEGER NOT NULL DEFAULT 0 CHECK (streamed_byte_count >= 0),
  failure_classification TEXT CHECK (failure_classification IN (
    'retryable-environment', 'unsupported-capability', 'source-drift',
    'invalid-plan', 'destination-conflict', 'io-integrity-manual-attention',
    'ambiguous-recovery', 'internal-failure'
  )),
  failure_detail TEXT,
  UNIQUE (attempt_id, plan_id),
  CHECK (
    (state = 'FAILED' AND completed_at IS NOT NULL
      AND failure_classification IS NOT NULL)
    OR (state = 'COMPLETE' AND completed_at IS NOT NULL
      AND failure_classification IS NULL AND failure_detail IS NULL)
    OR (state NOT IN ('FAILED', 'COMPLETE') AND completed_at IS NULL
      AND failure_classification IS NULL AND failure_detail IS NULL)
  ),
  FOREIGN KEY (plan_id) REFERENCES consolidation_plans(plan_id)
);
CREATE INDEX IF NOT EXISTS materialization_attempt_plan_lookup
  ON materialization_attempts (plan_id, created_at, attempt_id);

CREATE TABLE IF NOT EXISTS execution_manifests (
  attempt_id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL,
  manifest_version INTEGER NOT NULL CHECK (manifest_version = 3),
  mode TEXT NOT NULL CHECK (mode IN ('source-preserving', 'in-place')),
  run_id TEXT NOT NULL,
  selected_backup_root TEXT NOT NULL,
  canonical_destination TEXT NOT NULL,
  finalized_projection_fingerprint TEXT NOT NULL,
  staging_owner_attempt_id TEXT NOT NULL,
  staging_relative_path TEXT NOT NULL,
  staging_root_dev INTEGER,
  staging_root_ino INTEGER,
  created_at TEXT NOT NULL,
  UNIQUE (attempt_id, plan_id, run_id),
  CHECK (
    (staging_root_dev IS NULL AND staging_root_ino IS NULL)
    OR (staging_root_dev IS NOT NULL AND staging_root_ino IS NOT NULL)
  ),
  CHECK (staging_owner_attempt_id = attempt_id),
  FOREIGN KEY (attempt_id, plan_id)
    REFERENCES materialization_attempts(attempt_id, plan_id),
  FOREIGN KEY (plan_id, run_id) REFERENCES consolidation_plans(plan_id, run_id)
);
CREATE INDEX IF NOT EXISTS execution_manifest_plan_lookup
  ON execution_manifests (plan_id, created_at, attempt_id);
CREATE TABLE IF NOT EXISTS execution_manifest_entries (
  attempt_id TEXT NOT NULL,
  operation_index INTEGER NOT NULL,
  entry_kind TEXT NOT NULL,
  source_relative_path TEXT,
  output_relative_path TEXT NOT NULL,
  expected_byte_size INTEGER NOT NULL,
  algorithm TEXT,
  algorithm_version INTEGER,
  digest TEXT,
  expected_modified_ns INTEGER,
  temporary_relative_path TEXT,
  PRIMARY KEY (attempt_id, operation_index),
  FOREIGN KEY (attempt_id) REFERENCES execution_manifests(attempt_id)
);

CREATE TABLE IF NOT EXISTS execution_file_evidence (
  attempt_id TEXT NOT NULL,
  plan_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  entry_index INTEGER NOT NULL,
  source_relative_path TEXT NOT NULL,
  observed_byte_size INTEGER NOT NULL CHECK (observed_byte_size >= 0),
  observed_mtime_ns INTEGER NOT NULL,
  transfer_kind TEXT NOT NULL CHECK (transfer_kind IN ('streamed', 'native-clone')),
  algorithm TEXT,
  algorithm_version INTEGER,
  digest TEXT,
  PRIMARY KEY (attempt_id, entry_index),
  CHECK (
    (transfer_kind = 'streamed' AND algorithm IS NOT NULL
      AND algorithm_version IS NOT NULL AND digest IS NOT NULL)
    OR (transfer_kind = 'native-clone' AND algorithm IS NULL
      AND algorithm_version IS NULL AND digest IS NULL)
  ),
  FOREIGN KEY (attempt_id, plan_id, run_id)
    REFERENCES execution_manifests(attempt_id, plan_id, run_id),
  FOREIGN KEY (plan_id, entry_index)
    REFERENCES final_plan_entries(plan_id, entry_index),
  FOREIGN KEY (run_id, source_relative_path, observed_byte_size, observed_mtime_ns)
    REFERENCES inventory_entries(
      run_id, relative_path, observed_byte_size, modified_ns
    )
);
"""


class RunWorkspaceError(Exception):
    """A safe, user-facing refusal of a Run Workspace operation."""


def resolve_run_directory(analysis_run: Path) -> tuple[Path, Path]:
    """Resolve an Analysis Run directory and its database path.

    Returns ``(run_path, database_path)`` with the run directory resolved
    strictly and its ``analysis.sqlite3`` present, refusing anything else.
    """
    run_path = analysis_run.expanduser().resolve(strict=True)
    if not run_path.is_dir():
        raise RunWorkspaceError(f"Analysis Run is not a directory: {run_path}")
    database_path = run_path / DATABASE_NAME
    if not database_path.is_file():
        raise RunWorkspaceError(
            f"Analysis Run directory is missing {DATABASE_NAME}: {run_path}"
        )
    return run_path, database_path


def is_within(candidate: Path, directory: Path) -> bool:
    return candidate == directory or directory in candidate.parents


def require_outside_selected_root(
    candidate: Path, label: str, selected_root: Path
) -> None:
    """Refuse a destination path that lies inside the Selected Backup Root."""
    if is_within(candidate, selected_root):
        raise RunWorkspaceError(
            f"{label} must be outside the Selected Backup Root: {candidate}"
        )


def open_read_write(
    database_path: Path,
    *,
    connection_factory: type[sqlite3.Connection] = sqlite3.Connection,
) -> sqlite3.Connection:
    """Open a read-write connection with the run-database row configuration."""
    connection = sqlite3.connect(
        database_path, isolation_level=None, factory=connection_factory
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def admit_wal(connection: sqlite3.Connection) -> None:
    """Admit the connection's database to WAL mode, refusing other modes.

    The Run Output Root filesystem must support WAL journaling; any other
    result is a hard refusal so the run database never silently drops to a
    weaker durability mode.
    """
    mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    if str(mode).lower() != "wal":
        raise RunWorkspaceError(
            f"Run Output Root filesystem failed WAL admission: SQLite returned {mode!r}"
        )
    connection.execute("PRAGMA synchronous = FULL")


def open_wal_read_write(
    database_path: Path,
    *,
    connection_factory: type[sqlite3.Connection] = sqlite3.Connection,
) -> sqlite3.Connection:
    """Open a read-write connection admitted to WAL on its filesystem."""
    connection = (
        open_read_write(database_path)
        if connection_factory is sqlite3.Connection
        else open_read_write(database_path, connection_factory=connection_factory)
    )
    admit_wal(connection)
    return connection


def open_read_only(database_path: Path) -> sqlite3.Connection:
    """Open a read-only URI connection to an existing run database."""
    uri = f"file:{quote(str(database_path), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, isolation_level=None)
    connection.row_factory = sqlite3.Row
    return connection


@contextmanager
def read_only_transaction(database_path: Path) -> Iterator[sqlite3.Connection]:
    """Expose one stable read transaction and close it without committing."""
    connection = open_read_only(database_path)
    try:
        connection.execute("BEGIN")
        yield connection
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    """Whether a named table exists in the current schema snapshot."""
    row = connection.execute(
        "SELECT COUNT(*) FROM sqlite_schema WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return int(row[0]) == 1


def select_plan_row(connection: sqlite3.Connection, plan_id: str | None) -> sqlite3.Row:
    """Locate one plan row for a write operation, holding the caller's lock.

    Selects the explicitly identified plan, or the most recently created
    plan when ``plan_id`` is omitted, mirroring plan-report's
    default-selection behavior.
    """
    rows: list[sqlite3.Row]
    if plan_id is not None:
        rows = connection.execute(
            "SELECT * FROM consolidation_plans WHERE plan_id = ?", (plan_id,)
        ).fetchall()
        if not rows:
            raise RunWorkspaceError(f"Consolidation Plan not found: {plan_id}")
    else:
        rows = connection.execute(
            "SELECT * FROM consolidation_plans ORDER BY created_at DESC LIMIT 1"
        ).fetchall()
        if not rows:
            raise RunWorkspaceError("Analysis Run has no Consolidation Plan")
    plan = rows[0]
    run = connection.execute(
        "SELECT schema_version FROM analysis_runs WHERE run_id = ?", (plan["run_id"],)
    ).fetchone()
    if run is None or int(run["schema_version"]) != SCHEMA_VERSION:
        raise RunWorkspaceError(
            "Analysis Run uses unsupported clean-cutover evidence schema; "
            "create a new Analysis Run before using its Consolidation Plans"
        )
    if int(plan["schema_version"]) != PLAN_SCHEMA_VERSION:
        raise RunWorkspaceError(
            "Consolidation Plan uses unsupported clean-cutover Consolidation Plan schema; "
            "create a new Consolidation Plan"
        )
    return plan


def ensure_journal_schema(connection: sqlite3.Connection) -> None:
    """Add the append-only materialization journal to the run database."""
    connection.executescript(_JOURNAL_SCHEMA)
    manifest_columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(execution_manifest_entries)")
    }
    if "expected_modified_ns" not in manifest_columns:
        connection.execute(
            "ALTER TABLE execution_manifest_entries ADD COLUMN expected_modified_ns INTEGER"
        )


def journal_append(
    connection: sqlite3.Connection,
    plan_id: str,
    event: str,
    operation_index: int | None = None,
    detail: str | None = None,
) -> None:
    """Append one durable journal event for a plan.

    Each event is committed in its own ``BEGIN IMMEDIATE`` transaction under
    ``PRAGMA synchronous = FULL``, so the transition is durable before the
    next filesystem boundary begins.
    """
    recorded_at = datetime.now(UTC).isoformat()
    connection.execute("BEGIN IMMEDIATE")
    try:
        next_seq = connection.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 FROM materialization_events "
            "WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO materialization_events VALUES (?, ?, ?, ?, ?, ?)",
            (plan_id, int(next_seq), event, operation_index, detail, recorded_at),
        )
    except BaseException:
        connection.rollback()
        raise
    connection.commit()


def journal_append_many(
    connection: sqlite3.Connection,
    plan_id: str,
    events: list[tuple[str, int | None, str | None]],
) -> None:
    """Append compact outcomes in one durable SQLite transaction.

    Materialization records one outcome per completed file, rather than a
    transaction for every transient copy state.  A publication that reaches
    disk before this transaction is reconciled from the execution manifest on
    the next invocation.
    """
    if not events:
        return
    recorded_at = datetime.now(UTC).isoformat()
    connection.execute("BEGIN IMMEDIATE")
    try:
        next_seq = int(
            connection.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 FROM materialization_events "
                "WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()[0]
        )
        connection.executemany(
            "INSERT INTO materialization_events VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    plan_id,
                    next_seq + offset,
                    event,
                    operation_index,
                    detail,
                    recorded_at,
                )
                for offset, (event, operation_index, detail) in enumerate(events)
            ],
        )
    except BaseException:
        connection.rollback()
        raise
    connection.commit()
