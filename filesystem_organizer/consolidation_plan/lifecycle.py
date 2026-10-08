from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path

from ..linux_filesystem import LinuxFilesystemError, nonblocking_lock
from ..progress import NullProgressReporter, ProgressReporter
from ..run_workspace import (
    SCHEMA_VERSION,
    open_wal_read_write,
    require_outside_selected_root,
    resolve_run_directory,
    select_plan_row,
)
from .custom_layout import finalize_layout_projection, initialize_layout_baseline
from .models import (
    PLAN_SCHEMA_VERSION,
    PLAN_STATUS_DRAFT,
    PLAN_STATUS_FINALIZED,
    ConsolidationPlanError,
    LosslessConflictProjection,
    PlanOutputEntry,
    StructuralUnion,
    run_workspace_refusal,
)
from .projection import (
    _create_plan_schema,
    _retained_operations,
    _structural_entries,
    _structural_projections,
    _validate_output_entries,
    persist_plan_projection,
)


@run_workspace_refusal
def create_consolidation_plan(
    analysis_run: Path, *, progress: ProgressReporter | None = None
) -> dict[str, object]:
    """Create a draft plan and report its coarse-grained phases when requested."""
    reporter = progress or NullProgressReporter()
    run_path, database_path = resolve_run_directory(analysis_run)

    connection = open_wal_read_write(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute("SELECT * FROM analysis_runs").fetchall()
        if len(rows) != 1:
            connection.rollback()
            raise ConsolidationPlanError(
                f"Analysis Run directory must contain exactly one run identity: {run_path}"
            )
        row = rows[0]
        if row["status"] != "complete":
            connection.rollback()
            raise ConsolidationPlanError(f"Analysis Run is not complete: {run_path}")
        if int(row["schema_version"]) != SCHEMA_VERSION:
            connection.rollback()
            raise ConsolidationPlanError(
                "Analysis Run does not contain directory evidence required by the clean cutover; create a new "
                f"Analysis Run before creating a Consolidation Plan: {run_path}"
            )
        connection.commit()

        run_id = str(row["run_id"])
        snapshot_id = str(row["snapshot_id"])
        selected_root = Path(str(row["selected_backup_root"]))

        reporter.start("Preparing plan")
        _create_plan_schema(connection)

        reporter.start("Deriving plan operations")
        operations = _retained_operations(connection, run_id)
        structural_projections = _structural_projections(connection, run_id)
        structural_unions = [
            projection
            for projection in structural_projections
            if isinstance(projection, StructuralUnion)
        ]
        conflict_groups = [
            projection
            for projection in structural_projections
            if isinstance(projection, LosslessConflictProjection)
        ]
        reporter.start("Projecting directory structure")
        structural_entries, covered_files, structural_roots, conflict_mappings = (
            _structural_entries(connection, run_id, structural_projections)
        )
        retained_entries = [
            PlanOutputEntry(
                "file",
                operation.output_relative_path,
                operation.source_relative_path,
                operation.expected_byte_size,
                operation.algorithm,
                operation.algorithm_version,
                operation.digest,
                operation.canonical_reason or "unique file",
                operation.modified_ns,
                operation.evidence_kind,
            )
            for operation in operations
            if operation.source_relative_path not in covered_files
        ]
        output_entries = structural_entries + retained_entries
        output_entries = [
            replace(
                entry,
                evidence_kind=(
                    "layout-owned-directory"
                    if entry.entry_kind == "directory"
                    else (
                        "content-identity"
                        if entry.digest is not None
                        else "metadata-observation"
                    )
                ),
            )
            for entry in output_entries
        ]
        _validate_output_entries(output_entries)
        output_entries.sort(
            key=lambda entry: (entry.output_relative_path, entry.entry_kind)
        )

        plan_id = str(uuid.uuid4())
        run_output_root = run_path.parent.parent
        intended_destination = str(
            (run_output_root / "materialized" / run_id).resolve(strict=False)
        )
        require_outside_selected_root(
            Path(intended_destination), "Materialized Consolidation", selected_root
        )

        reporter.start("Persisting plan")
        connection.execute("BEGIN IMMEDIATE")
        created_at = datetime.now(UTC).isoformat()
        connection.execute(
            "INSERT INTO consolidation_plans VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan_id,
                run_id,
                snapshot_id,
                PLAN_SCHEMA_VERSION,
                intended_destination,
                created_at,
                PLAN_STATUS_DRAFT,
                None,
            ),
        )
        for index, operation in enumerate(operations):
            connection.execute(
                "INSERT INTO plan_operations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    plan_id,
                    index,
                    operation.source_relative_path,
                    operation.output_relative_path,
                    operation.expected_byte_size,
                    operation.algorithm,
                    operation.algorithm_version,
                    operation.digest,
                    operation.canonical_reason,
                    operation.modified_ns,
                    operation.evidence_kind,
                ),
            )
        for index, entry in enumerate(output_entries):
            connection.execute(
                "INSERT INTO plan_output_entries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    plan_id,
                    index,
                    entry.entry_kind,
                    entry.output_relative_path,
                    entry.source_relative_path,
                    entry.expected_byte_size,
                    entry.algorithm,
                    entry.algorithm_version,
                    entry.digest,
                    entry.reason,
                    entry.modified_ns,
                    entry.evidence_kind,
                ),
            )
        connection.executemany(
            "INSERT INTO plan_structural_roots VALUES (?, ?)",
            ((plan_id, root) for root in sorted(structural_roots)),
        )
        connection.executemany(
            "INSERT INTO plan_structural_unions VALUES (?, ?)",
            ((plan_id, index) for index, _union in enumerate(structural_unions)),
        )
        conflict_rows: list[tuple[object, ...]] = []
        next_conflict_index: dict[int, int] = {}
        for group_index, mapping in conflict_mappings:
            conflict_index = next_conflict_index.get(group_index, 0)
            next_conflict_index[group_index] = conflict_index + 1
            conflict_rows.append(
                (
                    plan_id,
                    group_index,
                    conflict_index,
                    mapping.source_relative_path,
                    mapping.output_relative_path,
                    mapping.entry_kind,
                    mapping.disposition,
                    mapping.reason,
                )
            )
        connection.executemany(
            "INSERT INTO plan_conflict_projections VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            conflict_rows,
        )
        persist_plan_projection(
            connection, plan_id, run_id, structural_unions, conflict_groups
        )
        initialize_layout_baseline(connection, plan_id)
        connection.commit()
        reporter.complete()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()

    return {
        "analysis_run": str(run_path),
        "intended_destination": intended_destination,
        "operation_count": sum(entry.entry_kind == "file" for entry in output_entries),
        "explicit_directory_count": sum(
            entry.entry_kind == "directory" for entry in output_entries
        ),
        "output_entry_count": len(output_entries),
        "plan_id": plan_id,
        "run_id": run_id,
        "schema_version": PLAN_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "status": PLAN_STATUS_DRAFT,
    }


