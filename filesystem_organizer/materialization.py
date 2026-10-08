from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import NoReturn

from blake3 import blake3

from .consolidation_plan import PLAN_STATUS_FINALIZED
from .content_identity import (
    CONTENT_IDENTITY_ALGORITHM,
    CONTENT_IDENTITY_ALGORITHM_VERSION,
    digest_file,
)
from .linux_filesystem import (
    CloneResult,
    LinuxFilesystemError,
    destination_lock_path,
    nonblocking_lock,
    probe_hard_link_support,
    publish_directory_no_replace,
    sync_filesystem,
    try_native_clone,
)
from .progress import NullProgressReporter, ProgressReporter
from .run_workspace import (
    ATTEMPT_SCHEMA_VERSION,
    RunWorkspaceError,
    ensure_journal_schema,
    journal_append,
    journal_append_many,
    open_read_only,
    open_read_write,
    require_outside_selected_root,
    resolve_run_directory,
    select_plan_row,
    table_exists,
)
from .structural_relationships import revalidate_structural_snapshot

STAGING_DIRECTORY_NAME = ".materialization-staging"
PARTIAL_OWNER_FILE = ".filesystem-organizer-owner"
EXECUTION_MANIFEST_VERSION = 3
EXECUTION_MODE_SOURCE_PRESERVING = "source-preserving"
EXECUTION_MODE_IN_PLACE = "in-place"
OUTCOME_BATCH_SIZE = 128

EVENT_PLAN_ADMITTED = "PLAN_ADMITTED"
EVENT_STRUCTURAL_SNAPSHOT_VERIFIED = "STRUCTURAL_SNAPSHOT_VERIFIED"
EVENT_FILE_COMPLETED = "FILE_COMPLETED"
EVENT_RECONCILIATION = "RECONCILIATION"
EVENT_MATERIALIZATION_FAILED = "MATERIALIZATION_FAILED"
EVENT_MATERIALIZATION_COMPLETE = "MATERIALIZATION_COMPLETE"
EVENT_DIRECTORY_CREATED = "DIRECTORY_CREATED"
EVENT_IN_PLACE_PROTECTION_COMPLETE = "IN_PLACE_PROTECTION_COMPLETE"

# Kept as public compatibility names for callers that imported the former
# journal vocabulary. New executions intentionally do not emit these events.
EVENT_COPY_STARTED = "COPY_STARTED"
EVENT_STAGING_WRITTEN = "STAGING_WRITTEN"
EVENT_STAGING_VERIFIED = "STAGING_VERIFIED"
EVENT_FINAL_PUBLISHED = "FINAL_PUBLISHED"

CRASH_POINT_ENVIRONMENT = "FSO_MATERIALIZATION_CRASH_POINT"


class MaterializationError(Exception):
    """A safe, user-facing refusal of a Materialized Consolidation operation."""


class _DestinationConflict(MaterializationError):
    """A no-overwrite publication conflict, recorded as needs-attention."""


def _run_workspace_refusal[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    @wraps(function)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return function(*args, **kwargs)
        except RunWorkspaceError as error:
            raise MaterializationError(str(error)) from error

    return wrapper


class _CrashSimulation(BaseException):
    """Simulate process termination at a durable materialization boundary."""


@dataclass(frozen=True)
class MaterializationPreflight:
    analysis_run: str
    plan_id: str
    run_id: str
    selected_backup_root: str
    destination: str
    operation_count: int
    structural_union_count: int
    explicit_directory_count: int
    total_bytes: int
    free_bytes: int
    sufficient_free_space: bool
    mode: str = EXECUTION_MODE_SOURCE_PRESERVING
    staging_metadata_bytes: int = 0
    recovery_state: str = "not-started"


def _maybe_crash(crash_point: str) -> None:
    if os.environ.get(CRASH_POINT_ENVIRONMENT) == crash_point:
        raise _CrashSimulation(crash_point)


def _plan_entries(connection: sqlite3.Connection, plan_id: str) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT entry_index AS operation_index, entry_kind, source_relative_path, "
        "output_relative_path, expected_byte_size, algorithm, algorithm_version, digest "
        ", evidence_kind, evidence_mtime_ns "
        "FROM final_plan_entries WHERE plan_id = ? ORDER BY entry_index",
        (plan_id,),
    ).fetchall()


def _excluded_file_entries(
    connection: sqlite3.Connection, plan_id: str
) -> list[sqlite3.Row]:
    """Frozen Custom Layout exclusions, kept separate from executable placements."""
    return connection.execute(
        "SELECT source_relative_path, expected_byte_size, digest "
        "FROM final_plan_exclusions WHERE plan_id = ? ORDER BY source_relative_path",
        (plan_id,),
    ).fetchall()


def _skipped_exclusions(connection: sqlite3.Connection, plan_id: str) -> list[str]:
    return [
        str(row["relative_path"])
        for row in connection.execute(
            "SELECT relative_path FROM final_plan_skipped_actions WHERE plan_id = ? "
            "AND action = 'exclude' ORDER BY relative_path",
            (plan_id,),
        )
    ]


def _require_in_place_skipped_actions(
    connection: sqlite3.Connection, plan_id: str
) -> None:
    expected = {
        str(row["relative_path"])
        for row in connection.execute(
            "SELECT relative_path FROM skipped_entry_findings WHERE run_id = "
            "(SELECT run_id FROM consolidation_plans WHERE plan_id = ?)",
            (plan_id,),
        )
    }
    configured = {
        str(row["relative_path"])
        for row in connection.execute(
            "SELECT relative_path FROM final_plan_skipped_actions WHERE plan_id = ?",
            (plan_id,),
        )
    }
    if missing := expected - configured:
        raise MaterializationError(
            "In-place materialization requires retain or exclude actions for skipped entries: "
            + ", ".join(sorted(missing))
        )


def _is_content_empty_result(connection: sqlite3.Connection, plan_id: str) -> bool:
    """Return the persisted approval fact; never infer it from mutable output rows."""
    row = connection.execute(
        "SELECT content_empty FROM final_plan_projection WHERE plan_id = ?", (plan_id,)
    ).fetchone()
    return row is not None and bool(row["content_empty"])


@_run_workspace_refusal
def content_empty_result(analysis_run: Path, plan_id: str) -> bool:
    _, database_path = resolve_run_directory(analysis_run)
    with open_read_only(database_path) as connection:
        select_plan_row(connection, plan_id)
        return _is_content_empty_result(connection, plan_id)


def _plan_structural_roots(connection: sqlite3.Connection, plan_id: str) -> set[str]:
    plan = connection.execute(
        "SELECT schema_version FROM consolidation_plans WHERE plan_id = ?", (plan_id,)
    ).fetchone()
    try:
        table = (
            "plan_projection_union_roots"
            if plan is not None and int(plan["schema_version"]) >= 3
            else "plan_structural_roots"
        )
        return {
            str(row[0])
            for row in connection.execute(
                f"SELECT root_relative_path FROM {table} WHERE plan_id = ?", (plan_id,)
            )
        }
    except sqlite3.OperationalError:
        return set()


