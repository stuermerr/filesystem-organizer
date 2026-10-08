from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from filesystem_organizer import run_workspace
from filesystem_organizer.run_workspace import (
    DATABASE_NAME,
    PLAN_SCHEMA_VERSION,
    SCHEMA_VERSION,
    RunWorkspaceError,
    open_read_only,
    open_wal_read_write,
    require_outside_selected_root,
    resolve_run_directory,
    select_plan_row,
)


def test_open_wal_read_write_refuses_a_non_wal_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    non_wal_connection = sqlite3.connect(":memory:", isolation_level=None)
    non_wal_connection.row_factory = sqlite3.Row
    monkeypatch.setattr(
        run_workspace, "open_read_write", lambda _database: non_wal_connection
    )

    with pytest.raises(RunWorkspaceError, match="failed WAL admission"):
        open_wal_read_write(tmp_path / "analysis.sqlite3")


def test_open_wal_read_write_admits_a_wal_connection(tmp_path: Path) -> None:
    connection = open_wal_read_write(tmp_path / "analysis.sqlite3")
    try:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]
    finally:
        connection.close()
    assert str(mode).lower() == "wal"
    assert int(synchronous) == 2  # FULL


def test_open_read_only_connection_refuses_writes(tmp_path: Path) -> None:
    database_path = tmp_path / DATABASE_NAME
    connection = open_wal_read_write(database_path)
    connection.execute("CREATE TABLE probe (value TEXT)")
    connection.close()

    read_only = open_read_only(database_path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            read_only.execute("CREATE TABLE probe2 (value TEXT)")
    finally:
        read_only.close()


def test_require_outside_selected_root_refuses_a_destination_inside(tmp_path: Path) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    candidate = selected_root / "nested" / "output"

    with pytest.raises(
        RunWorkspaceError, match="Run Output Root must be outside the Selected Backup Root"
    ):
        require_outside_selected_root(candidate, "Run Output Root", selected_root)


def test_require_outside_selected_root_allows_a_destination_outside(tmp_path: Path) -> None:
    selected_root = tmp_path / "backup"
    selected_root.mkdir()
    candidate = tmp_path / "output"

    require_outside_selected_root(candidate, "Run Output Root", selected_root)


def test_resolve_run_directory_refuses_a_non_directory(tmp_path: Path) -> None:
    occupied_path = tmp_path / "occupied"
    occupied_path.write_bytes(b"not a directory")

    with pytest.raises(RunWorkspaceError, match="Analysis Run is not a directory"):
        resolve_run_directory(occupied_path)


def test_resolve_run_directory_refuses_a_directory_without_a_database(tmp_path: Path) -> None:
    run_path = tmp_path / "run"
    run_path.mkdir()

    with pytest.raises(RunWorkspaceError, match=f"missing {DATABASE_NAME}"):
        resolve_run_directory(run_path)


def test_resolve_run_directory_returns_run_and_database_paths(tmp_path: Path) -> None:
    run_path = tmp_path / "run"
    run_path.mkdir()
    (run_path / DATABASE_NAME).write_bytes(b"")

    resolved_run, resolved_database = resolve_run_directory(run_path)

    assert resolved_run == run_path
    assert resolved_database == run_path / DATABASE_NAME


def _connection_with_plan_rows(tmp_path: Path) -> tuple[sqlite3.Connection, Path]:
    database_path = tmp_path / DATABASE_NAME
    connection = open_wal_read_write(database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        CREATE TABLE analysis_runs (
          run_id TEXT PRIMARY KEY,
          snapshot_id TEXT NOT NULL,
          schema_version INTEGER NOT NULL,
          selected_backup_root TEXT NOT NULL,
          status TEXT NOT NULL,
          started_at TEXT NOT NULL,
          completed_at TEXT,
          writer_lease TEXT,
          checkpoint_relative_path TEXT,
          hash_checkpoint_relative_path TEXT
        );
        CREATE TABLE consolidation_plans (
          plan_id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL,
          snapshot_id TEXT NOT NULL,
          schema_version INTEGER NOT NULL,
          intended_destination TEXT NOT NULL,
          created_at TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'draft',
          finalized_at TEXT
        );
        """
    )
    connection.execute(
        "INSERT INTO analysis_runs VALUES ('run-1', 'snap-1', ?, '/backup', "
        "'complete', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', "
        "NULL, NULL, NULL)",
        (SCHEMA_VERSION,),
    )
    connection.execute(
        "INSERT INTO consolidation_plans VALUES "
        "('plan-old', 'run-1', 'snap-1', ?, '/materialized', "
        "'2026-01-01T00:00:00+00:00', 'draft', NULL)",
        (PLAN_SCHEMA_VERSION,),
    )
    connection.execute(
        "INSERT INTO consolidation_plans VALUES "
        "('plan-new', 'run-1', 'snap-1', ?, '/materialized', "
        "'2026-01-02T00:00:00+00:00', 'draft', NULL)",
        (PLAN_SCHEMA_VERSION,),
    )
    return connection, database_path


def test_select_plan_row_prefers_an_explicit_identifier(tmp_path: Path) -> None:
    connection, _database_path = _connection_with_plan_rows(tmp_path)
    try:
        row = select_plan_row(connection, "plan-old")
    finally:
        connection.close()
    assert str(row["plan_id"]) == "plan-old"


def test_select_plan_row_defaults_to_most_recently_created(tmp_path: Path) -> None:
    connection, _database_path = _connection_with_plan_rows(tmp_path)
    try:
        row = select_plan_row(connection, None)
    finally:
        connection.close()
    assert str(row["plan_id"]) == "plan-new"


def test_select_plan_row_refuses_an_unknown_identifier(tmp_path: Path) -> None:
    connection, _database_path = _connection_with_plan_rows(tmp_path)
    try:
        with pytest.raises(RunWorkspaceError, match="Consolidation Plan not found"):
            select_plan_row(connection, "missing-plan")
    finally:
        connection.close()


def test_select_plan_row_refuses_a_database_without_plans(tmp_path: Path) -> None:
    database_path = tmp_path / DATABASE_NAME
    connection = open_wal_read_write(database_path)
    connection.executescript(
        """
        CREATE TABLE consolidation_plans (
          plan_id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL,
          snapshot_id TEXT NOT NULL,
          schema_version INTEGER NOT NULL,
          intended_destination TEXT NOT NULL,
          created_at TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'draft',
          finalized_at TEXT
        );
        """
    )
    try:
        with pytest.raises(RunWorkspaceError, match="has no Consolidation Plan"):
            select_plan_row(connection, None)
    finally:
        connection.close()