def _with_run_lock[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    """Serialize plan finalization with same-run materialization admission."""

    @wraps(function)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        analysis_run = args[0] if args else kwargs["analysis_run"]
        run_path, _database_path = resolve_run_directory(Path(str(analysis_run)))
        try:
            with nonblocking_lock(run_path / ".filesystem-organizer.lock"):
                return function(*args, **kwargs)
        except LinuxFilesystemError as error:
            raise ConsolidationPlanError(str(error)) from error

    return wrapper


@run_workspace_refusal
def override_canonical_copy(
    analysis_run: Path,
    source_relative_path: str,
    reason: str,
    plan_id: str | None = None,
) -> dict[str, object]:
    """Replace a draft plan's Canonical Copy choice with another group member.

    ``source_relative_path`` must be a proven member (successful content
    identity) of the same Exact Duplicate group as the operation it
    replaces; a group of size one has no alternative member and is
    rejected. A non-empty ``reason`` is required. The override is refused
    visibly against a finalized plan: finalized operations never mutate,
    and a changed choice requires drafting a new plan instead.
    """
    reason = reason.strip()
    if not reason:
        raise ConsolidationPlanError("Override reason is required")

    run_path, database_path = resolve_run_directory(analysis_run)
    connection = open_wal_read_write(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        plan = select_plan_row(connection, plan_id)
        resolved_plan_id = str(plan["plan_id"])
        if plan["status"] != PLAN_STATUS_DRAFT:
            connection.rollback()
            raise ConsolidationPlanError(
                f"Consolidation Plan is not a draft: {resolved_plan_id}"
            )
        run_id = str(plan["run_id"])

        candidate = connection.execute(
            "SELECT algorithm, algorithm_version, byte_size, digest "
            "FROM content_identities "
            "WHERE run_id = ? AND relative_path = ? AND read_outcome = 'successful'",
            (run_id, source_relative_path),
        ).fetchone()
        if candidate is None:
            connection.rollback()
            raise ConsolidationPlanError(
                f"Not a proven Exact Duplicate group member: {source_relative_path}"
            )
        algorithm = str(candidate["algorithm"])
        algorithm_version = int(candidate["algorithm_version"])
        byte_size = int(candidate["byte_size"])
        digest = str(candidate["digest"])

        member_count = connection.execute(
            "SELECT COUNT(*) FROM content_identities "
            "WHERE run_id = ? AND algorithm = ? AND algorithm_version = ? "
            "AND byte_size = ? AND digest = ? AND read_outcome = 'successful'",
            (run_id, algorithm, algorithm_version, byte_size, digest),
        ).fetchone()[0]
        if member_count < 2:
            connection.rollback()
            raise ConsolidationPlanError(
                f"{source_relative_path} is not part of an Exact Duplicate group"
            )

        operation = connection.execute(
            "SELECT operation_index, source_relative_path FROM plan_operations "
            "WHERE plan_id = ? AND algorithm = ? AND algorithm_version = ? "
            "AND expected_byte_size = ? AND digest = ?",
            (resolved_plan_id, algorithm, algorithm_version, byte_size, digest),
        ).fetchone()
        if operation is None:
            connection.rollback()
            raise ConsolidationPlanError(
                "No plan operation matches the Exact Duplicate group for: "
                f"{source_relative_path}"
            )
        prior_relative_path = str(operation["source_relative_path"])

        overridden_reason = f"operator override: {reason}"
        connection.execute(
            "UPDATE plan_operations SET source_relative_path = ?, "
            "output_relative_path = ?, canonical_reason = ? "
            "WHERE plan_id = ? AND operation_index = ?",
            (
                source_relative_path,
                source_relative_path,
                overridden_reason,
                resolved_plan_id,
                operation["operation_index"],
            ),
        )
        connection.execute(
            "UPDATE plan_output_entries SET source_relative_path = ?, "
            "output_relative_path = CASE WHEN reason LIKE 'Structural Union (%' "
            "THEN output_relative_path ELSE ? END, reason = ? WHERE plan_id = ? "
            "AND entry_kind = 'file' AND algorithm = ? AND algorithm_version = ? "
            "AND expected_byte_size = ? AND digest = ?",
            (
                source_relative_path,
                source_relative_path,
                overridden_reason,
                resolved_plan_id,
                algorithm,
                algorithm_version,
                byte_size,
                digest,
            ),
        )
        connection.execute(
            "UPDATE plan_source_selections SET source_relative_path = ?, reason = ? "
            "WHERE plan_id = ? AND entry_id IN ("
            "SELECT entry_id FROM plan_baseline_entries WHERE plan_id = ? "
            "AND entry_kind = 'file' AND algorithm = ? AND algorithm_version = ? "
            "AND expected_byte_size = ? AND digest = ?)",
            (
                source_relative_path,
                overridden_reason,
                resolved_plan_id,
                resolved_plan_id,
                algorithm,
                algorithm_version,
                byte_size,
                digest,
            ),
        )
        next_override_index = connection.execute(
            "SELECT COALESCE(MAX(override_index), -1) + 1 FROM plan_overrides "
            "WHERE plan_id = ?",
            (resolved_plan_id,),
        ).fetchone()[0]
        created_at = datetime.now(UTC).isoformat()
        connection.execute(
            "INSERT INTO plan_overrides VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                resolved_plan_id,
                next_override_index,
                algorithm,
                algorithm_version,
                byte_size,
                digest,
                source_relative_path,
                prior_relative_path,
                reason,
                created_at,
            ),
        )
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()

    return {
        "analysis_run": str(run_path),
        "plan_id": resolved_plan_id,
        "prior_relative_path": prior_relative_path,
        "reason": reason,
        "source_relative_path": source_relative_path,
    }


@run_workspace_refusal
@_with_run_lock
def finalize_consolidation_plan(
    analysis_run: Path, plan_id: str | None = None, *, expected_revision: int | None = None
) -> dict[str, object]:
    """Explicitly freeze a draft plan's schema-versioned operations.

    A finalized plan never mutates again: a later desired change must
    draft a new Consolidation Plan instead, leaving this one unchanged.
    """
    run_path, database_path = resolve_run_directory(analysis_run)
    connection = open_wal_read_write(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        plan = select_plan_row(connection, plan_id)
        resolved_plan_id = str(plan["plan_id"])
        if plan["status"] == PLAN_STATUS_FINALIZED:
            connection.rollback()
            raise ConsolidationPlanError(
                f"Consolidation Plan is already finalized: {resolved_plan_id}"
            )
        if expected_revision is not None:
            active_revision = connection.execute(
                "SELECT active_revision FROM plan_layout_state WHERE plan_id = ?",
                (resolved_plan_id,),
            ).fetchone()
            if active_revision is None or int(active_revision[0]) != expected_revision:
                connection.rollback()
                raise ConsolidationPlanError(
                    "Layout Revision no longer matches the requested materialization revision"
                )
        finalized_at = datetime.now(UTC).isoformat()
        projection = finalize_layout_projection(
            connection, resolved_plan_id, finalized_at
        )
        connection.execute(
            "UPDATE consolidation_plans SET status = ?, finalized_at = ? "
            "WHERE plan_id = ?",
            (PLAN_STATUS_FINALIZED, finalized_at, resolved_plan_id),
        )
        connection.commit()
        run_id = str(plan["run_id"])
        snapshot_id = str(plan["snapshot_id"])
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()

    return {
        "analysis_run": str(run_path),
        "finalized_at": finalized_at,
        "plan_id": resolved_plan_id,
        "run_id": run_id,
        "snapshot_id": snapshot_id,
        "status": PLAN_STATUS_FINALIZED,
        **projection,
    }