def _plan_structural_union_count(
    connection: sqlite3.Connection, plan_id: str, run_id: str, roots: set[str]
) -> int:
    for table in ("plan_projection_unions", "plan_structural_unions"):
        try:
            count = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE plan_id = ?", (plan_id,)
                ).fetchone()[0]
            )
        except sqlite3.OperationalError:
            count = 0
        if count:
            return count
    if not roots:
        return 0
    try:
        rows = connection.execute(
            "SELECT digest, root_relative_path FROM directory_fingerprints "
            "WHERE run_id = ? ORDER BY digest, root_relative_path",
            (run_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return len(roots) // 2
    components: dict[bytes, set[str]] = {}
    for row in rows:
        components.setdefault(row["digest"], set()).add(str(row["root_relative_path"]))
    exact_roots: set[str] = set()
    exact_count = 0
    for component in components.values():
        if len(component) > 1 and component <= roots:
            exact_count += 1
            exact_roots.update(component)
    return exact_count + (len(roots - exact_roots) // 2)


def _nearest_existing_ancestor(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    raise MaterializationError(f"No existing ancestor directory found for: {path}")


def _path_entry_exists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _is_regular_no_follow(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_directories(paths: Iterable[Path]) -> None:
    for path in sorted({path.resolve(strict=True) for path in paths}, key=str):
        _fsync_directory(path)


def _has_plan_event(connection: sqlite3.Connection, plan_id: str, event: str) -> bool:
    if not table_exists(connection, "materialization_events"):
        return False
    return bool(
        connection.execute(
            "SELECT COUNT(*) FROM materialization_events "
            "WHERE plan_id = ? AND event = ? AND operation_index IS NULL",
            (plan_id, event),
        ).fetchone()[0]
    )


def _has_terminal_failure(connection: sqlite3.Connection, plan_id: str) -> bool:
    return bool(
        connection.execute(
            "SELECT COUNT(*) FROM materialization_events WHERE plan_id = ? AND event = ?",
            (plan_id, EVENT_MATERIALIZATION_FAILED),
        ).fetchone()[0]
    )


def _completed_indexes(connection: sqlite3.Connection, plan_id: str) -> set[int]:
    return {
        int(row[0])
        for row in connection.execute(
            "SELECT operation_index FROM materialization_events "
            "WHERE plan_id = ? AND event = ? AND operation_index IS NOT NULL",
            (plan_id, EVENT_FILE_COMPLETED),
        )
    }


def _operation_failure_message(plan_id: str, row: sqlite3.Row, reason: str) -> str:
    return (
        "materialization failed: "
        f"plan {plan_id} operation {int(row['operation_index'])} "
        f"source {row['source_relative_path']!s} "
        f"output {row['output_relative_path']!s}: {reason}"
    )


def _raise_operation_failure(
    connection: sqlite3.Connection,
    plan_id: str,
    row: sqlite3.Row,
    reason: str,
    *,
    conflict: bool = False,
) -> NoReturn:
    index = int(row["operation_index"])
    if conflict:
        journal_append(
            connection,
            plan_id,
            EVENT_RECONCILIATION,
            index,
            f"observed conflict: {reason}",
        )
    journal_append(connection, plan_id, EVENT_MATERIALIZATION_FAILED, index, reason)
    raise MaterializationError(_operation_failure_message(plan_id, row, reason))


def _revalidate_structural_snapshot(
    connection: sqlite3.Connection, run_id: str, selected_root: Path, plan_id: str
) -> set[str]:
    roots = _plan_structural_roots(connection, plan_id)
    try:
        revalidate_structural_snapshot(connection, run_id, selected_root, roots)
    except RuntimeError as error:
        raise MaterializationError(str(error)) from error
    return roots


def _source_same_filesystem(selected_root: Path, destination: Path) -> bool:
    return (
        selected_root.stat().st_dev
        == _nearest_existing_ancestor(destination).stat().st_dev
    )


def _destination_case_sensitive(destination: Path) -> bool:
    """Probe the actual destination volume without retaining a filesystem change."""
    parent = _nearest_existing_ancestor(destination)
    probe = parent / f".filesystem-organizer-case-probe-{uuid.uuid4().hex}"
    alternate = probe.with_name(probe.name.upper())
    try:
        probe.touch(exist_ok=False)
        return not alternate.exists()
    except OSError as error:
        raise MaterializationError(
            f"Cannot determine destination case compatibility: {parent}"
        ) from error
    finally:
        try:
            probe.unlink()
        except FileNotFoundError:
            pass


def _validate_destination_compatibility(
    entries: list[sqlite3.Row], destination: Path
) -> None:
    """Check destination-dependent names before any staging is admitted.

    Names are checked against the actual selected volume rather than the host
    platform, so a destination change necessarily receives a fresh probe.
    """
    paths = [str(entry["output_relative_path"]) for entry in entries]
    case_sensitive = _destination_case_sensitive(destination)
    folded: dict[str, str] = {}
    forbidden = set('<>:"/\\|?*')
    for path in paths:
        if os.name == "nt" and (
            any(character in forbidden for character in path)
            or any(part.rstrip(". ") != part for part in path.split("/"))
        ):
            raise MaterializationError(
                f"Destination does not support output name: {path}"
            )
        key = path if case_sensitive else path.casefold()
        prior = folded.get(key)
        if prior is not None and prior != path:
            raise MaterializationError(
                f"Destination has a case-equivalent output collision: {prior}, {path}"
            )
        folded[key] = path


def _probe_hard_link_support(selected_root: Path) -> None:
    """Refuse in-place execution before admission when hard links are unavailable."""
    try:
        probe_hard_link_support(selected_root)
    except LinuxFilesystemError as error:
        raise MaterializationError(
            "In-place materialization requires hard-link support and write permission "
            "in the Selected Backup Root"
        ) from error


def _preflight_materialization(
    analysis_run: Path,
    plan_id: str,
    destination: Path | None,
    *,
    in_place: bool,
    revalidate_sources: bool,
    revalidate_structural_sources: bool,
) -> MaterializationPreflight:
    if not plan_id:
        raise MaterializationError(
            "Materialization requires an explicit Consolidation Plan identifier"
        )
    run_path, database_path = resolve_run_directory(analysis_run)
    connection = open_read_only(database_path)
    try:
        connection.execute("BEGIN")
        plan = select_plan_row(connection, plan_id)
        resolved_plan_id = str(plan["plan_id"])
        if plan["status"] != PLAN_STATUS_FINALIZED:
            raise MaterializationError(
                f"Consolidation Plan is not finalized: {resolved_plan_id}"
            )
        run_id = str(plan["run_id"])
        selected_root = Path(
            str(
                connection.execute(
                    "SELECT selected_backup_root FROM analysis_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()["selected_backup_root"]
            )
        )
        if in_place:
            _require_in_place_skipped_actions(connection, resolved_plan_id)
            if destination is not None:
                raise MaterializationError("--in-place does not accept --destination")
            resolved_destination = selected_root
        else:
            resolved_destination = (
                Path(destination).expanduser().resolve(strict=False)
                if destination is not None
                else Path(str(plan["intended_destination"]))
                .expanduser()
                .resolve(strict=False)
            )
            require_outside_selected_root(
                resolved_destination,
                "Materialized Consolidation destination",
                selected_root,
            )
        if in_place and not _source_same_filesystem(selected_root, resolved_destination):
            raise MaterializationError(
                "Materialized Consolidation destination must be on the same filesystem "
                "as the Selected Backup Root"
            )
        recovery_state = "not-started"
        if in_place:
            staging = selected_root / _in_place_staging_name(resolved_plan_id)
            if _path_entry_exists(staging):
                manifest = (
                    connection.execute(
                        "SELECT mode FROM execution_manifests WHERE plan_id = ?",
                        (resolved_plan_id,),
                    ).fetchone()
                    if table_exists(connection, "execution_manifests")
                    else None
                )
                if manifest is None or manifest["mode"] != EXECUTION_MODE_IN_PLACE:
                    raise MaterializationError(
                        f"In-place staging directory has an unexpected occupant: {staging}"
                    )
                recovery_state = (
                    "staging-present; execution will reconcile proven state"
                )
            _probe_hard_link_support(selected_root)
        if not in_place and resolved_destination.exists():
            recovery_state = _existing_destination_recovery_state(
                connection, resolved_plan_id, run_id, selected_root, resolved_destination
            )
        roots = (
            _revalidate_structural_snapshot(
                connection, run_id, selected_root, resolved_plan_id
            )
            if revalidate_structural_sources
            else _plan_structural_roots(connection, resolved_plan_id)
        )
        entries = _plan_entries(connection, resolved_plan_id)
        _validate_destination_compatibility(entries, resolved_destination)
        if revalidate_sources:
            for entry in entries:
                if entry["entry_kind"] == "file":
                    _validate_source_metadata_observation(
                        selected_root / str(entry["source_relative_path"]),
                        int(entry["expected_byte_size"]),
                        entry["evidence_mtime_ns"],
                    )
                    _validate_source_before_copy(
                        selected_root / str(entry["source_relative_path"]),
                        int(entry["expected_byte_size"]),
                        entry["digest"],
                    )
        union_count = _plan_structural_union_count(
            connection, resolved_plan_id, run_id, roots
        )
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    files = [entry for entry in entries if entry["entry_kind"] == "file"]
    total_bytes = sum(int(entry["expected_byte_size"]) for entry in files)
    free_bytes = shutil.disk_usage(
        _nearest_existing_ancestor(resolved_destination)
    ).free
    staging_metadata_bytes = ((len(files) + len(entries) + 3) * 4096) + (1024 * 1024)
    return MaterializationPreflight(
        analysis_run=str(run_path),
        plan_id=resolved_plan_id,
        run_id=run_id,
        selected_backup_root=str(selected_root),
        destination=str(resolved_destination),
        operation_count=len(files),
        structural_union_count=union_count,
        explicit_directory_count=len(entries) - len(files),
        total_bytes=total_bytes,
        free_bytes=int(free_bytes),
        sufficient_free_space=(
            free_bytes >= staging_metadata_bytes
            if in_place
            else free_bytes >= total_bytes
        ),
        mode=EXECUTION_MODE_IN_PLACE if in_place else EXECUTION_MODE_SOURCE_PRESERVING,
        staging_metadata_bytes=staging_metadata_bytes if in_place else 0,
        recovery_state=recovery_state,
    )


def _existing_destination_recovery_state(
    connection: sqlite3.Connection,
    plan_id: str,
    run_id: str,
    selected_root: Path,
    destination: Path,
) -> str:
    """Accept only a final root proven to be an interrupted owned publication."""
    if not table_exists(connection, "materialization_attempts"):
        raise MaterializationError(
            f"Materialized Consolidation destination already exists: {destination}"
        )
    attempt = connection.execute(
        "SELECT attempt.attempt_id, attempt.state, manifest.attempt_id AS manifest_attempt_id, "
        "manifest.manifest_version, manifest.mode, manifest.run_id, "
        "manifest.selected_backup_root, manifest.canonical_destination, "
        "manifest.finalized_projection_fingerprint, manifest.staging_owner_attempt_id, "
        "manifest.staging_root_dev, manifest.staging_root_ino "
        "FROM materialization_attempts AS attempt "
        "LEFT JOIN execution_manifests AS manifest USING (attempt_id) "
        "WHERE attempt.plan_id = ? ORDER BY attempt.created_at DESC, attempt.attempt_id DESC LIMIT 1",
        (plan_id,),
    ).fetchone()
    if attempt is None:
        raise MaterializationError(
            f"Materialized Consolidation destination already exists: {destination}"
        )
    if attempt["state"] not in {"STAGING_DURABLE", "PUBLISHED"}:
        raise MaterializationError(
            f"Materialized Consolidation destination already exists: {destination}"
        )
    projection = connection.execute(
        "SELECT fingerprint FROM final_plan_projection WHERE plan_id = ?", (plan_id,)
    ).fetchone()
    ambiguous = (
        attempt["manifest_attempt_id"] is None
        or int(attempt["manifest_version"] or 0) != EXECUTION_MANIFEST_VERSION
        or attempt["mode"] != EXECUTION_MODE_SOURCE_PRESERVING
        or attempt["run_id"] != run_id
        or attempt["selected_backup_root"] != str(selected_root)
        or attempt["canonical_destination"] != str(destination)
        or projection is None
        or attempt["finalized_projection_fingerprint"] != projection["fingerprint"]
        or attempt["staging_owner_attempt_id"] != attempt["attempt_id"]
        or attempt["staging_root_dev"] is None
        or attempt["staging_root_ino"] is None
        or _partial_destination(destination, plan_id).exists()
    )
    try:
        destination_stat = destination.lstat()
    except OSError as error:
        raise MaterializationError(
            f"needs-attention: recovery-ambiguous final destination: {destination}"
        ) from error
    if (
        ambiguous
        or not stat.S_ISDIR(destination_stat.st_mode)
        or stat.S_ISLNK(destination_stat.st_mode)
        or (destination_stat.st_dev, destination_stat.st_ino)
        != (int(attempt["staging_root_dev"]), int(attempt["staging_root_ino"]))
    ):
        raise MaterializationError(
            f"needs-attention: recovery-ambiguous final destination: {destination}"
        )
    return str(attempt["state"])


def _validate_source_before_copy(
    source: Path, expected_size: int, expected_digest: str | None
) -> None:
    try:
        observed_size, observed_digest = digest_file(source)
    except FileNotFoundError as error:
        raise MaterializationError("source file is missing") from error
    except OSError as error:
        raise MaterializationError("source file is unreadable") from error
    if observed_size != expected_size or (
        expected_digest is not None and observed_digest != expected_digest
    ):
        raise MaterializationError(
            "source file content no longer matches the plan's content identity"
        )


def _validate_source_metadata_observation(
    source: Path, expected_size: int, expected_modified_ns: int | None
) -> None:
    try:
        observed = source.lstat()
    except FileNotFoundError as error:
        raise MaterializationError("source file is missing") from error
    except OSError as error:
        raise MaterializationError("source file is unreadable") from error
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_size != expected_size
        or (
            expected_modified_ns is not None
            and observed.st_mtime_ns != int(expected_modified_ns)
        )
    ):
        raise MaterializationError("source file metadata no longer matches the plan")


@_run_workspace_refusal
def preflight_materialization(
    analysis_run: Path,
    plan_id: str,
    destination: Path | None = None,
    *,
    in_place: bool = False,
) -> MaterializationPreflight:
    return _preflight_materialization(
        analysis_run,
        plan_id,
        destination,
        in_place=in_place,
        revalidate_sources=True,
        revalidate_structural_sources=True,
    )


@_run_workspace_refusal
def preflight_materialization_for_execution(
    analysis_run: Path,
    plan_id: str,
    destination: Path | None = None,
    *,
    in_place: bool = False,
) -> MaterializationPreflight:
    return _preflight_materialization(
        analysis_run,
        plan_id,
        destination,
        in_place=in_place,
        revalidate_sources=False,
        revalidate_structural_sources=False,
    )


def render_preflight_summary(preflight: MaterializationPreflight) -> str:
    sufficient = "sufficient" if preflight.sufficient_free_space else "insufficient"
    in_place_lines = (
        (
            f"  In-place staging metadata reservation: {preflight.staging_metadata_bytes} bytes (includes margin)",
            f"  Recovery state: {preflight.recovery_state}",
            "  Source must remain quiescent while destructive execution runs.",
        )
        if preflight.mode == EXECUTION_MODE_IN_PLACE
        else ()
    )
    return "\n".join(
        (
            "Materialize Consolidation Plan",
            f"  Plan ID: {preflight.plan_id}",
            f"  Source (Selected Backup Root): {preflight.selected_backup_root}",
            f"  Destination: {preflight.destination}",
            f"  Operation count: {preflight.operation_count}",
            f"  Structural Union count: {preflight.structural_union_count}",
            f"  Explicit directory count: {preflight.explicit_directory_count}",
            f"  Total bytes: {preflight.total_bytes}",
            f"  Free space at destination: {preflight.free_bytes} bytes ({sufficient})",
            *in_place_lines,
            "",
        )
    )


def _ensure_execution_manifest(
    connection: sqlite3.Connection,
    preflight: MaterializationPreflight,
    staging_directory_name: str,
) -> list[sqlite3.Row]:
    manifest = connection.execute(
        "SELECT attempt_id, manifest_version, mode, run_id, selected_backup_root, "
        "canonical_destination, finalized_projection_fingerprint "
        "FROM execution_manifests WHERE plan_id = ? "
        "ORDER BY created_at DESC, attempt_id DESC LIMIT 1",
        (preflight.plan_id,),
    ).fetchone()
    if manifest is None:
        entries = _plan_entries(connection, preflight.plan_id)
        attempt_id = str(uuid.uuid4())
        connection.execute("BEGIN IMMEDIATE")
        try:
            projection_fingerprint = str(
                connection.execute(
                    "SELECT fingerprint FROM final_plan_projection WHERE plan_id = ?",
                    (preflight.plan_id,),
                ).fetchone()[0]
            )
            file_count = sum(row["entry_kind"] == "file" for row in entries)
            connection.execute(
                "INSERT INTO materialization_attempts "
                "(attempt_id, attempt_version, plan_id, state, created_at, updated_at, "
                "completed_at, planned_file_count, planned_byte_count) "
                "VALUES (?, ?, ?, 'ADMITTED', datetime('now'), datetime('now'), NULL, ?, ?)",
                (
                    attempt_id,
                    ATTEMPT_SCHEMA_VERSION,
                    preflight.plan_id,
                    file_count,
                    preflight.total_bytes,
                ),
            )
            connection.execute(
                "INSERT INTO execution_manifests "
                "(attempt_id, plan_id, manifest_version, mode, run_id, "
                "selected_backup_root, canonical_destination, "
                "finalized_projection_fingerprint, staging_owner_attempt_id, "
                "staging_relative_path, staging_root_dev, staging_root_ino, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, datetime('now'))",
                (
                    attempt_id,
                    preflight.plan_id,
                    EXECUTION_MANIFEST_VERSION,
                    preflight.mode,
                    preflight.run_id,
                    preflight.selected_backup_root,
                    preflight.destination,
                    projection_fingerprint,
                    attempt_id,
                    (
                        staging_directory_name
                        if preflight.mode == EXECUTION_MODE_IN_PLACE
                        else f"{staging_directory_name}/{preflight.plan_id}"
                    ),
                ),
            )
            connection.executemany(
                "INSERT INTO execution_manifest_entries "
                "(attempt_id, operation_index, entry_kind, source_relative_path, "
                "output_relative_path, expected_byte_size, algorithm, algorithm_version, "
                "digest, expected_modified_ns, temporary_relative_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        attempt_id,
                        int(row["operation_index"]),
                        str(row["entry_kind"]),
                        row["source_relative_path"],
                        str(row["output_relative_path"]),
                        int(row["expected_byte_size"] or 0),
                        row["algorithm"],
                        row["algorithm_version"],
                        row["digest"],
                        row["evidence_mtime_ns"],
                        f"{staging_directory_name}/{preflight.plan_id}/{int(row['operation_index'])}.part"
                        if row["entry_kind"] == "file"
                        else None,
                    )
                    for row in entries
                ],
            )
        except BaseException:
            connection.rollback()
            raise
        connection.commit()
    elif (
        int(manifest["manifest_version"]) != EXECUTION_MANIFEST_VERSION
        or manifest["mode"] != preflight.mode
        or manifest["run_id"] != preflight.run_id
        or manifest["selected_backup_root"] != preflight.selected_backup_root
        or manifest["canonical_destination"] != preflight.destination
        or manifest["finalized_projection_fingerprint"]
        != connection.execute(
            "SELECT fingerprint FROM final_plan_projection WHERE plan_id = ?",
            (preflight.plan_id,),
        ).fetchone()[0]
    ):
        raise MaterializationError(
            "Execution manifest does not match this finalized plan, source, and destination"
        )
    return connection.execute(
        "SELECT operation_index, entry_kind, source_relative_path, output_relative_path, "
        "expected_byte_size, algorithm, algorithm_version, digest, expected_modified_ns, "
        "temporary_relative_path "
        "FROM execution_manifest_entries WHERE attempt_id = ("
        "SELECT attempt_id FROM execution_manifests WHERE plan_id = ? "
        "ORDER BY created_at DESC, attempt_id DESC LIMIT 1) ORDER BY operation_index",
        (preflight.plan_id,),
    ).fetchall()


def _complete_materialization_attempt(
    connection: sqlite3.Connection,
    plan_id: str,
    cloned_file_count: int,
    cloned_byte_count: int,
    streamed_file_count: int,
    streamed_byte_count: int,
) -> None:
    """Close the current legacy execution as one compact successful Attempt."""
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "UPDATE materialization_attempts SET state = 'COMPLETE', "
        "updated_at = datetime('now'), completed_at = datetime('now'), "
        "cloned_file_count = ?, cloned_byte_count = ?, "
        "streamed_file_count = ?, streamed_byte_count = ? "
        "WHERE attempt_id = (SELECT attempt_id FROM execution_manifests "
        "WHERE plan_id = ? ORDER BY created_at DESC, attempt_id DESC LIMIT 1)",
        (
            cloned_file_count,
            cloned_byte_count,
            streamed_file_count,
            streamed_byte_count,
            plan_id,
        ),
    )
    connection.commit()


def _set_attempt_state(
    connection: sqlite3.Connection, plan_id: str, state: str
) -> None:
    """Persist one coarse source-preserving execution phase transition."""
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "UPDATE materialization_attempts SET state = ?, updated_at = datetime('now') "
        "WHERE attempt_id = (SELECT attempt_id FROM execution_manifests "
        "WHERE plan_id = ? ORDER BY created_at DESC, attempt_id DESC LIMIT 1)",
        (state, plan_id),
    )
    connection.commit()


def _mark_attempt_failed(
    connection: sqlite3.Connection, plan_id: str, classification: str, detail: str
) -> None:
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "UPDATE materialization_attempts SET state = 'FAILED', "
        "updated_at = datetime('now'), completed_at = datetime('now'), "
        "failure_classification = ?, failure_detail = ? "
        "WHERE attempt_id = (SELECT attempt_id FROM execution_manifests "
        "WHERE plan_id = ? ORDER BY created_at DESC, attempt_id DESC LIMIT 1)",
        (classification, detail, plan_id),
    )
    connection.commit()


