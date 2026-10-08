from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast

from .consolidation_plan import (
    PLAN_SCHEMA_VERSION,
    PLAN_STATUS_DRAFT,
    PLAN_STATUS_FINALIZED,
)
from .materialization import (
    EVENT_MATERIALIZATION_COMPLETE,
    EVENT_MATERIALIZATION_FAILED,
)
from .run_workspace import (
    DATABASE_NAME,
    SCHEMA_VERSION,
    read_only_transaction,
    table_exists,
)

RUN_STATUSES = {"scanning", "hashing", "structural-analysis", "complete"}


class ControlPlaneError(Exception):
    """A safe, user-facing refusal of an agent control-plane operation."""


def _safe_database_path(run_path: Path) -> Path:
    database_path = run_path / DATABASE_NAME
    if database_path.is_symlink() or not database_path.is_file():
        raise ControlPlaneError(
            f"Analysis Run directory is missing a safe {DATABASE_NAME}: {run_path}"
        )
    return database_path


def _validated_run_row(connection: sqlite3.Connection, run_path: Path) -> sqlite3.Row:
    rows = connection.execute("SELECT * FROM analysis_runs").fetchall()
    if len(rows) != 1:
        raise ControlPlaneError(
            f"Analysis Run directory must contain exactly one run identity: {run_path}"
        )
    row = rows[0]
    schema_version = int(row["schema_version"])
    if schema_version != SCHEMA_VERSION:
        raise ControlPlaneError(
            f"Unsupported Analysis Run schema version {schema_version}: {run_path}"
        )
    status = str(row["status"])
    if status not in RUN_STATUSES:
        raise ControlPlaneError(
            f"Unsupported Analysis Run status {status!r}: {run_path}"
        )
    return cast(sqlite3.Row, row)


def _run_summary(run_path: Path) -> dict[str, object]:
    database_path = _safe_database_path(run_path)

    with read_only_transaction(database_path) as connection:
        row = _validated_run_row(connection, run_path)
        return {
            "analysis_run": str(run_path),
            "completed_at": row["completed_at"],
            "run_id": str(row["run_id"]),
            "selected_backup_root": str(row["selected_backup_root"]),
            "snapshot_id": str(row["snapshot_id"]),
            "started_at": str(row["started_at"]),
            "status": str(row["status"]),
        }


def _plan_summaries(
    connection: sqlite3.Connection, run_id: str, run_path: Path
) -> list[dict[str, object]]:
    if table_exists(connection, "consolidation_plans"):
        rows = connection.execute(
            "SELECT * FROM consolidation_plans WHERE run_id = ? "
            "ORDER BY created_at, plan_id",
            (run_id,),
        ).fetchall()
    else:
        return []

    if table_exists(connection, "materialization_events"):
        event_rows = connection.execute(
            "SELECT plan_id, event FROM materialization_events ORDER BY plan_id, seq"
        ).fetchall()
    else:
        event_rows = []
    events_by_plan: dict[str, list[str]] = {}
    for event_row in event_rows:
        events_by_plan.setdefault(str(event_row["plan_id"]), []).append(
            str(event_row["event"])
        )
    if table_exists(connection, "materialization_attempts"):
        attempt_rows = connection.execute(
            "SELECT plan_id, state FROM materialization_attempts "
            "ORDER BY created_at DESC, attempt_id DESC"
        ).fetchall()
    else:
        attempt_rows = []
    latest_attempt_state = {
        str(attempt["plan_id"]): str(attempt["state"])
        for attempt in reversed(attempt_rows)
    }

    summaries: list[dict[str, object]] = []
    for row in rows:
        schema_version = int(row["schema_version"])
        if schema_version != PLAN_SCHEMA_VERSION:
            raise ControlPlaneError(
                "Unsupported Consolidation Plan schema version "
                f"{schema_version}: {run_path}"
            )
        status = str(row["status"])
        if status not in {PLAN_STATUS_DRAFT, PLAN_STATUS_FINALIZED}:
            raise ControlPlaneError(
                f"Unsupported Consolidation Plan status {status!r}: {run_path}"
            )
        plan_id = str(row["plan_id"])
        events = events_by_plan.get(plan_id, [])
        attempt_state = latest_attempt_state.get(plan_id)
        if attempt_state == "FAILED" or EVENT_MATERIALIZATION_FAILED in events:
            materialization_status = "needs-attention"
        elif attempt_state == "COMPLETE" or EVENT_MATERIALIZATION_COMPLETE in events:
            materialization_status = "complete"
        elif events:
            materialization_status = "in-progress"
        else:
            materialization_status = "not-started"
        summaries.append(
            {
                "created_at": str(row["created_at"]),
                "finalized_at": row["finalized_at"],
                "intended_destination": str(row["intended_destination"]),
                "materialization_status": materialization_status,
                "plan_id": plan_id,
                "recovery_available": materialization_status == "in-progress",
                "status": status,
            }
        )
    return summaries


