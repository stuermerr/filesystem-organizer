"""Incremental Custom Layout drafting over an immutable plan baseline."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from time import perf_counter
from typing import Any, NamedTuple, cast

from ..run_workspace import (
    open_read_only,
    open_wal_read_write,
    resolve_run_directory,
    select_plan_row,
)
from .models import PLAN_STATUS_DRAFT, ConsolidationPlanError, run_workspace_refusal

LAYOUT_SCHEMA_VERSION = 2
_STAGING_NAMESPACE = ".filesystem-organizer-staging"


@dataclass(frozen=True)
class ResolvedLayoutEntry:
    baseline: sqlite3.Row
    disposition: str
    output_relative_path: str | None


@dataclass(frozen=True)
class Compilation:
    validation: dict[str, Any]
    layout: dict[str, Any] | None
    changed_entries: tuple[ResolvedLayoutEntry, ...]
    directories: tuple[str, ...]
    skipped_actions: tuple[tuple[str, str], ...]
    directory_delta: tuple[tuple[str, str], ...]
    skipped_delta: tuple[tuple[str, str | None], ...]
    no_change: bool


class _ObservedCursor(sqlite3.Cursor):
    """Count rows returned to the compiler; VM steps cover internal scans."""

    def _count(self, amount: int) -> None:
        connection = cast(_ObservedConnection, self.connection)
        if hasattr(connection, "metrics"):
            connection.metrics["sqlite_rows_read"] += amount

    def fetchone(self) -> sqlite3.Row | None:
        row = super().fetchone()
        self._count(int(row is not None))
        return cast(sqlite3.Row | None, row)

    def fetchall(self) -> list[sqlite3.Row]:
        rows = super().fetchall()
        self._count(len(rows))
        return rows

    def __next__(self) -> sqlite3.Row:
        row = super().__next__()
        self._count(1)
        return cast(sqlite3.Row, row)


class _ObservedConnection(sqlite3.Connection):
    metrics: dict[str, int]

    def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
        return self.cursor(factory=_ObservedCursor).execute(sql, parameters)

    def executemany(self, sql: str, parameters: Iterable[Any]) -> sqlite3.Cursor:
        return self.cursor(factory=_ObservedCursor).executemany(sql, parameters)


def _schema(connection: sqlite3.Connection) -> None:
    legacy_columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(plan_layout_revisions)")
    }
    if legacy_columns and "authoring_fingerprint" not in legacy_columns:
        for table in ("plan_layout_revisions", "plan_layout_state"):
            connection.execute(f"ALTER TABLE {table} RENAME TO legacy_{table}")
    statements = """
    CREATE TABLE IF NOT EXISTS plan_baseline_entries (
      plan_id TEXT NOT NULL, entry_id TEXT NOT NULL, entry_kind TEXT NOT NULL,
      baseline_path TEXT NOT NULL, source_relative_path TEXT, expected_byte_size INTEGER,
      algorithm TEXT, algorithm_version INTEGER, digest TEXT, reason TEXT NOT NULL,
      modified_ns INTEGER, evidence_kind TEXT NOT NULL,
      PRIMARY KEY (plan_id, entry_id), UNIQUE (plan_id, baseline_path));
    CREATE INDEX IF NOT EXISTS plan_baseline_path_lookup ON plan_baseline_entries(plan_id, baseline_path);
    CREATE INDEX IF NOT EXISTS plan_baseline_identity_lookup ON plan_baseline_entries(plan_id, algorithm, algorithm_version, expected_byte_size, digest);
    CREATE TABLE IF NOT EXISTS plan_baseline_metadata (
      plan_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, entry_count INTEGER NOT NULL,
      file_count INTEGER NOT NULL, total_bytes INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS plan_source_selections (
      plan_id TEXT NOT NULL, entry_id TEXT NOT NULL, source_relative_path TEXT NOT NULL,
      reason TEXT NOT NULL, PRIMARY KEY(plan_id, entry_id));
    CREATE INDEX IF NOT EXISTS plan_source_selection_path_lookup ON plan_source_selections(plan_id, source_relative_path);
    CREATE TABLE IF NOT EXISTS plan_layout_revisions (
      plan_id TEXT NOT NULL, revision INTEGER NOT NULL, authoring_json TEXT NOT NULL,
      authoring_fingerprint TEXT NOT NULL, baseline_fingerprint TEXT NOT NULL,
      validation_json TEXT NOT NULL, total_entry_count INTEGER NOT NULL,
      affected_entry_count INTEGER NOT NULL, changed_entry_count INTEGER NOT NULL,
      placed_entry_count INTEGER NOT NULL, excluded_entry_count INTEGER NOT NULL,
      placed_content_entry_count INTEGER NOT NULL, total_placed_bytes INTEGER NOT NULL,
      content_empty INTEGER NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(plan_id, revision));
    CREATE TABLE IF NOT EXISTS plan_layout_state (
      plan_id TEXT PRIMARY KEY, active_revision INTEGER NOT NULL, content_empty INTEGER NOT NULL,
      total_entry_count INTEGER NOT NULL, placed_entry_count INTEGER NOT NULL,
      excluded_entry_count INTEGER NOT NULL, placed_content_entry_count INTEGER NOT NULL,
      total_placed_bytes INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS plan_layout_entry_deltas (
      plan_id TEXT NOT NULL, revision INTEGER NOT NULL, entry_id TEXT NOT NULL,
      disposition TEXT NOT NULL, output_relative_path TEXT, PRIMARY KEY(plan_id, revision, entry_id));
    CREATE TABLE IF NOT EXISTS plan_layout_directory_deltas (
      plan_id TEXT NOT NULL, revision INTEGER NOT NULL, output_relative_path TEXT NOT NULL,
      operation TEXT NOT NULL, PRIMARY KEY(plan_id, revision, output_relative_path));
    CREATE TABLE IF NOT EXISTS plan_layout_skipped_action_deltas (
      plan_id TEXT NOT NULL, revision INTEGER NOT NULL, relative_path TEXT NOT NULL,
      action TEXT, PRIMARY KEY(plan_id, revision, relative_path));
    CREATE TABLE IF NOT EXISTS plan_layout_active_entries (
      plan_id TEXT NOT NULL, entry_id TEXT NOT NULL, entry_kind TEXT NOT NULL,
      disposition TEXT NOT NULL, output_relative_path TEXT, PRIMARY KEY(plan_id, entry_id));
    CREATE UNIQUE INDEX IF NOT EXISTS plan_layout_active_output_occupancy
      ON plan_layout_active_entries(plan_id, output_relative_path) WHERE disposition='place';
    CREATE INDEX IF NOT EXISTS plan_layout_active_output_subtree ON plan_layout_active_entries(plan_id, output_relative_path, entry_kind);
    CREATE INDEX IF NOT EXISTS plan_layout_active_depth_lookup ON plan_layout_active_entries
      (plan_id, (length(output_relative_path)-length(replace(output_relative_path, '/', ''))), output_relative_path)
      WHERE disposition='place';
    CREATE INDEX IF NOT EXISTS plan_layout_active_disposition ON plan_layout_active_entries(plan_id, disposition, entry_id);
    CREATE TABLE IF NOT EXISTS plan_layout_active_directories (
      plan_id TEXT NOT NULL, output_relative_path TEXT NOT NULL, PRIMARY KEY(plan_id, output_relative_path));
    CREATE TABLE IF NOT EXISTS plan_layout_active_skipped_actions (
      plan_id TEXT NOT NULL, relative_path TEXT NOT NULL, action TEXT NOT NULL, PRIMARY KEY(plan_id, relative_path));
    CREATE TABLE IF NOT EXISTS plan_layout_rules (
      plan_id TEXT NOT NULL, revision INTEGER NOT NULL, rule_kind TEXT NOT NULL,
      identity TEXT NOT NULL, canonical_json TEXT NOT NULL, PRIMARY KEY(plan_id, revision, rule_kind, identity));
    CREATE TABLE IF NOT EXISTS plan_layout_findings (
      plan_id TEXT NOT NULL, revision INTEGER NOT NULL, finding_id TEXT NOT NULL,
      severity TEXT NOT NULL, code TEXT NOT NULL, finding_json TEXT NOT NULL,
      PRIMARY KEY(plan_id, revision, finding_id));
    CREATE TABLE IF NOT EXISTS plan_layout_acknowledgements (
      plan_id TEXT NOT NULL, revision INTEGER NOT NULL, category TEXT NOT NULL,
      value TEXT NOT NULL, PRIMARY KEY(plan_id, revision, category, value));
    CREATE TABLE IF NOT EXISTS final_plan_projection (
      plan_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, entry_count INTEGER NOT NULL,
      placed_entry_count INTEGER NOT NULL, excluded_entry_count INTEGER NOT NULL,
      directory_count INTEGER NOT NULL, skipped_action_count INTEGER NOT NULL,
      total_placed_bytes INTEGER NOT NULL, content_empty INTEGER NOT NULL,
      fingerprint TEXT NOT NULL, finalized_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS final_plan_entries (
      plan_id TEXT NOT NULL, entry_index INTEGER NOT NULL, entry_id TEXT NOT NULL,
      entry_kind TEXT NOT NULL, source_relative_path TEXT, output_relative_path TEXT NOT NULL,
      expected_byte_size INTEGER, algorithm TEXT, algorithm_version INTEGER, digest TEXT,
      reason TEXT NOT NULL,
      evidence_kind TEXT NOT NULL CHECK (evidence_kind IN (
        'content-identity', 'metadata-observation', 'layout-owned-directory')),
      evidence_entry_type TEXT,
      evidence_mtime_ns INTEGER,
      evidence_directory_path TEXT,
      evidence_layout_revision INTEGER,
      PRIMARY KEY(plan_id, entry_index), UNIQUE(plan_id, entry_id),
      CHECK (
        (evidence_kind = 'content-identity' AND entry_kind = 'file'
          AND source_relative_path IS NOT NULL AND expected_byte_size IS NOT NULL
          AND algorithm IS NOT NULL AND algorithm_version IS NOT NULL
          AND digest IS NOT NULL AND evidence_entry_type IS NULL
          AND evidence_mtime_ns IS NULL AND evidence_directory_path IS NULL
          AND evidence_layout_revision IS NULL)
        OR (evidence_kind = 'metadata-observation'
          AND source_relative_path IS NOT NULL AND algorithm IS NULL
          AND algorithm_version IS NULL AND digest IS NULL
          AND evidence_entry_type IS NOT NULL AND evidence_mtime_ns IS NOT NULL
          AND evidence_directory_path IS NULL AND evidence_layout_revision IS NULL
          AND ((entry_kind = 'file' AND evidence_entry_type = 'regular-file'
            AND expected_byte_size IS NOT NULL)
            OR (entry_kind = 'directory' AND evidence_entry_type = 'directory'
              AND expected_byte_size IS NULL)))
        OR (evidence_kind = 'layout-owned-directory' AND entry_kind = 'directory'
          AND source_relative_path IS NULL AND expected_byte_size IS NULL
          AND algorithm IS NULL AND algorithm_version IS NULL AND digest IS NULL
          AND evidence_entry_type IS NULL AND evidence_mtime_ns IS NULL
          AND evidence_directory_path = output_relative_path
          AND evidence_layout_revision IS NOT NULL)
      ));
    CREATE INDEX IF NOT EXISTS final_plan_entry_output_lookup ON final_plan_entries(plan_id, output_relative_path);
    CREATE INDEX IF NOT EXISTS final_plan_entry_depth_lookup ON final_plan_entries
      (plan_id, (length(output_relative_path)-length(replace(output_relative_path, '/', ''))), output_relative_path);
    CREATE TABLE IF NOT EXISTS final_plan_exclusions (
      plan_id TEXT NOT NULL, entry_id TEXT NOT NULL, source_relative_path TEXT NOT NULL,
      expected_byte_size INTEGER, digest TEXT, PRIMARY KEY(plan_id, entry_id, source_relative_path));
    CREATE TABLE IF NOT EXISTS final_plan_skipped_actions (
      plan_id TEXT NOT NULL, relative_path TEXT NOT NULL, action TEXT NOT NULL, PRIMARY KEY(plan_id, relative_path));
    """
    for statement in statements.split(";"):
        if statement.strip():
            connection.execute(statement)
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='content_identities'"
    ).fetchone():
        connection.execute(
            "CREATE INDEX IF NOT EXISTS plan_content_identity_lookup ON content_identities "
            "(run_id, algorithm, algorithm_version, byte_size, digest)"
        )


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _final_projection_fingerprint(
    connection: sqlite3.Connection, plan_id: str, revision: int
) -> str:
    """Hash every immutable output and selected source without materializing it in memory."""
    digest = hashlib.sha256()
    digest.update(_canonical_json(["final-plan-v3", plan_id, revision]).encode())
    for table, columns, ordering in (
        (
            "final_plan_entries",
            "entry_index, entry_id, entry_kind, source_relative_path, output_relative_path, "
            + "expected_byte_size, algorithm, algorithm_version, digest, reason, "
            + "evidence_kind, evidence_entry_type, evidence_mtime_ns, "
            + "evidence_directory_path, evidence_layout_revision",
            "entry_index",
        ),
        (
            "final_plan_exclusions",
            "entry_id, source_relative_path, expected_byte_size, digest",
            "entry_id, source_relative_path",
        ),
        (
            "final_plan_skipped_actions",
            "relative_path, action",
            "relative_path",
        ),
        (
            "plan_source_selections",
            "entry_id, source_relative_path, reason",
            "entry_id",
        ),
    ):
        digest.update(_canonical_json(table).encode())
        for row in connection.execute(
            f"SELECT {columns} FROM {table} WHERE plan_id=? ORDER BY {ordering}",
            (plan_id,),
        ):
            digest.update(_canonical_json(list(row)).encode())
            digest.update(b"\n")
    return digest.hexdigest()


def _entry_id(row: sqlite3.Row) -> str:
    return str(row["entry_id"])


def _baseline_fingerprint(connection: sqlite3.Connection, plan_id: str) -> str:
    digest = hashlib.sha256()
    digest.update(b"baseline-v2\0")
    for row in connection.execute(
        "SELECT * FROM plan_baseline_entries WHERE plan_id=? ORDER BY baseline_path, entry_kind",
        (plan_id,),
    ):
        digest.update(
            _canonical_json(
                [
                    row[k]
                    for k in (
                        "entry_id",
                        "entry_kind",
                        "baseline_path",
                        "expected_byte_size",
                        "algorithm",
                        "algorithm_version",
                        "digest",
                        "reason",
                        "modified_ns",
                        "evidence_kind",
                    )
                ]
            ).encode()
        )
        digest.update(b"\0")
    digest.update(b"skipped\0")
    for row in connection.execute(
        "SELECT relative_path, reason FROM skipped_entry_findings "
        "WHERE run_id=(SELECT run_id FROM consolidation_plans WHERE plan_id=?) "
        "ORDER BY relative_path",
        (plan_id,),
    ):
        digest.update(_canonical_json([row["relative_path"], row["reason"]]).encode())
        digest.update(b"\0")
    return digest.hexdigest()


def _default_layout(plan_id: str, revision: int, fingerprint: str) -> dict[str, Any]:
    return {
        "layout_schema_version": 2,
        "plan_id": plan_id,
        "base_revision": revision,
        "baseline_fingerprint": fingerprint,
        "unmatched": "preserve",
        "rules": [],
        "entry_exceptions": [],
        "directories": [],
        "skipped_actions": [],
    }


def _semantic_layout(layout: dict[str, Any]) -> dict[str, Any]:
    return {
        "unmatched": layout.get("unmatched"),
        "rules": sorted(layout.get("rules", []), key=_canonical_json),
        "entry_exceptions": sorted(
            layout.get("entry_exceptions", []), key=_canonical_json
        ),
        "directories": sorted(layout.get("directories", [])),
        "skipped_actions": sorted(
            layout.get("skipped_actions", []), key=_canonical_json
        ),
    }


def _canonical_authoring(
    layout: dict[str, Any], revision: int, fingerprint: str
) -> dict[str, Any]:
    return {
        "layout_schema_version": 2,
        "plan_id": layout["plan_id"],
        "base_revision": revision,
        "baseline_fingerprint": fingerprint,
        **_semantic_layout(layout),
    }


def initialize_layout_baseline(connection: sqlite3.Connection, plan_id: str) -> None:
    _schema(connection)
    pending: list[tuple[object, ...]] = []
    for row in connection.execute(
        "SELECT * FROM plan_output_entries WHERE plan_id=? ORDER BY entry_index",
        (plan_id,),
    ):
        path = str(row["output_relative_path"])
        anchor = (
            [
                row["entry_kind"],
                row["algorithm"],
                row["algorithm_version"],
                row["expected_byte_size"],
                row["digest"],
                row["modified_ns"],
                row["evidence_kind"],
                path,
            ]
            if row["entry_kind"] == "file"
            else [row["entry_kind"], path]
        )
        pending.append(
            (
                plan_id,
                _fingerprint(anchor)[:24],
                row["entry_kind"],
                path,
                row["source_relative_path"],
                row["expected_byte_size"],
                row["algorithm"],
                row["algorithm_version"],
                row["digest"],
                row["reason"],
                row["modified_ns"],
                row["evidence_kind"],
            ),
        )
        if len(pending) == 10_000:
            connection.executemany(
                "INSERT INTO plan_baseline_entries VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", pending
            )
            pending.clear()
    connection.executemany(
        "INSERT INTO plan_baseline_entries VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", pending
    )
    fingerprint = _baseline_fingerprint(connection, plan_id)
    entry_count, files, total_bytes = connection.execute(
        "SELECT COUNT(*), COALESCE(SUM(entry_kind='file'),0), "
        "COALESCE(SUM(CASE WHEN entry_kind='file' THEN expected_byte_size ELSE 0 END),0) "
        "FROM plan_baseline_entries WHERE plan_id=?",
        (plan_id,),
    ).fetchone()
    entry_count, files, total_bytes = int(entry_count), int(files), int(total_bytes)
    connection.execute(
        "INSERT INTO plan_baseline_metadata VALUES(?,?,?,?,?)",
        (plan_id, fingerprint, entry_count, files, total_bytes),
    )
    connection.execute(
        "INSERT INTO plan_source_selections "
        "SELECT ?, entry_id, source_relative_path, reason FROM plan_baseline_entries "
        "WHERE plan_id=? AND entry_kind='file'",
        (plan_id, plan_id),
    )
    layout = _default_layout(plan_id, 0, fingerprint)
    validation = {
        "plan_id": plan_id,
        "base_revision": 0,
        "baseline_fingerprint": fingerprint,
        "valid": True,
        "findings": [],
        "acknowledgements_required": {"exclusions": False, "content_empty": False},
        "total_entry_count": entry_count,
        "affected_entry_count": 0,
        "changed_entry_count": 0,
        "placed_entry_count": entry_count,
        "excluded_entry_count": 0,
        "placed_content_entry_count": files,
        "total_placed_bytes": total_bytes,
    }
    connection.execute(
        "INSERT INTO plan_layout_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            plan_id,
            0,
            _canonical_json(layout),
            _fingerprint(_semantic_layout(layout)),
            fingerprint,
            _canonical_json(validation),
            entry_count,
            0,
            0,
            entry_count,
            0,
            files,
            total_bytes,
            0,
            datetime.now(UTC).isoformat(),
        ),
    )
    connection.execute(
        "INSERT INTO plan_layout_state VALUES(?,?,?,?,?,?,?,?)",
        (plan_id, 0, 0, entry_count, entry_count, 0, files, total_bytes),
    )
    connection.execute(
        "INSERT INTO plan_layout_active_entries "
        "SELECT ?, entry_id, entry_kind, 'place', baseline_path FROM plan_baseline_entries "
        "WHERE plan_id=?",
        (plan_id, plan_id),
    )


def _finding(code: str, message: str, **details: object) -> dict[str, object]:
    return {
        "id": _fingerprint([code, details])[:16],
        "severity": "error",
        "code": code,
        "message": message,
        **details,
    }


def _path(value: object, field: str, findings: list[dict[str, object]]) -> str | None:
    if not isinstance(value, str) or not value:
        findings.append(
            _finding("invalid-path", f"{field} must be a non-empty relative path")
        )
        return None
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or "\\" in value
        or "\x00" in value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        findings.append(
            _finding(
                "unsafe-path",
                f"{field} is not a safe normalized relative path",
                path=value,
            )
        )
        return None
    if value == _STAGING_NAMESPACE or value.startswith(_STAGING_NAMESPACE + "/"):
        findings.append(
            _finding(
                "reserved-path",
                f"{field} uses the plan-owned staging namespace",
                path=value,
            )
        )
        return None
    return value


def _decode(
    layout_path: str, plan_id: str, revision: int, fingerprint: str
) -> tuple[dict[str, Any] | None, list[dict[str, object]]]:
    findings: list[dict[str, object]] = []
    try:
        raw = json.loads(Path(layout_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return None, [_finding("invalid-json", f"Cannot read layout JSON: {error}")]
    if not isinstance(raw, dict):
        return None, [
            _finding("invalid-layout", "Layout document must be a JSON object")
        ]
    required = {
        "layout_schema_version",
        "plan_id",
        "base_revision",
        "baseline_fingerprint",
        "unmatched",
        "rules",
        "entry_exceptions",
        "directories",
        "skipped_actions",
    }
    if set(raw) != required:
        findings.append(
            _finding(
                "invalid-fields", "Layout fields must exactly match schema version 2"
            )
        )
    if raw.get("layout_schema_version") != 2:
        findings.append(
            _finding("unsupported-layout-schema", "Unsupported layout schema version")
        )
    if raw.get("plan_id") != plan_id:
        findings.append(_finding("wrong-plan", "Layout belongs to a different plan"))
    if raw.get("base_revision") != revision:
        findings.append(_finding("stale-revision", "Layout base_revision is stale"))
    if raw.get("baseline_fingerprint") != fingerprint:
        findings.append(
            _finding(
                "baseline-fingerprint-mismatch",
                "Layout baseline_fingerprint does not match the immutable baseline",
            )
        )
    if raw.get("unmatched") != "preserve":
        findings.append(_finding("invalid-unmatched", "unmatched must be preserve"))
    for field in ("rules", "entry_exceptions", "directories", "skipped_actions"):
        if not isinstance(raw.get(field), list):
            findings.append(_finding("invalid-field-type", f"{field} must be an array"))
            raw[field] = []
    return raw, findings


def _subtree_rows(
    connection: sqlite3.Connection, plan_id: str, root: str
) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM plan_baseline_entries WHERE plan_id=? AND (baseline_path=? OR (baseline_path>=? AND baseline_path<?)) ORDER BY baseline_path",
        (plan_id, root, root + "/", root + "0"),
    ).fetchall()


def _subtree_exists(connection: sqlite3.Connection, plan_id: str, root: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM plan_baseline_entries WHERE plan_id=? AND "
            "(baseline_path=? OR (baseline_path>=? AND baseline_path<?)) LIMIT 1",
            (plan_id, root, root + "/", root + "0"),
        ).fetchone()
        is not None
    )


def _parse_layout(
    connection: sqlite3.Connection,
    plan_id: str,
    layout: dict[str, Any],
    findings: list[dict[str, object]],
) -> tuple[
    dict[str, tuple[str | None, bool]],
    dict[str, tuple[str | None, bool]],
    tuple[str, ...],
    tuple[tuple[str, str], ...],
]:
    rules: dict[str, tuple[str | None, bool]] = {}
    for index, raw in enumerate(layout["rules"]):
        if (
            not isinstance(raw, dict)
            or set(raw) != {"selector", "action"}
            or not isinstance(raw.get("selector"), dict)
            or set(raw["selector"]) != {"subtree"}
            or not isinstance(raw.get("action"), dict)
        ):
            findings.append(
                _finding(
                    "invalid-rule",
                    "Rule must contain a subtree selector and action",
                    rule_index=index,
                )
            )
            continue
        subtree = _path(raw["selector"].get("subtree"), "selector.subtree", findings)
        action = raw["action"]
        excluded = action == {"exclude": True}
        destination = (
            None
            if excluded
            else _path(action.get("place_under"), "action.place_under", findings)
        )
        if not excluded and set(action) != {"place_under"}:
            findings.append(
                _finding(
                    "invalid-rule-action",
                    "Rule action must place_under or exclude",
                    rule_index=index,
                )
            )
            continue
        if subtree is None or (not excluded and destination is None):
            continue
        if subtree in rules:
            code = (
                "contradictory-rules"
                if rules[subtree] != (destination, excluded)
                else "duplicate-rule"
            )
            findings.append(
                _finding(
                    code,
                    "A subtree has more than one rule",
                    rule_index=index,
                    path=subtree,
                )
            )
            continue
        if not _subtree_exists(connection, plan_id, subtree):
            findings.append(
                _finding(
                    "unknown-subtree",
                    "Rule selector matches no baseline entries",
                    rule_index=index,
                    path=subtree,
                )
            )
            continue
        rules[subtree] = (destination, excluded)
    exceptions: dict[str, tuple[str | None, bool]] = {}
    for index, raw in enumerate(layout["entry_exceptions"]):
        if (
            not isinstance(raw, dict)
            or set(raw) != {"entry_id", "action"}
            or not isinstance(raw.get("entry_id"), str)
            or not isinstance(raw.get("action"), dict)
        ):
            findings.append(
                _finding(
                    "invalid-entry-exception",
                    "Invalid entry exception",
                    exception_index=index,
                )
            )
            continue
        entry_id, action = raw["entry_id"], raw["action"]
        if (
            connection.execute(
                "SELECT 1 FROM plan_baseline_entries WHERE plan_id=? AND entry_id=?",
                (plan_id, entry_id),
            ).fetchone()
            is None
        ):
            findings.append(
                _finding(
                    "unknown-entry",
                    "Entry exception has unknown entry_id",
                    exception_index=index,
                    entry_id=entry_id,
                )
            )
            continue
        if entry_id in exceptions:
            findings.append(
                _finding(
                    "duplicate-entry-exception",
                    "Entry exception is duplicated",
                    exception_index=index,
                    entry_id=entry_id,
                )
            )
            continue
        excluded = action == {"exclude": True}
        target = (
            None
            if excluded
            else _path(action.get("place_at"), "action.place_at", findings)
        )
        if not excluded and (set(action) != {"place_at"} or target is None):
            findings.append(
                _finding(
                    "invalid-entry-action",
                    "Entry exception action must place_at or exclude",
                    exception_index=index,
                )
            )
            continue
        exceptions[entry_id] = (target, excluded)
    directories: list[str] = []
    for raw in layout["directories"]:
        path = _path(raw, "directories", findings)
        if path is None:
            continue
        if path in directories:
            findings.append(
                _finding(
                    "duplicate-directory",
                    "Layout-Owned Directory is duplicated",
                    path=path,
                )
            )
        else:
            directories.append(path)
    expected_skipped = {
        str(row[0])
        for row in connection.execute(
            "SELECT relative_path FROM skipped_entry_findings WHERE run_id=(SELECT run_id FROM consolidation_plans WHERE plan_id=?)",
            (plan_id,),
        )
    }
    skipped: dict[str, str] = {}
    for index, raw in enumerate(layout["skipped_actions"]):
        if (
            not isinstance(raw, dict)
            or set(raw) != {"path", "action"}
            or raw.get("action") not in {"retain", "exclude"}
            or raw.get("path") not in expected_skipped
            or raw.get("path") in skipped
        ):
            findings.append(
                _finding(
                    "invalid-skipped-action",
                    "Skipped action must name one skipped entry exactly once with retain or exclude",
                    action_index=index,
                    path=raw.get("path") if isinstance(raw, dict) else None,
                )
            )
            continue
        skipped[str(raw["path"])] = str(raw["action"])
    return rules, exceptions, tuple(sorted(directories)), tuple(sorted(skipped.items()))


def _rule_map(layout: dict[str, Any]) -> dict[str, str]:
    return {
        str(item["selector"]["subtree"]): _canonical_json(item["action"])
        for item in layout.get("rules", [])
        if isinstance(item, dict)
        and isinstance(item.get("selector"), dict)
        and "subtree" in item["selector"]
        and "action" in item
    }


def _exception_map(layout: dict[str, Any]) -> dict[str, str]:
    return {
        str(item["entry_id"]): _canonical_json(item["action"])
        for item in layout.get("entry_exceptions", [])
        if isinstance(item, dict) and "entry_id" in item and "action" in item
    }


def _affected_rows(
    connection: sqlite3.Connection,
    plan_id: str,
    current: dict[str, Any],
    proposed: dict[str, Any],
) -> list[sqlite3.Row]:
    old_rules, new_rules = _rule_map(current), _rule_map(proposed)
    roots = {
        root
        for root in old_rules.keys() | new_rules.keys()
        if old_rules.get(root) != new_rules.get(root)
    }
    old_exceptions, new_exceptions = _exception_map(current), _exception_map(proposed)
    ids = {
        entry_id
        for entry_id in old_exceptions.keys() | new_exceptions.keys()
        if old_exceptions.get(entry_id) != new_exceptions.get(entry_id)
    }
    rows: dict[str, sqlite3.Row] = {}
    for root in roots:
        for row in _subtree_rows(connection, plan_id, root):
            rows[_entry_id(row)] = row
    if ids:
        ordered_ids = sorted(ids)
        for start in range(0, len(ordered_ids), 800):
            batch = ordered_ids[start : start + 800]
            placeholders = ",".join("?" for _ in batch)
            for row in connection.execute(
                f"SELECT * FROM plan_baseline_entries WHERE plan_id=? AND entry_id IN ({placeholders})",
                (plan_id, *batch),
            ):
                rows[_entry_id(row)] = row
    return sorted(rows.values(), key=lambda row: str(row["baseline_path"]))


def _resolve(
    row: sqlite3.Row,
    rules: dict[str, tuple[str | None, bool]],
    exceptions: dict[str, tuple[str | None, bool]],
) -> ResolvedLayoutEntry:
    entry_id, source = _entry_id(row), str(row["baseline_path"])
    if entry_id in exceptions:
        target, excluded = exceptions[entry_id]
        return ResolvedLayoutEntry(row, "exclude" if excluded else "place", target)
    matching = [
        root for root in rules if source == root or source.startswith(root + "/")
    ]
    if not matching:
        return ResolvedLayoutEntry(row, "place", source)
    root = max(matching, key=lambda value: (value.count("/"), len(value)))
    target_root, excluded = rules[root]
    suffix = source.removeprefix(root).lstrip("/")
    target = (
        None if excluded else target_root if not suffix else f"{target_root}/{suffix}"
    )
    return ResolvedLayoutEntry(row, "exclude" if excluded else "place", target)


def _validate_occupancy(
    connection: sqlite3.Connection,
    plan_id: str,
    resolved: list[ResolvedLayoutEntry],
    directories: Iterable[str],
    findings: list[dict[str, object]],
) -> None:
    affected = {_entry_id(entry.baseline) for entry in resolved}
    candidates: dict[str, ResolvedLayoutEntry] = {}
    for entry in resolved:
        target = entry.output_relative_path
        if entry.disposition == "exclude" or target is None:
            continue
        if entry.baseline["entry_kind"] == "file" and "/" not in target:
            findings.append(
                _finding(
                    "file-at-root",
                    "Files cannot be placed at the output root",
                    entry_id=_entry_id(entry.baseline),
                    path=target,
                )
            )
        if target in candidates and _entry_id(candidates[target].baseline) != _entry_id(
            entry.baseline
        ):
            findings.append(
                _finding(
                    "output-collision",
                    "Multiple baseline entries resolve to one output path",
                    path=target,
                )
            )
        candidates[target] = entry
    for target, entry in candidates.items():
        occupant = connection.execute(
            "SELECT entry_id FROM plan_layout_active_entries WHERE plan_id=? AND disposition='place' AND output_relative_path=?",
            (plan_id, target),
        ).fetchone()
        if occupant is not None and str(occupant["entry_id"]) not in affected:
            findings.append(
                _finding(
                    "output-collision",
                    "A changed entry collides with unchanged active occupancy",
                    path=target,
                )
            )
        for parent in PurePosixPath(target).parents:
            parent_path = parent.as_posix()
            if parent_path == ".":
                break
            active_file = connection.execute(
                "SELECT entry_id FROM plan_layout_active_entries WHERE plan_id=? AND disposition='place' AND entry_kind='file' AND output_relative_path=?",
                (plan_id, parent_path),
            ).fetchone()
            candidate = candidates.get(parent_path)
            if (
                active_file is not None and str(active_file["entry_id"]) not in affected
            ) or (candidate is not None and candidate.baseline["entry_kind"] == "file"):
                findings.append(
                    _finding(
                        "file-directory-conflict",
                        "A file is an ancestor of an output entry",
                        path=target,
                    )
                )
                break
        if entry.baseline["entry_kind"] == "file":
            if any(
                directory == target or directory.startswith(target + "/")
                for directory in directories
            ):
                findings.append(
                    _finding(
                        "file-directory-conflict",
                        "A file conflicts with a proposed Layout-Owned Directory",
                        path=target,
                    )
                )
            descendants = connection.execute(
                "SELECT entry_id FROM plan_layout_active_entries WHERE plan_id=? AND disposition='place' AND output_relative_path>=? AND output_relative_path<?",
                (plan_id, target + "/", target + "0"),
            ).fetchall()
            if any(
                str(descendant["entry_id"]) not in affected
                for descendant in descendants
            ):
                findings.append(
                    _finding(
                        "file-directory-conflict",
                        "A file is an ancestor of an unchanged output entry",
                        path=target,
                    )
                )
    for directory in directories:
        occupant = connection.execute(
            "SELECT entry_id, entry_kind FROM plan_layout_active_entries WHERE plan_id=? AND disposition='place' AND output_relative_path=?",
            (plan_id, directory),
        ).fetchone()
        candidate = candidates.get(directory)
        if (
            occupant is not None
            and occupant["entry_kind"] == "file"
            and str(occupant["entry_id"]) not in affected
        ) or (candidate is not None and candidate.baseline["entry_kind"] == "file"):
            findings.append(
                _finding(
                    "file-directory-conflict",
                    "A Layout-Owned Directory collides with a file",
                    path=directory,
                )
            )
        for parent in PurePosixPath(directory).parents:
            parent_path = parent.as_posix()
            if parent_path == ".":
                break
            active_file = connection.execute(
                "SELECT entry_id FROM plan_layout_active_entries WHERE plan_id=? "
                "AND disposition='place' AND entry_kind='file' AND output_relative_path=?",
                (plan_id, parent_path),
            ).fetchone()
            if active_file is not None and str(active_file["entry_id"]) not in affected:
                findings.append(
                    _finding(
                        "file-directory-conflict",
                        "A file is an ancestor of a Layout-Owned Directory",
                        path=directory,
                    )
                )
                break


def _current_authoring(
    connection: sqlite3.Connection, plan_id: str, revision: int
) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(
            str(
                connection.execute(
                    "SELECT authoring_json FROM plan_layout_revisions WHERE plan_id=? AND revision=?",
                    (plan_id, revision),
                ).fetchone()[0]
            )
        ),
    )


def _active_maps(
    connection: sqlite3.Connection, plan_id: str
) -> tuple[set[str], dict[str, str]]:
    directories = {
        str(row[0])
        for row in connection.execute(
            "SELECT output_relative_path FROM plan_layout_active_directories WHERE plan_id=?",
            (plan_id,),
        )
    }
    skipped = {
        str(row[0]): str(row[1])
        for row in connection.execute(
            "SELECT relative_path, action FROM plan_layout_active_skipped_actions WHERE plan_id=?",
            (plan_id,),
        )
    }
    return directories, skipped


def _compile(
    connection: sqlite3.Connection, plan_id: str, layout_path: str
) -> Compilation:
    started = perf_counter()
    state = connection.execute(
        "SELECT * FROM plan_layout_state WHERE plan_id=?", (plan_id,)
    ).fetchone()
    metadata = connection.execute(
        "SELECT * FROM plan_baseline_metadata WHERE plan_id=?", (plan_id,)
    ).fetchone()
    active, fingerprint = int(state["active_revision"]), str(metadata["fingerprint"])
    layout, findings = _decode(layout_path, plan_id, active, fingerprint)
    decoded = perf_counter()
    current = _current_authoring(connection, plan_id, active)
    changed_entries: list[ResolvedLayoutEntry] = []
    directories: tuple[str, ...] = ()
    skipped_actions: tuple[tuple[str, str], ...] = ()
    no_change = False
    affected_count = changed_count = 0
    placed = int(state["placed_entry_count"])
    excluded = int(state["excluded_entry_count"])
    placed_content = int(state["placed_content_entry_count"])
    placed_bytes = int(state["total_placed_bytes"])
    directory_delta: tuple[tuple[str, str], ...] = ()
    skipped_delta: tuple[tuple[str, str | None], ...] = ()
    if layout is not None:
        rules, exceptions, directories, skipped_actions = _parse_layout(
            connection, plan_id, layout, findings
        )
        no_change = _semantic_layout(current) == _semantic_layout(layout)
        affected = (
            [] if no_change else _affected_rows(connection, plan_id, current, layout)
        )
        affected_count = len(affected)
        resolved = [_resolve(row, rules, exceptions) for row in affected]
        _validate_occupancy(connection, plan_id, resolved, directories, findings)
        affected_ids = {_entry_id(row) for row in affected}
        active_by_id: dict[str, sqlite3.Row] = {}
        ordered_ids = sorted(affected_ids)
        # SQLite's variable limit varies by build; cap each batch well below
        # the lowest supported limit even when a root-wide rule touches 1M IDs.
        for start in range(0, len(ordered_ids), 800):
            batch = ordered_ids[start : start + 800]
            placeholders = ",".join("?" for _ in batch)
            for row in connection.execute(
                "SELECT entry_id, disposition, output_relative_path FROM plan_layout_active_entries "
                f"WHERE plan_id=? AND entry_id IN ({placeholders})",
                (plan_id, *batch),
            ):
                active_by_id[str(row["entry_id"])] = row
        for entry in resolved:
            old = active_by_id[_entry_id(entry.baseline)]
            if (
                old["disposition"] == entry.disposition
                and old["output_relative_path"] == entry.output_relative_path
            ):
                continue
            changed_entries.append(entry)
            changed_count += 1
            old_placed, new_placed = (
                old["disposition"] == "place",
                entry.disposition == "place",
            )
            placed += int(new_placed) - int(old_placed)
            excluded += int(not new_placed) - int(not old_placed)
            if entry.baseline["entry_kind"] == "file":
                placed_content += int(new_placed) - int(old_placed)
                placed_bytes += int(entry.baseline["expected_byte_size"] or 0) * (
                    int(new_placed) - int(old_placed)
                )
        active_directories, active_skipped = _active_maps(connection, plan_id)
        proposed_directories = set(directories)
        directory_delta = tuple(
            [
                (path, "remove")
                for path in sorted(active_directories - proposed_directories)
            ]
            + [
                (path, "add")
                for path in sorted(proposed_directories - active_directories)
            ]
        )
        proposed_skipped = dict(skipped_actions)
        skipped_delta = tuple(
            (path, proposed_skipped.get(path))
            for path in sorted(active_skipped.keys() | proposed_skipped.keys())
            if active_skipped.get(path) != proposed_skipped.get(path)
        )
    content_empty = int(metadata["file_count"]) > 0 and placed_content == 0
    result = {
        "plan_id": plan_id,
        "base_revision": active,
        "baseline_fingerprint": fingerprint,
        "valid": not findings,
        "findings": findings,
        "acknowledgements_required": {
            "exclusions": excluded > 0,
            "content_empty": content_empty,
        },
        "total_entry_count": int(state["total_entry_count"]),
        "affected_entry_count": affected_count,
        "changed_entry_count": changed_count,
        "placed_entry_count": placed,
        "excluded_entry_count": excluded,
        "placed_content_entry_count": placed_content,
        "total_placed_bytes": placed_bytes,
        "no_change": no_change,
        "phase_timings_ms": {
            "decode": round((decoded - started) * 1000, 3),
            "resolve_and_validate": round((perf_counter() - decoded) * 1000, 3),
        },
        "structure_delta": {
            "entries_changed": changed_count,
            "directories_added": [p for p, op in directory_delta if op == "add"],
            "directories_removed": [p for p, op in directory_delta if op == "remove"],
            "skipped_actions_changed": [p for p, _ in skipped_delta],
        },
    }
    return Compilation(
        result,
        layout,
        tuple(changed_entries),
        directories,
        skipped_actions,
        directory_delta,
        skipped_delta,
        no_change,
    )


@run_workspace_refusal
def export_layout(
    analysis_run: Any, plan_id: str, output_path: Any
) -> dict[str, object]:
    run_path, database_path = resolve_run_directory(analysis_run)
    output = Path(output_path)
    if output.exists():
        raise ConsolidationPlanError(f"Layout output already exists: {output}")
    with open_read_only(database_path) as connection:
        select_plan_row(connection, plan_id)
        active = int(
            connection.execute(
                "SELECT active_revision FROM plan_layout_state WHERE plan_id=?",
                (plan_id,),
            ).fetchone()[0]
        )
        fingerprint = str(
            connection.execute(
                "SELECT fingerprint FROM plan_baseline_metadata WHERE plan_id=?",
                (plan_id,),
            ).fetchone()[0]
        )
        layout = _canonical_authoring(
            _current_authoring(connection, plan_id, active), active, fingerprint
        )
    output.write_text(
        json.dumps(layout, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {
        "analysis_run": str(run_path),
        "plan_id": plan_id,
        "base_revision": active,
        "baseline_fingerprint": fingerprint,
        "output": str(output),
        "layout_schema_version": 2,
    }


@run_workspace_refusal
def validate_layout(
    analysis_run: Any, plan_id: str, layout_path: Any
) -> dict[str, Any]:
    _, database_path = resolve_run_directory(analysis_run)
    with open_read_only(database_path) as connection:
        select_plan_row(connection, plan_id)
        return _compile(connection, plan_id, str(layout_path)).validation


def _persist_rules(
    connection: sqlite3.Connection, plan_id: str, revision: int, layout: dict[str, Any]
) -> None:
    rows = [
        (
            plan_id,
            revision,
            "rule",
            str(item["selector"]["subtree"]),
            _canonical_json(item),
        )
        for item in layout["rules"]
    ]
    rows += [
        (plan_id, revision, "exception", str(item["entry_id"]), _canonical_json(item))
        for item in layout["entry_exceptions"]
    ]
    connection.executemany("INSERT INTO plan_layout_rules VALUES(?,?,?,?,?)", rows)


@run_workspace_refusal
def apply_layout(
    analysis_run: Any,
    plan_id: str,
    layout_path: Any,
    acknowledge_exclusions: bool = False,
    acknowledge_content_empty: bool = False,
    *,
    performance_metrics: dict[str, int] | None = None,
) -> dict[str, Any]:
    run_path, database_path = resolve_run_directory(analysis_run)
    connection = open_wal_read_write(
        database_path,
        connection_factory=(
            _ObservedConnection
            if performance_metrics is not None
            else sqlite3.Connection
        ),
    )
    vm_samples = 0
    if performance_metrics is not None:
        assert isinstance(connection, _ObservedConnection)
        connection.metrics = performance_metrics
        performance_metrics["sqlite_rows_read"] = 0
        initial_changes = connection.total_changes

        def count_vm_steps() -> int:
            nonlocal vm_samples
            vm_samples += 1
            return 0

        connection.set_progress_handler(count_vm_steps, 1000)
    try:
        connection.execute("BEGIN IMMEDIATE")
        plan = select_plan_row(connection, plan_id)
        if plan["status"] != PLAN_STATUS_DRAFT:
            raise ConsolidationPlanError(
                f"Consolidation Plan is not a draft: {plan_id}"
            )
        compiled = _compile(connection, plan_id, str(layout_path))
        result = compiled.validation
        if not result["valid"]:
            raise ConsolidationPlanError(
                json.dumps(result, ensure_ascii=False, sort_keys=True)
            )
        if compiled.no_change:
            connection.rollback()
            return {
                "analysis_run": str(run_path),
                "revision": result["base_revision"],
                **result,
            }
        if (
            result["acknowledgements_required"]["exclusions"]
            and not acknowledge_exclusions
        ):
            raise ConsolidationPlanError(
                "Applying a layout with exclusions requires --acknowledge-exclusions"
            )
        if (
            result["acknowledgements_required"]["content_empty"]
            and not acknowledge_content_empty
        ):
            raise ConsolidationPlanError(
                "Applying a Content-Empty Result requires --acknowledge-content-empty"
            )
        active, revision = (
            int(result["base_revision"]),
            int(result["base_revision"]) + 1,
        )
        assert compiled.layout is not None
        authoring = _canonical_authoring(
            compiled.layout, revision, str(result["baseline_fingerprint"])
        )
        connection.execute(
            "INSERT INTO plan_layout_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                plan_id,
                revision,
                _canonical_json(authoring),
                _fingerprint(_semantic_layout(authoring)),
                result["baseline_fingerprint"],
                _canonical_json(result),
                result["total_entry_count"],
                result["affected_entry_count"],
                result["changed_entry_count"],
                result["placed_entry_count"],
                result["excluded_entry_count"],
                result["placed_content_entry_count"],
                result["total_placed_bytes"],
                int(result["acknowledgements_required"]["content_empty"]),
                datetime.now(UTC).isoformat(),
            ),
        )
        _persist_rules(connection, plan_id, revision, authoring)
        connection.executemany(
            "INSERT INTO plan_layout_entry_deltas VALUES(?,?,?,?,?)",
            [
                (
                    plan_id,
                    revision,
                    _entry_id(entry.baseline),
                    entry.disposition,
                    entry.output_relative_path,
                )
                for entry in compiled.changed_entries
            ],
        )
        connection.executemany(
            "UPDATE plan_layout_active_entries SET disposition='updating', output_relative_path=NULL "
            "WHERE plan_id=? AND entry_id=?",
            [
                (plan_id, _entry_id(entry.baseline))
                for entry in compiled.changed_entries
            ],
        )
        for entry in compiled.changed_entries:
            connection.execute(
                "UPDATE plan_layout_active_entries SET disposition=?, output_relative_path=? WHERE plan_id=? AND entry_id=?",
                (
                    entry.disposition,
                    entry.output_relative_path,
                    plan_id,
                    _entry_id(entry.baseline),
                ),
            )
        connection.executemany(
            "INSERT INTO plan_layout_directory_deltas VALUES(?,?,?,?)",
            [(plan_id, revision, p, op) for p, op in compiled.directory_delta],
        )
        for path, operation in compiled.directory_delta:
            if operation == "add":
                connection.execute(
                    "INSERT INTO plan_layout_active_directories VALUES(?,?)",
                    (plan_id, path),
                )
            else:
                connection.execute(
                    "DELETE FROM plan_layout_active_directories WHERE plan_id=? AND output_relative_path=?",
                    (plan_id, path),
                )
        connection.executemany(
            "INSERT INTO plan_layout_skipped_action_deltas VALUES(?,?,?,?)",
            [(plan_id, revision, p, a) for p, a in compiled.skipped_delta],
        )
        for path, action in compiled.skipped_delta:
            if action is None:
                connection.execute(
                    "DELETE FROM plan_layout_active_skipped_actions WHERE plan_id=? AND relative_path=?",
                    (plan_id, path),
                )
            else:
                connection.execute(
                    "INSERT INTO plan_layout_active_skipped_actions VALUES(?,?,?) ON CONFLICT(plan_id,relative_path) DO UPDATE SET action=excluded.action",
                    (plan_id, path, action),
                )
        cursor = connection.execute(
            "UPDATE plan_layout_state SET active_revision=?, content_empty=?, placed_entry_count=?, excluded_entry_count=?, placed_content_entry_count=?, total_placed_bytes=? WHERE plan_id=? AND active_revision=?",
            (
                revision,
                int(result["acknowledgements_required"]["content_empty"]),
                result["placed_entry_count"],
                result["excluded_entry_count"],
                result["placed_content_entry_count"],
                result["total_placed_bytes"],
                plan_id,
                active,
            ),
        )
        if cursor.rowcount != 1:
            raise ConsolidationPlanError("Layout base_revision is stale")
        acknowledgements: list[tuple[str, int, str, str]] = []
        if acknowledge_exclusions:
            acknowledgements.append((plan_id, revision, "exclusions", "accepted"))
        if acknowledge_content_empty:
            acknowledgements.append((plan_id, revision, "content-empty", "accepted"))
        connection.executemany(
            "INSERT INTO plan_layout_acknowledgements VALUES(?,?,?,?)", acknowledgements
        )
        connection.commit()
        if performance_metrics is not None:
            performance_metrics["sqlite_rows_written"] = (
                connection.total_changes - initial_changes
            )
            performance_metrics["sqlite_vm_steps_lower_bound"] = vm_samples * 1000
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
    return {"analysis_run": str(run_path), "revision": revision, **result}


class Projection(NamedTuple):
    entries: list[tuple[str, str, str, str | None]]
    directories: set[str]
    skipped_actions: dict[str, str]


def _reconstruct(
    connection: sqlite3.Connection, plan_id: str, revision: int
) -> Projection:
    entries: dict[str, list[str | None]] = {
        str(row["entry_id"]): [
            str(row["entry_id"]),
            str(row["entry_kind"]),
            "place",
            str(row["baseline_path"]),
        ]
        for row in connection.execute(
            "SELECT entry_id, entry_kind, baseline_path FROM plan_baseline_entries WHERE plan_id=?",
            (plan_id,),
        )
    }
    for row in connection.execute(
        "SELECT entry_id, disposition, output_relative_path FROM plan_layout_entry_deltas WHERE plan_id=? AND revision<=? ORDER BY revision, entry_id",
        (plan_id, revision),
    ):
        entries[str(row["entry_id"])][2:] = [
            str(row["disposition"]),
            row["output_relative_path"],
        ]
    directories: set[str] = set()
    for row in connection.execute(
        "SELECT output_relative_path, operation FROM plan_layout_directory_deltas WHERE plan_id=? AND revision<=? ORDER BY revision, output_relative_path",
        (plan_id, revision),
    ):
        (directories.add if row["operation"] == "add" else directories.discard)(
            str(row["output_relative_path"])
        )
    skipped: dict[str, str] = {}
    for row in connection.execute(
        "SELECT relative_path, action FROM plan_layout_skipped_action_deltas WHERE plan_id=? AND revision<=? ORDER BY revision, relative_path",
        (plan_id, revision),
    ):
        if row["action"] is None:
            skipped.pop(str(row["relative_path"]), None)
        else:
            skipped[str(row["relative_path"])] = str(row["action"])
    ordered = [
        (str(v[0]), str(v[1]), str(v[2]), v[3])
        for v in sorted(entries.values(), key=lambda value: str(value[0]))
    ]
    return Projection(ordered, directories, skipped)


def _active_projection(connection: sqlite3.Connection, plan_id: str) -> Projection:
    entries = [
        (
            str(row["entry_id"]),
            str(row["entry_kind"]),
            str(row["disposition"]),
            row["output_relative_path"],
        )
        for row in connection.execute(
            "SELECT entry_id, entry_kind, disposition, output_relative_path FROM plan_layout_active_entries WHERE plan_id=? ORDER BY entry_id",
            (plan_id,),
        )
    ]
    directories, skipped = _active_maps(connection, plan_id)
    return Projection(entries, directories, skipped)


def _projection_payload(projection: Projection) -> dict[str, object]:
    entries, directories, skipped = projection
    return {
        "entries": entries,
        "directories": sorted(directories),
        "skipped_actions": sorted(skipped.items()),
    }


def _reconstructed_aggregates(
    connection: sqlite3.Connection, plan_id: str, projection: Projection
) -> tuple[int, int, int, int, int, int]:
    entries = projection[0]
    excluded_files = {
        entry_id
        for entry_id, kind, disposition, _ in entries
        if kind == "file" and disposition == "exclude"
    }
    metadata = connection.execute(
        "SELECT file_count, total_bytes FROM plan_baseline_metadata WHERE plan_id=?",
        (plan_id,),
    ).fetchone()
    excluded_bytes = sum(
        int(row["expected_byte_size"] or 0)
        for row in connection.execute(
            "SELECT entry_id, expected_byte_size FROM plan_baseline_entries "
            "WHERE plan_id=? AND entry_kind='file'",
            (plan_id,),
        )
        if str(row["entry_id"]) in excluded_files
    )
    placed = sum(disposition == "place" for _, _, disposition, _ in entries)
    excluded = len(entries) - placed
    placed_content = int(metadata["file_count"]) - len(excluded_files)
    return (
        len(entries),
        placed,
        excluded,
        placed_content,
        int(metadata["total_bytes"]) - excluded_bytes,
        int(int(metadata["file_count"]) > 0 and placed_content == 0),
    )


def _active_aggregate_tuple(
    connection: sqlite3.Connection, plan_id: str
) -> tuple[int, int, int, int, int, int]:
    state = connection.execute(
        "SELECT * FROM plan_layout_state WHERE plan_id=?", (plan_id,)
    ).fetchone()
    return (
        int(state["total_entry_count"]),
        int(state["placed_entry_count"]),
        int(state["excluded_entry_count"]),
        int(state["placed_content_entry_count"]),
        int(state["total_placed_bytes"]),
        int(state["content_empty"]),
    )


@run_workspace_refusal
def rebuild_active_projection(
    analysis_run: Any, plan_id: str, *, repair: bool = False
) -> dict[str, object]:
    run_path, database_path = resolve_run_directory(analysis_run)
    connection = (
        open_wal_read_write(database_path) if repair else open_read_only(database_path)
    )
    try:
        connection.execute("BEGIN IMMEDIATE" if repair else "BEGIN")
        plan = select_plan_row(connection, plan_id)
        if repair and plan["status"] != PLAN_STATUS_DRAFT:
            raise ConsolidationPlanError(
                "Only draft plans may repair derived active state"
            )
        revision = int(
            connection.execute(
                "SELECT active_revision FROM plan_layout_state WHERE plan_id=?",
                (plan_id,),
            ).fetchone()[0]
        )
        reconstructed = _reconstruct(connection, plan_id, revision)
        before = _active_projection(connection, plan_id)
        aggregates = _reconstructed_aggregates(connection, plan_id, reconstructed)
        consistent = reconstructed == before and aggregates == _active_aggregate_tuple(
            connection, plan_id
        )
        was_consistent = consistent
        if repair and not consistent:
            entries, directories, skipped = reconstructed
            connection.execute(
                "DELETE FROM plan_layout_active_entries WHERE plan_id=?", (plan_id,)
            )
            connection.executemany(
                "INSERT INTO plan_layout_active_entries VALUES(?,?,?,?,?)",
                [(plan_id, *row) for row in entries],
            )
            connection.execute(
                "DELETE FROM plan_layout_active_directories WHERE plan_id=?", (plan_id,)
            )
            connection.executemany(
                "INSERT INTO plan_layout_active_directories VALUES(?,?)",
                [(plan_id, path) for path in sorted(directories)],
            )
            connection.execute(
                "DELETE FROM plan_layout_active_skipped_actions WHERE plan_id=?",
                (plan_id,),
            )
            connection.executemany(
                "INSERT INTO plan_layout_active_skipped_actions VALUES(?,?,?)",
                [(plan_id, path, action) for path, action in sorted(skipped.items())],
            )
        if repair and aggregates != _active_aggregate_tuple(connection, plan_id):
            connection.execute(
                "UPDATE plan_layout_state SET total_entry_count=?, placed_entry_count=?, "
                "excluded_entry_count=?, placed_content_entry_count=?, total_placed_bytes=?, "
                "content_empty=? WHERE plan_id=?",
                (*aggregates, plan_id),
            )
        if repair:
            consistent = reconstructed == _active_projection(
                connection, plan_id
            ) and aggregates == _active_aggregate_tuple(connection, plan_id)
        connection.commit() if repair else connection.rollback()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "analysis_run": str(run_path),
        "plan_id": plan_id,
        "revision": revision,
        "consistent": consistent,
        "repaired": repair and not was_consistent,
        "reconstructed_fingerprint": _fingerprint(_projection_payload(reconstructed)),
        "active_fingerprint": _fingerprint(
            _projection_payload(before if not repair else reconstructed)
        ),
    }


def finalize_layout_projection(
    connection: sqlite3.Connection, plan_id: str, finalized_at: str
) -> dict[str, object]:
    """Verify derived state and atomically persist a complete immutable snapshot."""
    state = connection.execute(
        "SELECT * FROM plan_layout_state WHERE plan_id=?", (plan_id,)
    ).fetchone()
    revision = int(state["active_revision"])
    reconstructed = _reconstruct(connection, plan_id, revision)
    if reconstructed != _active_projection(
        connection, plan_id
    ) or _reconstructed_aggregates(
        connection, plan_id, reconstructed
    ) != _active_aggregate_tuple(connection, plan_id):
        raise ConsolidationPlanError("active-projection-inconsistent")
    entries, directories, skipped = reconstructed
    placed = [entry for entry in entries if entry[2] == "place"]
    excluded = [entry for entry in entries if entry[2] == "exclude"]
    connection.execute(
        "INSERT INTO final_plan_entries "
        "(plan_id, entry_index, entry_id, entry_kind, source_relative_path, "
        "output_relative_path, expected_byte_size, algorithm, algorithm_version, "
        "digest, reason, evidence_kind, evidence_entry_type, evidence_mtime_ns, "
        "evidence_directory_path, evidence_layout_revision) "
        "SELECT active.plan_id, row_number() OVER (ORDER BY active.output_relative_path, active.entry_id)-1, "
        "active.entry_id, active.entry_kind, selection.source_relative_path, active.output_relative_path, "
        "baseline.expected_byte_size, baseline.algorithm, baseline.algorithm_version, baseline.digest, "
        "COALESCE(selection.reason, baseline.reason), "
        "CASE WHEN selection.source_relative_path IS NOT NULL THEN baseline.evidence_kind "
        "ELSE 'layout-owned-directory' END, "
        "CASE WHEN baseline.evidence_kind = 'metadata-observation' "
        "THEN 'regular-file' END, "
        "CASE WHEN baseline.evidence_kind = 'metadata-observation' "
        "THEN baseline.modified_ns END, "
        "CASE WHEN selection.source_relative_path IS NULL THEN active.output_relative_path END, "
        "CASE WHEN selection.source_relative_path IS NULL THEN ? END "
        "FROM plan_layout_active_entries AS active "
        "JOIN plan_baseline_entries AS baseline ON baseline.plan_id=active.plan_id AND baseline.entry_id=active.entry_id "
        "LEFT JOIN plan_source_selections AS selection ON selection.plan_id=active.plan_id AND selection.entry_id=active.entry_id "
        "WHERE active.plan_id=? AND active.disposition='place'",
        (revision, plan_id),
    )
    connection.executemany(
        "INSERT INTO final_plan_entries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                plan_id,
                len(placed) + offset,
                "layout-directory:" + _fingerprint(output_path)[:24],
                "directory",
                None,
                output_path,
                None,
                None,
                None,
                None,
                "Layout-Owned Directory",
                "layout-owned-directory",
                None,
                None,
                output_path,
                revision,
            )
            for offset, output_path in enumerate(sorted(directories))
        ],
    )
    connection.execute(
        "INSERT INTO final_plan_exclusions "
        "SELECT active.plan_id, active.entry_id, identity.relative_path, baseline.expected_byte_size, baseline.digest "
        "FROM plan_layout_active_entries AS active JOIN plan_baseline_entries AS baseline "
        "ON baseline.plan_id=active.plan_id AND baseline.entry_id=active.entry_id "
        "JOIN consolidation_plans AS plan ON plan.plan_id=active.plan_id "
        "JOIN content_identities AS identity ON identity.run_id=plan.run_id "
        "AND identity.algorithm=baseline.algorithm AND identity.algorithm_version=baseline.algorithm_version "
        "AND identity.byte_size=baseline.expected_byte_size AND identity.digest=baseline.digest "
        "WHERE active.plan_id=? AND active.disposition='exclude' AND active.entry_kind='file' "
        "AND baseline.evidence_kind='content-identity' "
        "UNION ALL "
        "SELECT active.plan_id, active.entry_id, baseline.source_relative_path, "
        "baseline.expected_byte_size, baseline.digest "
        "FROM plan_layout_active_entries AS active JOIN plan_baseline_entries AS baseline "
        "ON baseline.plan_id=active.plan_id AND baseline.entry_id=active.entry_id "
        "WHERE active.plan_id=? AND active.disposition='exclude' AND active.entry_kind='file' "
        "AND baseline.evidence_kind='metadata-observation'",
        (plan_id, plan_id),
    )
    connection.executemany(
        "INSERT INTO final_plan_skipped_actions VALUES(?,?,?)",
        [(plan_id, path, action) for path, action in sorted(skipped.items())],
    )
    fingerprint = _final_projection_fingerprint(connection, plan_id, revision)
    connection.execute(
        "INSERT INTO final_plan_projection VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            plan_id,
            revision,
            len(entries),
            len(placed),
            len(excluded),
            len(directories),
            len(skipped),
            int(state["total_placed_bytes"]),
            int(state["content_empty"]),
            fingerprint,
            finalized_at,
        ),
    )
    return {
        "revision": revision,
        "projection_fingerprint": fingerprint,
        "entry_count": len(entries),
    }