def _attempt_transfer_totals(
    connection: sqlite3.Connection, plan_id: str
) -> tuple[int, int, int, int]:
    row = connection.execute(
        "SELECT "
        "COALESCE(SUM(transfer_kind = 'native-clone'), 0), "
        "COALESCE(SUM(CASE WHEN transfer_kind = 'native-clone' "
        "THEN observed_byte_size ELSE 0 END), 0), "
        "COALESCE(SUM(transfer_kind = 'streamed'), 0), "
        "COALESCE(SUM(CASE WHEN transfer_kind = 'streamed' "
        "THEN observed_byte_size ELSE 0 END), 0) "
        "FROM execution_file_evidence WHERE plan_id = ?",
        (plan_id,),
    ).fetchone()
    assert row is not None
    return (int(row[0]), int(row[1]), int(row[2]), int(row[3]))


def _reconcile_published_destination(
    connection: sqlite3.Connection,
    preflight: MaterializationPreflight,
    final_root: Path,
) -> bool:
    """Finish only a publication proven by the durable staging-root identity."""
    _existing_destination_recovery_state(
        connection,
        preflight.plan_id,
        preflight.run_id,
        Path(preflight.selected_backup_root),
        final_root,
    )
    try:
        _fsync_directory(final_root.parent)
    except OSError as error:
        _mark_attempt_failed(
            connection,
            preflight.plan_id,
            "io-integrity-manual-attention",
            "recovery parent-directory fsync failed",
        )
        raise MaterializationError(
            f"needs-attention: recovery-ambiguous final destination: {final_root}"
        ) from error
    _set_attempt_state(connection, preflight.plan_id, "PUBLISHED")
    _complete_materialization_attempt(
        connection, preflight.plan_id, *_attempt_transfer_totals(connection, preflight.plan_id)
    )
    return True