def discover_analysis_runs(run_output_root: Path) -> dict[str, object]:
    """Discover Analysis Runs beneath one resolved Run Output Root."""
    resolved_root = run_output_root.expanduser().resolve(strict=False)
    if not resolved_root.exists():
        return {"run_output_root": str(resolved_root), "runs": []}
    if not resolved_root.is_dir():
        raise ControlPlaneError(f"Run Output Root is not a directory: {resolved_root}")

    runs_directory = resolved_root / "runs"
    if not runs_directory.exists():
        return {"run_output_root": str(resolved_root), "runs": []}
    if runs_directory.is_symlink() or not runs_directory.is_dir():
        raise ControlPlaneError(
            f"Run Output Root contains an unsafe runs directory: {runs_directory}"
        )

    summaries: list[dict[str, object]] = []
    for candidate in sorted(runs_directory.iterdir(), key=lambda path: path.name):
        if candidate.is_symlink() or not candidate.is_dir():
            raise ControlPlaneError(
                f"Run Output Root contains an unsafe Analysis Run entry: {candidate}"
            )
        summaries.append(_run_summary(candidate.resolve(strict=True)))
    run_ids = [str(summary["run_id"]) for summary in summaries]
    if len(run_ids) != len(set(run_ids)):
        raise ControlPlaneError(
            f"Run Output Root contains ambiguous duplicate run identities: {resolved_root}"
        )
    summaries.sort(key=lambda run: str(run["analysis_run"]))
    return {"run_output_root": str(resolved_root), "runs": summaries}


def inspect_analysis_run(analysis_run: Path) -> dict[str, object]:
    """Report persisted recovery state for one explicitly identified Analysis Run."""
    try:
        run_path = analysis_run.expanduser().resolve(strict=True)
    except FileNotFoundError as error:
        raise ControlPlaneError(
            f"Analysis Run does not exist: {analysis_run}"
        ) from error
    if not run_path.is_dir():
        raise ControlPlaneError(f"Analysis Run is not a directory: {run_path}")
    database_path = _safe_database_path(run_path)

    with read_only_transaction(database_path) as connection:
        row = _validated_run_row(connection, run_path)
        run_id = str(row["run_id"])
        inventory_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM inventory_entries WHERE run_id = ? "
                "AND entry_kind = 'regular-file' AND read_outcome = 'successful'",
                (run_id,),
            ).fetchone()[0]
        )
        skipped_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM skipped_entry_findings WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
        )
        plans = _plan_summaries(connection, run_id, run_path)
        return {
            "analysis_run": str(run_path),
            "checkpoint_relative_path": row["checkpoint_relative_path"],
            "completed_at": row["completed_at"],
            "hash_checkpoint_relative_path": row["hash_checkpoint_relative_path"],
            "inventory_count": inventory_count,
            "plans": plans,
            "recovery_available": (
                row["status"] != "complete" and row["writer_lease"] is None
            ),
            "run_id": run_id,
            "schema_version": int(row["schema_version"]),
            "selected_backup_root": str(row["selected_backup_root"]),
            "skipped_entry_finding_count": skipped_count,
            "snapshot_id": str(row["snapshot_id"]),
            "started_at": str(row["started_at"]),
            "status": str(row["status"]),
            "writer_active": row["writer_lease"] is not None,
        }