def _partial_destination(destination: Path, plan_id: str) -> Path:
    """Name the complete owned normal-path staging tree beside its final path."""
    return destination.parent / f".{destination.name}.partial-{plan_id}"


def _partial_owner_payload(connection: sqlite3.Connection, plan_id: str) -> str:
    row = connection.execute(
        "SELECT manifest.attempt_id, manifest.canonical_destination, "
        "manifest.finalized_projection_fingerprint FROM execution_manifests AS manifest "
        "WHERE manifest.plan_id = ? ORDER BY manifest.created_at DESC, "
        "manifest.attempt_id DESC LIMIT 1",
        (plan_id,),
    ).fetchone()
    if row is None:
        raise MaterializationError("owned partial destination has no execution manifest")
    return "\n".join(str(value) for value in row) + "\n"


def _discard_proven_partial(
    connection: sqlite3.Connection, preflight: MaterializationPreflight, partial_root: Path
) -> None:
    """Discard only a partial tree bound to this exact durable manifest."""
    if partial_root.is_symlink() or not partial_root.is_dir():
        raise MaterializationError(
            f"needs-attention: partial destination is not a safe directory: {partial_root}"
        )
    owner_file = partial_root / PARTIAL_OWNER_FILE
    try:
        payload = owner_file.read_text(encoding="utf-8")
    except OSError as error:
        raise MaterializationError(
            f"needs-attention: partial destination ownership is ambiguous: {partial_root}"
        ) from error
    if payload != _partial_owner_payload(connection, preflight.plan_id):
        raise MaterializationError(
            f"needs-attention: partial destination ownership is ambiguous: {partial_root}"
        )
    shutil.rmtree(partial_root)
    sync_filesystem(partial_root.parent)


def _validate_manifest_algorithms(entries: list[sqlite3.Row], plan_id: str) -> None:
    if any(
        row["entry_kind"] == "file"
        and (
            (row["algorithm"] is None) != (row["digest"] is None)
            or (
                row["algorithm"] is not None
                and (
                    row["algorithm"] != CONTENT_IDENTITY_ALGORITHM
                    or int(row["algorithm_version"])
                    != CONTENT_IDENTITY_ALGORITHM_VERSION
                )
            )
        )
        for row in entries
    ):
        raise MaterializationError(
            f"Consolidation Plan {plan_id} uses an unsupported content-identity algorithm"
        )


def _validate_legacy_operation_algorithms(
    connection: sqlite3.Connection, plan_id: str
) -> None:
    """Reject a plan whose original operation evidence no longer matches BLAKE3."""
    rows = connection.execute(
        "SELECT algorithm, algorithm_version FROM plan_operations WHERE plan_id = ?",
        (plan_id,),
    ).fetchall()
    if any(
        row["algorithm"] is not None
        and (
            row["algorithm"] != CONTENT_IDENTITY_ALGORITHM
            or int(row["algorithm_version"]) != CONTENT_IDENTITY_ALGORITHM_VERSION
        )
        for row in rows
    ):
        raise MaterializationError(
            f"Consolidation Plan {plan_id} uses an unsupported content-identity algorithm"
        )


def _copy_to_temporary(
    source: Path, temporary: Path, row: sqlite3.Row
) -> tuple[int, str]:
    expected_size = int(row["expected_byte_size"])
    expected_digest = row["digest"]
    try:
        before_path = source.lstat()
    except FileNotFoundError as error:
        raise MaterializationError("source file is missing") from error
    except OSError as error:
        raise MaterializationError("source file is unreadable") from error
    if not stat.S_ISREG(before_path.st_mode) or before_path.st_size != expected_size:
        raise MaterializationError("source file byte size no longer matches the plan")
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        temporary_fd = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
    except BaseException:
        os.close(source_fd)
        raise
    try:
        opened = os.fstat(source_fd)
        hasher = blake3()
        total = 0
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            hasher.update(chunk)
            _write_all(temporary_fd, chunk)
            _maybe_crash("copying")
        after_fd = os.fstat(source_fd)
        after_path = source.lstat()
        stable = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
        if total != expected_size or not all(
            getattr(before_path, field)
            == getattr(opened, field)
            == getattr(after_fd, field)
            == getattr(after_path, field)
            for field in stable
        ):
            raise MaterializationError("source file changed while being read")
        observed_digest = hasher.hexdigest()
        if expected_digest is not None and observed_digest != expected_digest:
            raise MaterializationError(
                "source file content no longer matches the plan's content identity"
            )
        try:
            os.fchmod(temporary_fd, stat.S_IMODE(before_path.st_mode))
            os.utime(
                temporary_fd, ns=(before_path.st_atime_ns, before_path.st_mtime_ns)
            )
        except OSError:
            # Mode and mtime are deliberately best effort; copied bytes remain valid.
            pass
        os.fsync(temporary_fd)
    finally:
        os.close(temporary_fd)
        os.close(source_fd)
    return total, observed_digest


def _write_all(descriptor: int, chunk: bytes) -> None:
    """Write one streamed source chunk completely before reading the next one."""
    view = memoryview(chunk)
    while view:
        written = os.write(descriptor, view)
        if written == 0:
            raise OSError("temporary file write made no progress")
        view = view[written:]


def _in_place_staging_name(plan_id: str) -> str:
    return f".filesystem-organizer-staging-{plan_id}"


def _validate_source_before_link(
    source: Path, row: sqlite3.Row, expected_modified_ns: int
) -> None:
    before = source.lstat()
    if before.st_mtime_ns != expected_modified_ns:
        raise MaterializationError(
            "source file modification time no longer matches the plan"
        )
    _validate_source_before_copy(
        source, int(row["expected_byte_size"]), row["digest"]
    )
    after = source.lstat()
    if not stat.S_ISREG(after.st_mode):
        raise MaterializationError("source file is not a regular file")
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise MaterializationError("source file changed while being validated")


def _remove_empty_directories(selected_root: Path, staging_root: Path) -> None:
    directories = sorted(
        (
            path
            for path in selected_root.rglob("*")
            if path.is_dir() and path != staging_root
        ),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            pass


def _materialize_in_place(
    analysis_run: Path, plan_id: str, progress: ProgressReporter
) -> dict[str, object]:
    progress.start("Preparing in-place materialization")
    preflight = preflight_materialization_for_execution(
        analysis_run, plan_id, in_place=True
    )
    run_path, database_path = resolve_run_directory(Path(preflight.analysis_run))
    selected_root = Path(preflight.selected_backup_root)
    staging_root = selected_root / _in_place_staging_name(preflight.plan_id)
    protected_root = staging_root / "protected"
    connection = open_read_write(database_path)
    try:
        connection.execute("PRAGMA synchronous = FULL")
        ensure_journal_schema(connection)
        if _has_terminal_failure(connection, preflight.plan_id):
            raise MaterializationError(
                "Consolidation Plan stopped in a needs-attention state after a recorded "
                f"materialization failure: {preflight.plan_id}; completing requires a new Analysis Run and plan"
            )
        entries = _ensure_execution_manifest(
            connection, preflight, _in_place_staging_name(preflight.plan_id)
        )
        _validate_manifest_algorithms(entries, preflight.plan_id)
        _validate_legacy_operation_algorithms(connection, preflight.plan_id)
        files = [row for row in entries if row["entry_kind"] == "file"]
        directories = [row for row in entries if row["entry_kind"] == "directory"]
        excluded_files = _excluded_file_entries(connection, preflight.plan_id)
        skipped_exclusions = _skipped_exclusions(connection, preflight.plan_id)
        snapshot_files = {
            str(row["relative_path"]): (
                int(row["observed_byte_size"]),
                int(row["modified_ns"]),
                str(row["digest"]),
            )
            for row in connection.execute(
                "SELECT inventory.relative_path, inventory.observed_byte_size, "
                "inventory.modified_ns, identity.digest FROM inventory_entries AS inventory "
                "LEFT JOIN content_identities AS identity ON identity.run_id = inventory.run_id "
                "AND identity.relative_path = inventory.relative_path "
                "WHERE inventory.run_id = ? AND inventory.entry_kind = 'regular-file' "
                "AND inventory.read_outcome = 'successful' "
                "AND (identity.read_outcome IS NULL OR identity.read_outcome = 'successful')",
                (preflight.run_id,),
            )
        }
        if _has_plan_event(
            connection, preflight.plan_id, EVENT_MATERIALIZATION_COMPLETE
        ):
            for row in files:
                final = selected_root / str(row["output_relative_path"])
                if not _is_regular_no_follow(final) or final.stat().st_size != int(
                    row["expected_byte_size"]
                ):
                    _raise_operation_failure(
                        connection,
                        preflight.plan_id,
                        row,
                        "final manifest check: planned output missing or size mismatch",
                        conflict=True,
                    )
            if staging_root.exists():
                shutil.rmtree(staging_root)
                _fsync_directory(selected_root)
            _complete_materialization_attempt(
                connection, preflight.plan_id, 0, 0, len(files), preflight.total_bytes
            )
            return {
                "analysis_run": str(run_path),
                "destination": str(selected_root),
                "operation_count": len(files),
                "explicit_directory_count": len(directories),
                "plan_id": preflight.plan_id,
                "run_id": preflight.run_id,
                "status": "materialized-in-place",
                "total_bytes": preflight.total_bytes,
            }
        sources = [str(row["source_relative_path"]) for row in files]
        if len(sources) != len(set(sources)):
            raise MaterializationError(
                "In-place execution requires one unique retained source for every planned output"
            )
        if not _has_plan_event(connection, preflight.plan_id, EVENT_PLAN_ADMITTED):
            if staging_root.exists():
                raise MaterializationError(
                    f"In-place staging directory already exists: {staging_root}"
                )
            journal_append(
                connection,
                preflight.plan_id,
                EVENT_PLAN_ADMITTED,
                detail="in-place execution manifest admitted",
            )
            _maybe_crash("admission")
        if not _has_plan_event(
            connection, preflight.plan_id, EVENT_MATERIALIZATION_COMPLETE
        ):
            _revalidate_structural_snapshot(
                connection, preflight.run_id, selected_root, preflight.plan_id
            )
        protection_complete = _has_plan_event(
            connection, preflight.plan_id, EVENT_IN_PLACE_PROTECTION_COMPLETE
        )
        progress.complete()
        progress.start("Protecting originals", len(files), unit="files")
        if not protection_complete:
            staging_root.mkdir(mode=0o700, exist_ok=True)
            protected_root.mkdir(mode=0o700, exist_ok=True)
            dirty_directories: set[Path] = {selected_root, staging_root, protected_root}
            for row in files:
                protected = protected_root / str(int(row["operation_index"]))
                expected_modified_ns = snapshot_files[str(row["source_relative_path"])][
                    1
                ]
                if _path_entry_exists(protected):
                    _validate_source_before_link(protected, row, expected_modified_ns)
                    source = selected_root / str(row["source_relative_path"])
                    if not _path_entry_exists(source):
                        raise MaterializationError(
                            f"needs-attention: retained source is missing before protection completes: {source}"
                        )
                    source_stat = source.lstat()
                    protected_stat = protected.lstat()
                    if (source_stat.st_dev, source_stat.st_ino) != (
                        protected_stat.st_dev,
                        protected_stat.st_ino,
                    ):
                        raise MaterializationError(
                            f"needs-attention: staging entry is not the retained source hard link: {protected}"
                        )
                    progress.advance()
                    continue
                source = selected_root / str(row["source_relative_path"])
                _validate_source_before_link(source, row, expected_modified_ns)
                os.link(source, protected)
                dirty_directories.add(protected.parent)
                progress.advance()
            # Excluded bytes are protected before any destructive publication.
            # They deliberately are not execution-manifest placements.
            for row in excluded_files:
                source = selected_root / str(row["source_relative_path"])
                protected = (
                    protected_root / "excluded" / str(row["source_relative_path"])
                )
                if _path_entry_exists(protected):
                    continue
                protected.parent.mkdir(parents=True, exist_ok=True)
                _validate_source_before_copy(
                    source, int(row["expected_byte_size"]), row["digest"]
                )
                os.link(source, protected)
                dirty_directories.add(protected.parent)
            for relative_path in skipped_exclusions:
                source = selected_root / relative_path
                protected = protected_root / "unverified" / relative_path
                if _path_entry_exists(protected):
                    continue
                protected.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(source, protected, follow_symlinks=False)
                except OSError as error:
                    raise MaterializationError(
                        f"Unverified Exclusion cannot stage safely: {source}"
                    ) from error
                dirty_directories.add(protected.parent)
            _sync_directories(dirty_directories)
            journal_append(
                connection,
                preflight.plan_id,
                EVENT_IN_PLACE_PROTECTION_COMPLETE,
                detail="every retained planned file has a durable hard-link protection",
            )
            _maybe_crash("postprotect")
        else:
            for row in files:
                protected = protected_root / str(int(row["operation_index"]))
                if not _path_entry_exists(protected):
                    raise MaterializationError(
                        f"needs-attention: protected staging link is missing: {protected}"
                    )
                _validate_source_before_link(
                    protected,
                    row,
                    snapshot_files[str(row["source_relative_path"])][1],
                )
                progress.advance()
        progress.complete()
        retained_sources = set(sources)
        for row in directories:
            directory = selected_root / str(row["output_relative_path"])
            if _path_entry_exists(directory) and not directory.is_dir():
                _raise_operation_failure(
                    connection,
                    preflight.plan_id,
                    row,
                    "final path is not a directory",
                    conflict=True,
                )
            directory.mkdir(parents=True, exist_ok=True)
        outcomes: list[tuple[str, int | None, str | None]] = []
        completed = _completed_indexes(connection, preflight.plan_id)
        progress.start("Publishing files", len(files), unit="files")
        for row in files:
            index = int(row["operation_index"])
            final = selected_root / str(row["output_relative_path"])
            protected = protected_root / str(index)
            if _path_entry_exists(final):
                if final == selected_root / str(row["source_relative_path"]):
                    _validate_source_before_link(
                        final,
                        row,
                        snapshot_files[str(row["source_relative_path"])][1],
                    )
                    if index not in completed:
                        outcomes.append(
                            (
                                EVENT_FILE_COMPLETED,
                                index,
                                "retained planned source already occupies final path",
                            )
                        )
                        completed.add(index)
                    progress.advance()
                    continue
                output_relative_path = str(row["output_relative_path"])
                snapshot = snapshot_files.get(output_relative_path)
                expected_identity = (
                    int(row["expected_byte_size"]),
                    str(row["digest"]),
                )
                if (
                    _is_regular_no_follow(final)
                    and digest_file(final) == expected_identity
                ):
                    source = selected_root / str(row["source_relative_path"])
                    if source != final and _path_entry_exists(source):
                        _validate_source_before_link(
                            source,
                            row,
                            snapshot_files[str(row["source_relative_path"])][1],
                        )
                        source.unlink()
                    if index not in completed:
                        outcomes.append(
                            (
                                EVENT_FILE_COMPLETED,
                                index,
                                "published final reconciled from durable in-place protection",
                            )
                        )
                        completed.add(index)
                    progress.advance()
                    continue
                if (
                    snapshot is not None
                    and _is_regular_no_follow(final)
                    and final.stat().st_size == snapshot[0]
                    and final.stat().st_mtime_ns == snapshot[1]
                    and digest_file(final) == (snapshot[0], snapshot[2])
                    and output_relative_path in retained_sources
                ):
                    final.unlink()
                else:
                    _raise_operation_failure(
                        connection,
                        preflight.plan_id,
                        row,
                        "output path already exists",
                        conflict=True,
                    )
            final.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(protected, final)
            except FileExistsError as error:
                raise _DestinationConflict("output path already exists") from error
            source = selected_root / str(row["source_relative_path"])
            if source != final and _path_entry_exists(source):
                _validate_source_before_link(
                    source,
                    row,
                    snapshot_files[str(row["source_relative_path"])][1],
                )
                source.unlink()
            if index not in completed:
                outcomes.append(
                    (
                        EVENT_FILE_COMPLETED,
                        index,
                        "published from durable in-place protection",
                    )
                )
                completed.add(index)
            _maybe_crash("postpublish")
            progress.advance()
        journal_append_many(connection, preflight.plan_id, outcomes)
        progress.complete()
        # Only exact, successfully verified noncanonical members of retained content
        # are eligible for permanent removal.
        retained_identities = {
            (int(row["expected_byte_size"]), str(row["digest"])) for row in files
        }
        planned_outputs = {str(row["output_relative_path"]) for row in files}
        duplicate_rows = connection.execute(
            "SELECT identity.relative_path, identity.byte_size, identity.digest "
            "FROM content_identities AS identity JOIN inventory_entries AS inventory "
            "ON inventory.run_id = identity.run_id AND inventory.relative_path = identity.relative_path "
            "WHERE identity.run_id = ? AND inventory.entry_kind = 'regular-file' "
            "AND identity.read_outcome = 'successful'",
            (preflight.run_id,),
        ).fetchall()
        for duplicate in duplicate_rows:
            relative_path = str(duplicate["relative_path"])
            if (
                relative_path in retained_sources
                or relative_path in planned_outputs
                or (int(duplicate["byte_size"]), str(duplicate["digest"]))
                not in retained_identities
            ):
                continue
            candidate = selected_root / relative_path
            if _path_entry_exists(candidate):
                snapshot = snapshot_files[relative_path]
                observed = candidate.lstat()
                if (
                    not stat.S_ISREG(observed.st_mode)
                    or observed.st_size != snapshot[0]
                    or observed.st_mtime_ns != snapshot[1]
                ):
                    raise MaterializationError(
                        f"needs-attention: duplicate candidate drifted: {candidate}"
                    )
                size, digest = digest_file(candidate)
                if (size, digest) != (
                    int(duplicate["byte_size"]),
                    str(duplicate["digest"]),
                ):
                    raise MaterializationError(
                        f"needs-attention: duplicate candidate drifted: {candidate}"
                    )
                candidate.unlink()
        for relative_path in skipped_exclusions:
            candidate = selected_root / relative_path
            if _path_entry_exists(candidate):
                candidate.unlink()
        # An explicit exclusion is a reviewed instruction for this observed
        # source path even when it was not a content-identity candidate.
        for row in excluded_files:
            candidate = selected_root / str(row["source_relative_path"])
            if _path_entry_exists(candidate):
                candidate.unlink()
        # An explicit User Exclusion owns the whole proven identity, not merely
        # the selected canonical occurrence.  The hard links above remain until
        # the completion event and staging cleanup, preserving forward recovery.
        excluded_identities = {
            (int(row["expected_byte_size"]), str(row["digest"]))
            for row in excluded_files
        }
        for duplicate in duplicate_rows:
            identity = (int(duplicate["byte_size"]), str(duplicate["digest"]))
            if identity not in excluded_identities:
                continue
            candidate = selected_root / str(duplicate["relative_path"])
            if _path_entry_exists(candidate):
                snapshot = snapshot_files[str(duplicate["relative_path"])]
                if (
                    not _is_regular_no_follow(candidate)
                    or candidate.stat().st_size != snapshot[0]
                    or candidate.stat().st_mtime_ns != snapshot[1]
                    or digest_file(candidate) != identity
                ):
                    raise MaterializationError(
                        f"needs-attention: excluded candidate drifted: {candidate}"
                    )
                candidate.unlink()
        _remove_empty_directories(selected_root, staging_root)
        for row in files:
            final = selected_root / str(row["output_relative_path"])
            if not _is_regular_no_follow(final) or final.stat().st_size != int(
                row["expected_byte_size"]
            ):
                _raise_operation_failure(
                    connection,
                    preflight.plan_id,
                    row,
                    "final manifest check: planned output missing or size mismatch",
                    conflict=True,
                )
        if not _has_plan_event(
            connection, preflight.plan_id, EVENT_MATERIALIZATION_COMPLETE
        ):
            journal_append(
                connection,
                preflight.plan_id,
                EVENT_MATERIALIZATION_COMPLETE,
                detail="in-place execution manifest entries present with expected types and sizes",
            )
        _maybe_crash("postcomplete")
        shutil.rmtree(staging_root)
        _fsync_directory(selected_root)
        _complete_materialization_attempt(
            connection, preflight.plan_id, 0, 0, len(files), preflight.total_bytes
        )
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    return {
        "analysis_run": str(run_path),
        "destination": str(selected_root),
        "operation_count": len(files),
        "explicit_directory_count": len(directories),
        "plan_id": preflight.plan_id,
        "run_id": preflight.run_id,
        "status": "materialized-in-place",
        "total_bytes": preflight.total_bytes,
    }


@_run_workspace_refusal
def materialize_consolidation_plan(
    analysis_run: Path,
    plan_id: str,
    destination: Path | None = None,
    *,
    in_place: bool = False,
    progress: ProgressReporter | None = None,
) -> dict[str, object]:
    """Run execution under non-blocking Run and destination locks.

    A lightweight initial lookup selects the deterministic lock sidecar. The
    implementation repeats its complete metadata pre-check after both locks
    are held, so this lookup grants no authorization.
    """
    try:
        run_path, _database_path = resolve_run_directory(analysis_run)
        initial = preflight_materialization_for_execution(
            analysis_run, plan_id, destination, in_place=in_place
        )
        with nonblocking_lock(run_path / ".filesystem-organizer.lock"):
            if in_place:
                return _materialize_consolidation_plan(
                    analysis_run, plan_id, destination, in_place=True, progress=progress
                )
            with nonblocking_lock(destination_lock_path(Path(initial.destination))):
                return _materialize_consolidation_plan(
                    analysis_run, plan_id, destination, in_place=False, progress=progress
                )
    except LinuxFilesystemError as error:
        raise MaterializationError(str(error)) from error


def _materialize_consolidation_plan(
    analysis_run: Path,
    plan_id: str,
    destination: Path | None = None,
    *,
    in_place: bool = False,
    progress: ProgressReporter | None = None,
) -> dict[str, object]:
    reporter = progress or NullProgressReporter()
    if in_place:
        return _materialize_in_place(analysis_run, plan_id, reporter)
    return _materialize_normal_phase(analysis_run, plan_id, destination, reporter)


def _record_streamed_execution_evidence(
    connection: sqlite3.Connection,
    preflight: MaterializationPreflight,
    row: sqlite3.Row,
    *,
    transfer_kind: str,
    digest: str | None,
) -> None:
    attempt_id = connection.execute(
        "SELECT attempt_id FROM execution_manifests WHERE plan_id = ? "
        "ORDER BY created_at DESC, attempt_id DESC LIMIT 1",
        (preflight.plan_id,),
    ).fetchone()[0]
    observed_mtime_ns = connection.execute(
        "SELECT modified_ns FROM inventory_entries WHERE run_id = ? AND relative_path = ?",
        (preflight.run_id, str(row["source_relative_path"])),
    ).fetchone()[0]
    connection.execute(
        "INSERT OR REPLACE INTO execution_file_evidence VALUES (?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?)",
        (
            attempt_id,
            preflight.plan_id,
            preflight.run_id,
            int(row["operation_index"]),
            str(row["source_relative_path"]),
            int(row["expected_byte_size"]),
            int(observed_mtime_ns),
            transfer_kind,
            CONTENT_IDENTITY_ALGORITHM if digest is not None else None,
            CONTENT_IDENTITY_ALGORITHM_VERSION if digest is not None else None,
            digest,
        ),
    )


def _materialize_normal_phase(
    analysis_run: Path,
    plan_id: str,
    destination: Path | None,
    reporter: ProgressReporter,
) -> dict[str, object]:
    """Build one complete owned partial tree, then publish it atomically."""
    preflight = preflight_materialization_for_execution(
        analysis_run, plan_id, destination, in_place=False
    )
    if not preflight.sufficient_free_space:
        raise MaterializationError(
            "Insufficient free space at the Materialized Consolidation destination: "
            f"{preflight.free_bytes} bytes free, {preflight.total_bytes} bytes required"
        )
    run_path, database_path = resolve_run_directory(Path(preflight.analysis_run))
    final_root = Path(preflight.destination)
    selected_root = Path(preflight.selected_backup_root)
    partial_root = _partial_destination(final_root, preflight.plan_id)
    connection = open_read_write(database_path)
    try:
        connection.execute("PRAGMA synchronous = FULL")
        ensure_journal_schema(connection)
        if final_root.exists():
            _reconcile_published_destination(connection, preflight, final_root)
            return {
                "analysis_run": str(run_path),
                "destination": str(final_root),
                "operation_count": preflight.operation_count,
                "explicit_directory_count": preflight.explicit_directory_count,
                "plan_id": preflight.plan_id,
                "run_id": preflight.run_id,
                "status": "materialized",
                "total_bytes": preflight.total_bytes,
            }
        entries = _ensure_execution_manifest(
            connection, preflight, partial_root.name
        )
        if partial_root.exists():
            _discard_proven_partial(connection, preflight, partial_root)
        _validate_manifest_algorithms(entries, preflight.plan_id)
        _validate_legacy_operation_algorithms(connection, preflight.plan_id)
        _maybe_crash("admission")
        _revalidate_structural_snapshot(
            connection, preflight.run_id, selected_root, preflight.plan_id
        )
        files = [row for row in entries if row["entry_kind"] == "file"]
        directories = [row for row in entries if row["entry_kind"] == "directory"]
        cloned_file_count = 0
        cloned_byte_count = 0
        streamed_file_count = 0
        streamed_byte_count = 0
        partial_root.mkdir(mode=0o700)
        owner_file = partial_root / PARTIAL_OWNER_FILE
        owner_file.write_text(
            _partial_owner_payload(connection, preflight.plan_id), encoding="utf-8"
        )
        with owner_file.open("rb") as owner_descriptor:
            os.fsync(owner_descriptor.fileno())
        _fsync_directory(partial_root)
        _set_attempt_state(connection, preflight.plan_id, "STAGING_STARTED")
        for row in directories:
            (partial_root / str(row["output_relative_path"])).mkdir(parents=True, exist_ok=True)
        reporter.start("Staging Materialized Consolidation", len(files), unit="files")
        for row in files:
            output = partial_root / str(row["output_relative_path"])
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = partial_root / f".partial-{int(row['operation_index'])}"
            source = selected_root / str(row["source_relative_path"])
            _validate_source_metadata_observation(
                source,
                int(row["expected_byte_size"]),
                row["expected_modified_ns"],
            )
            clone_result = (
                try_native_clone(source, output)
                if _source_same_filesystem(selected_root, partial_root)
                else CloneResult.UNAVAILABLE
            )
            if clone_result is CloneResult.CLONED:
                _validate_source_metadata_observation(
                    source,
                    int(row["expected_byte_size"]),
                    row["expected_modified_ns"],
                )
                source_metadata = source.stat()
                try:
                    os.chmod(output, stat.S_IMODE(source_metadata.st_mode))
                    os.utime(
                        output,
                        ns=(source_metadata.st_atime_ns, source_metadata.st_mtime_ns),
                    )
                except OSError:
                    pass
                size = int(row["expected_byte_size"])
                transfer_kind = "native-clone"
                digest: str | None = None
                cloned_file_count += 1
                cloned_byte_count += size
            else:
                size, digest = _copy_to_temporary(source, temporary, row)
                os.replace(temporary, output)
                transfer_kind = "streamed"
                streamed_file_count += 1
                streamed_byte_count += size
            _maybe_crash("postwritten")
            _record_streamed_execution_evidence(
                connection,
                preflight,
                row,
                transfer_kind=transfer_kind,
                digest=digest,
            )
            reporter.advance()
            if size != int(row["expected_byte_size"]):
                raise MaterializationError("streamed source byte size changed during staging")
        reporter.complete()
        owner_file.unlink()
        sync_filesystem(partial_root)
        root_stat = partial_root.stat()
        connection.execute(
            "UPDATE execution_manifests SET staging_root_dev = ?, staging_root_ino = ? "
            "WHERE plan_id = ?",
            (root_stat.st_dev, root_stat.st_ino, preflight.plan_id),
        )
        connection.commit()
        _set_attempt_state(connection, preflight.plan_id, "STAGING_DURABLE")
        publish_directory_no_replace(partial_root, final_root)
        sync_filesystem(final_root.parent)
        _set_attempt_state(connection, preflight.plan_id, "PUBLISHED")
        _maybe_crash("published")
        _complete_materialization_attempt(
            connection,
            preflight.plan_id,
            cloned_file_count,
            cloned_byte_count,
            streamed_file_count,
            streamed_byte_count,
        )
    except LinuxFilesystemError as error:
        raise MaterializationError(str(error)) from error
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    return {
        "analysis_run": str(run_path),
        "destination": str(final_root),
        "operation_count": len(files),
        "explicit_directory_count": len(directories),
        "plan_id": preflight.plan_id,
        "run_id": preflight.run_id,
        "status": "materialized",
        "total_bytes": preflight.total_bytes,
    }
