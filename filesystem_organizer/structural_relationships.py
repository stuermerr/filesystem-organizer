"""Persisted, deterministic structural evidence for Directory Trees."""

from __future__ import annotations

import os
import sqlite3
import stat
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from blake3 import blake3

from .content_identity import FileObservation, hash_regular_file
from .exact_duplicate_groups import READ_OUTCOME_SUCCESSFUL

FINGERPRINT_VERSION = 1
FINGERPRINT_ALGORITHM = "BLAKE3-256"
STRUCTURAL_ANALYSIS_SCHEMA_VERSION = 5
_STRUCTURAL_PHASES = frozenset(
    {"candidates", "relationships", "finalization", "complete"}
)
_STRUCTURAL_BATCH_MAX_ROWS = 500
_STRUCTURAL_BATCH_MAX_BYTES = 64 * 1024 * 1024
_STRUCTURAL_BATCH_MAX_PAYLOAD_BYTES = _STRUCTURAL_BATCH_MAX_BYTES - 1024 * 1024
CANONICAL_REASON_RANKING = "shallowest relative path, then lexical relative-path order"


@dataclass(frozen=True)
class Relationship:
    left_root: str
    right_root: str
    classification: str
    canonical_root: str
    canonical_reason: str
    qualifications: tuple[str, ...]
    unverified_counterparts: tuple[str, ...]


class _StructuralEvidenceBatch:
    """Commit bounded structural evidence transactions with FULL WAL durability."""

    def __init__(self, connection: sqlite3.Connection, checkpoint: str) -> None:
        self._connection = connection
        self._checkpoint = checkpoint
        self._rows = 0
        self._bytes = 0

    def write(self, statement: str, parameters: tuple[object, ...]) -> None:
        row_bytes = _evidence_row_bytes(parameters)
        if row_bytes > _STRUCTURAL_BATCH_MAX_PAYLOAD_BYTES:
            raise sqlite3.DatabaseError(
                "Structural evidence row exceeds the durable batch limit"
            )
        if self._rows and (
            self._rows == _STRUCTURAL_BATCH_MAX_ROWS
            or self._bytes + row_bytes > _STRUCTURAL_BATCH_MAX_PAYLOAD_BYTES
        ):
            self.commit()
        if self._rows == 0:
            self._connection.execute("BEGIN IMMEDIATE")
        self._connection.execute(statement, parameters)
        self._rows += 1
        self._bytes += row_bytes

    def commit(self) -> None:
        if self._rows == 0:
            return
        self._connection.commit()
        _checkpoint(self._checkpoint)
        self._rows = 0
        self._bytes = 0


def _evidence_row_bytes(parameters: tuple[object, ...]) -> int:
    """Bound encoded payload below 64 MiB, reserving SQLite/WAL framing space."""
    return sum(len(str(value).encode("utf-8")) for value in parameters)


def create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS directory_fingerprints (
          run_id TEXT NOT NULL, root_relative_path TEXT NOT NULL,
          fingerprint_version INTEGER NOT NULL, algorithm TEXT NOT NULL,
          digest BLOB NOT NULL, PRIMARY KEY (run_id, root_relative_path)
        );
        CREATE INDEX IF NOT EXISTS directory_fingerprint_lookup
          ON directory_fingerprints (run_id, digest);
        CREATE TABLE IF NOT EXISTS actionable_exact_component_members (
          run_id TEXT NOT NULL, component_id TEXT NOT NULL,
          root_relative_path TEXT NOT NULL,
          PRIMARY KEY (run_id, component_id, root_relative_path)
        );
        CREATE TABLE IF NOT EXISTS structural_shared_descendants (
          run_id TEXT NOT NULL, root_relative_path TEXT NOT NULL,
          descendant_relative_path TEXT NOT NULL, entry_kind TEXT NOT NULL,
          identity TEXT NOT NULL,
          PRIMARY KEY (run_id, root_relative_path, descendant_relative_path)
        );
        CREATE INDEX IF NOT EXISTS structural_shared_descendant_lookup
          ON structural_shared_descendants
          (run_id, descendant_relative_path, entry_kind, identity);
        CREATE TABLE IF NOT EXISTS directory_relationships (
          run_id TEXT NOT NULL, left_root TEXT NOT NULL, right_root TEXT NOT NULL,
          classification TEXT NOT NULL, canonical_root TEXT NOT NULL,
          canonical_reason TEXT NOT NULL, qualifications TEXT NOT NULL,
          unverified_counterparts TEXT NOT NULL,
          PRIMARY KEY (run_id, left_root, right_root)
        );
        CREATE TABLE IF NOT EXISTS relationship_conflicts (
          run_id TEXT NOT NULL, left_root TEXT NOT NULL, right_root TEXT NOT NULL,
          descendant_relative_path TEXT NOT NULL, left_evidence TEXT NOT NULL,
          right_evidence TEXT NOT NULL,
          PRIMARY KEY (run_id, left_root, right_root, descendant_relative_path)
        );
        CREATE TABLE IF NOT EXISTS structural_analysis (
          run_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL,
          phase TEXT NOT NULL, candidate_count INTEGER NOT NULL,
          comparison_count INTEGER NOT NULL, status TEXT NOT NULL,
          CHECK ((phase = 'complete' AND status = 'complete') OR
                 (phase != 'complete' AND status = 'in-progress'))
        );
        """
    )


def _relative_to(root: str, path: str) -> str | None:
    if root == ".":
        return None if path == "." else path
    if path == root:
        return None
    prefix = f"{root}/"
    return path[len(prefix) :] if path.startswith(prefix) else None


def _fingerprint(records: list[tuple[str, str, str]]) -> str:
    digest = blake3()
    digest.update(b"filesystem-organizer:directory-fingerprint:v1\\0")
    for path, kind, value in records:
        for field in (path, kind, value):
            encoded = field.encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    return digest.hexdigest()


def _canonical_root(left: str, right: str, classification: str) -> tuple[str, str]:
    if classification == "strict-subset":
        return right, "strict superset"
    if classification == "strict-superset":
        return left, "strict superset"
    return min(
        (left, right), key=lambda path: (path.count("/"), path)
    ), CANONICAL_REASON_RANKING


def _persist_directory_tree_evidence(
    connection: sqlite3.Connection, run_id: str
) -> dict[str, list[tuple[str, str, str]]]:
    """Index every Directory Tree by propagating each entry to its ancestors.

    Each inventory entry is visited once and contributes evidence only along its
    ancestor chain.  This is bottom-up construction of the persisted Directory
    Tree index, rather than a complete-inventory scan for every directory root.
    """
    roots = {
        str(row[0])
        for row in connection.execute(
            "SELECT relative_path FROM directory_evidence WHERE run_id = ? "
            "AND read_outcome = ?",
            (run_id, READ_OUTCOME_SUCCESSFUL),
        )
    }
    records_by_root: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    entries = connection.execute(
        "SELECT inventory_entries.relative_path, inventory_entries.entry_kind, "
        "inventory_entries.read_outcome, directory_evidence.is_empty, "
        "content_identities.algorithm, content_identities.algorithm_version, "
        "content_identities.byte_size, content_identities.digest, "
        "content_identities.read_outcome "
        "FROM inventory_entries "
        "LEFT JOIN directory_evidence ON directory_evidence.run_id = inventory_entries.run_id "
        "AND directory_evidence.relative_path = inventory_entries.relative_path "
        "LEFT JOIN content_identities ON content_identities.run_id = inventory_entries.run_id "
        "AND content_identities.relative_path = inventory_entries.relative_path "
        "WHERE inventory_entries.run_id = ? ORDER BY inventory_entries.relative_path",
        (run_id,),
    )
    for row in entries:
        path, kind, outcome = (str(row[0]), str(row[1]), str(row[2]))
        if path == ".":
            continue
        if kind == "regular-file" and outcome == READ_OUTCOME_SUCCESSFUL:
            if row[8] != READ_OUTCOME_SUCCESSFUL:
                record = ("unprocessable", "identity-not-proven")
            else:
                record = (
                    "regular-file",
                    ":".join(str(row[index]) for index in range(4, 8)),
                )
        elif kind == "directory" and outcome == READ_OUTCOME_SUCCESSFUL:
            record = ("empty-directory" if bool(row[3]) else "directory", "")
        else:
            record = ("unprocessable", f"{kind}:{outcome}")
        parts = path.split("/")
        for length in range(len(parts)):
            root = "." if length == 0 else "/".join(parts[:length])
            if root not in roots:
                continue
            relative = "/".join(parts[length:])
            records_by_root[root].append((relative, *record))

    evidence = _StructuralEvidenceBatch(connection, "tree-index-batch")
    for root in sorted(roots):
        records_by_root[root].sort(key=lambda record: (record[0], record[1]))
        for relative, kind, value in records_by_root[root]:
            evidence.write(
                "INSERT OR IGNORE INTO structural_shared_descendants VALUES (?, ?, ?, ?, ?)",
                (run_id, root, relative, kind, value),
            )
    evidence.commit()
    return records_by_root


def _indexed_tree_records(
    connection: sqlite3.Connection, run_id: str, roots: set[str]
) -> dict[str, list[tuple[str, str, str]]]:
    """Read persisted Directory Tree evidence for the candidate roots only."""
    result: dict[str, list[tuple[str, str, str]]] = {root: [] for root in roots}
    for row in connection.execute(
        "SELECT root_relative_path, descendant_relative_path, entry_kind, identity "
        "FROM structural_shared_descendants WHERE run_id = ? ORDER BY "
        "root_relative_path, descendant_relative_path, entry_kind",
        (run_id,),
    ):
        root = str(row[0])
        if root in result:
            result[root].append((str(row[1]), str(row[2]), str(row[3])))
    return result


def _metadata_shared_descendants(
    connection: sqlite3.Connection, run_id: str, roots: set[str]
) -> dict[tuple[str, str, str], list[str]]:
    """Index same-path, same-size file observations without using payload proof."""
    shared: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    entries = connection.execute(
        "SELECT relative_path, observed_byte_size FROM inventory_entries "
        "WHERE run_id = ? AND entry_kind = 'regular-file' AND read_outcome = ? "
        "ORDER BY relative_path",
        (run_id, READ_OUTCOME_SUCCESSFUL),
    )
    for path_value, byte_size in entries:
        path = str(path_value)
        parts = path.split("/")
        for length in range(len(parts)):
            root = "." if length == 0 else "/".join(parts[:length])
            if root not in roots:
                continue
            relative = "/".join(parts[length:])
            shared[(relative, "regular-file", f"size:{int(byte_size)}")].append(root)
    return shared


def _classify(
    left: list[tuple[str, str, str]], right: list[tuple[str, str, str]]
) -> tuple[str, tuple[str, ...], tuple[str, ...], tuple[tuple[str, str, str], ...]]:
    left_map, right_map = (
        {
            path: ("directory", "")
            if kind in {"directory", "empty-directory"}
            else (kind, value)
            for path, kind, value in records
        }
        for records in (left, right)
    )
    qualifications = sorted(
        {path for path, (kind, _) in left_map.items() if kind == "unprocessable"}
        | {path for path, (kind, _) in right_map.items() if kind == "unprocessable"}
    )
    unverified = sorted(
        f"{path} ({'left' if left_map.get(path, ('', ''))[0] == 'unprocessable' else 'right'} unprocessable)"
        for path in qualifications
        if (left_map.get(path, ("", ""))[0] == "unprocessable")
        != (right_map.get(path, ("", ""))[0] == "unprocessable")
    )
    copyable = {"regular-file", "directory"}
    left_copyable = {
        path: item for path, item in left_map.items() if item[0] in copyable
    }
    right_copyable = {
        path: item for path, item in right_map.items() if item[0] in copyable
    }
    conflicts = tuple(
        sorted(
            (path, ":".join(left_map[path]), ":".join(right_map[path]))
            for path in left_map.keys() & right_map.keys()
            if left_map[path][0] != "unprocessable"
            and right_map[path][0] != "unprocessable"
            and left_map[path] != right_map[path]
        )
    )
    conflict = bool(conflicts)
    if conflict:
        return "conflicting", tuple(qualifications), tuple(unverified), conflicts
    if left_copyable == right_copyable:
        return "identical", tuple(qualifications), tuple(unverified), conflicts
    if left_copyable.items() < right_copyable.items():
        return "strict-subset", tuple(qualifications), tuple(unverified), conflicts
    if right_copyable.items() < left_copyable.items():
        return "strict-superset", tuple(qualifications), tuple(unverified), conflicts
    return "union-compatible", tuple(qualifications), tuple(unverified), conflicts


def _is_actionable_relationship(
    left_root: str,
    right_root: str,
    classification: str,
    left: list[tuple[str, str, str]],
    right: list[tuple[str, str, str]],
) -> bool:
    """Require dominant shared proof for non-containment relationships.

    Different direct backup wrappers containing the same complete relative root
    path provide explicit structural context. Otherwise, proven same-path file
    identities must strictly outnumber every other regular-file position in
    each tree and span at least two independent top-level descendant directory
    regions. Root-level files contribute overlap but not independent boundary
    evidence. This applies to exact and non-identical trees alike.
    """
    left_files = {
        path: identity for path, kind, identity in left if kind == "regular-file"
    }
    right_files = {
        path: identity for path, kind, identity in right if kind == "regular-file"
    }
    shared_paths = {
        path
        for path in left_files.keys() & right_files.keys()
        if left_files[path] == right_files[path]
    }
    shared_regions = {
        path.split("/", maxsplit=1)[0] for path in shared_paths if "/" in path
    }
    unproven_file_counts = [
        sum(
            kind == "unprocessable"
            and (value == "identity-not-proven" or value.startswith("regular-file:"))
            for _, kind, value in records
        )
        for records in (left, right)
    ]
    left_parts = left_root.split("/")
    right_parts = right_root.split("/")
    matching_wrapped_context = (
        len(left_parts) > 2
        and len(right_parts) > 2
        and left_parts[0] != right_parts[0]
        and left_parts[1:] == right_parts[1:]
    )
    return matching_wrapped_context or (
        len(shared_regions) >= 2
        and len(shared_paths) * 2 > len(left_files) + unproven_file_counts[0]
        and len(shared_paths) * 2 > len(right_files) + unproven_file_counts[1]
    )


def _actionable_exact_components[FingerprintKey](
    fingerprints: Mapping[FingerprintKey, Sequence[str]],
    records_by_root: Mapping[str, list[tuple[str, str, str]]],
) -> tuple[dict[str, tuple[str, ...]], dict[str, str]]:
    """Qualify identical evidence once, then partition by wrapper context.

    Within a fingerprint bucket all descendant records are identical. Dominant
    shared proof therefore qualifies the entire bucket; otherwise only equal
    relative paths beneath distinct wrappers can connect its members.
    """
    components: dict[str, tuple[str, ...]] = {}
    for roots in fingerprints.values():
        members = sorted(set(roots))
        if len(members) < 2:
            continue
        representative = members[0]
        records = records_by_root[representative]
        if _is_actionable_relationship(
            representative, representative, "identical", records, records
        ):
            components[representative] = tuple(members)
            continue
        by_context: dict[tuple[str, ...], list[str]] = defaultdict(list)
        for root in members:
            parts = root.split("/")
            if len(parts) > 2:
                by_context[tuple(parts[1:])].append(root)
        for connected in by_context.values():
            if len(connected) > 1:
                components[connected[0]] = tuple(connected)
    component_by_root = {
        root: component_id
        for component_id, roots in components.items()
        for root in roots
    }
    return components, component_by_root


def analyze(connection: sqlite3.Connection, run_id: str, selected_root: str) -> None:
    """Durably derive graph evidence from one completed inventory snapshot."""
    create_schema(connection)
    structural = connection.execute(
        "SELECT phase, status FROM structural_analysis WHERE run_id = ?", (run_id,)
    ).fetchone()
    if structural is None:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO structural_analysis VALUES (?, ?, 'candidates', 0, 0, 'in-progress')",
            (run_id, STRUCTURAL_ANALYSIS_SCHEMA_VERSION),
        )
        connection.commit()
        phase = "candidates"
    else:
        phase = str(structural["phase"])
        schema_version = connection.execute(
            "SELECT schema_version FROM structural_analysis WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        if (
            int(schema_version) != STRUCTURAL_ANALYSIS_SCHEMA_VERSION
            or phase not in _STRUCTURAL_PHASES
        ):
            raise sqlite3.DatabaseError("Unsupported structural analysis checkpoint")
        if str(structural["status"]) == "complete":
            return

    candidates: set[tuple[str, str]] | None = None
    if phase == "candidates":
        candidates = _derive_candidates(connection, run_id, selected_root)
        phase = "relationships"
    if phase == "relationships":
        if candidates is None:
            candidates = _recompute_candidates(connection, run_id)
        _persist_relationships(connection, run_id, candidates)
        phase = "finalization"
    if phase == "finalization":
        connection.execute("BEGIN IMMEDIATE")
        candidate_count = connection.execute(
            "SELECT candidate_count FROM structural_analysis WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0]
        comparison_count = connection.execute(
            "SELECT COUNT(*) FROM directory_relationships WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        connection.execute(
            "UPDATE structural_analysis SET phase = 'complete', candidate_count = ?, "
            "comparison_count = ?, status = 'complete' WHERE run_id = ?",
            (candidate_count, comparison_count, run_id),
        )
        connection.commit()
        _checkpoint("graph-finalized")


def _checkpoint(_name: str) -> None:
    """Test seam called only after a structural transaction is durable."""


def _derive_candidates(
    connection: sqlite3.Connection, run_id: str, selected_root: str
) -> set[tuple[str, str]]:
    """Persist candidate evidence, then return the coalesced candidate pairs.

    Raw fingerprints remain complete evidence. Boundary-qualified exact
    components are persisted separately so reporting, planning, and resumed
    candidate coalescing consume the same actionable membership.
    """
    del selected_root  # Content identities are persisted atomically with inventory.
    records_by_root = _persist_directory_tree_evidence(connection, run_id)
    persisted_fingerprints = {
        str(row[0])
        for row in connection.execute(
            "SELECT root_relative_path FROM directory_fingerprints WHERE run_id = ?",
            (run_id,),
        )
    }
    fingerprints: dict[bytes, list[str]] = defaultdict(list)
    evidence = _StructuralEvidenceBatch(connection, "candidate-batch")
    for root, records in records_by_root.items():
        digest = bytes.fromhex(_fingerprint(records))
        if root not in persisted_fingerprints:
            evidence.write(
                "INSERT INTO directory_fingerprints VALUES (?, ?, ?, ?, ?)",
                (run_id, root, FINGERPRINT_VERSION, FINGERPRINT_ALGORITHM, digest),
            )
        fingerprints[digest].append(root)
    evidence.commit()
    exact_components, component_by_root = _actionable_exact_components(
        fingerprints, records_by_root
    )
    component_evidence = _StructuralEvidenceBatch(connection, "candidate-batch")
    for component_id, roots in exact_components.items():
        for root in roots:
            component_evidence.write(
                "INSERT OR IGNORE INTO actionable_exact_component_members VALUES (?, ?, ?)",
                (run_id, component_id, root),
            )
    component_evidence.commit()
    shared = _metadata_shared_descendants(
        connection, run_id, set(records_by_root)
    )
    candidates = _coalesced_candidates(exact_components, component_by_root, shared)

    connection.execute("BEGIN IMMEDIATE")
    for left, right in sorted(candidates):
        key = blake3(
            f"{len(left)}:{left}{len(right)}:{right}".encode()
        ).hexdigest()
        connection.execute(
            "INSERT OR IGNORE INTO analysis_candidates "
            "(run_id, candidate_kind, candidate_key, byte_size, "
            "left_relative_path, right_relative_path, state) "
            "VALUES (?, 'structural-pair', ?, NULL, ?, ?, 'pending')",
            (run_id, key, left, right),
        )
    candidate_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM analysis_candidates WHERE run_id = ? "
            "AND candidate_kind = 'structural-pair'",
            (run_id,),
        ).fetchone()[0]
    )
    connection.execute(
        "UPDATE structural_analysis SET phase = 'relationships', candidate_count = ? WHERE run_id = ?",
        (candidate_count, run_id),
    )
    connection.execute(
        "UPDATE evidence_discovery_progress SET emitted_candidate_count = ? "
        "WHERE run_id = ?",
        (candidate_count, run_id),
    )
    connection.commit()
    _checkpoint("candidate-phase")
    return candidates


def _coalesced_candidates[FingerprintKey](
    exact_components: Mapping[FingerprintKey, tuple[str, ...]],
    component_by_root: Mapping[str, FingerprintKey],
    shared: Mapping[tuple[str, str, str], Sequence[str]],
) -> set[tuple[str, str]]:
    """Coalesce exact identity components before expanding candidate pairs."""
    candidates: set[tuple[str, str]] = set()
    for shared_roots in shared.values():
        representatives = sorted(
            {
                exact_components[component_by_root[root]][0]
                if root in component_by_root
                else root
                for root in shared_roots
            }
        )
        candidates.update(pairwise(representatives))
    return candidates


def _recompute_candidates(
    connection: sqlite3.Connection, run_id: str
) -> set[tuple[str, str]]:
    """Rebuild the candidate pair set from persisted evidence after a crash.

    The candidates phase commits directory fingerprints and the indexed
    Directory Tree evidence before its checkpoint, so a resumed
    relationships phase can deterministically re-derive the exact identity
    components and the coalesced candidate pairs without stored pair rows.
    """
    return {
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            "SELECT left_relative_path, right_relative_path FROM analysis_candidates "
            "WHERE run_id = ? AND candidate_kind = 'structural-pair'",
            (run_id,),
        )
    }


def _persist_relationships(
    connection: sqlite3.Connection, run_id: str, candidates: set[tuple[str, str]]
) -> None:
    """Classify every durable candidate in bounded, restart-safe batches."""
    records_by_root = _indexed_tree_records(
        connection,
        run_id,
        {left for left, _ in candidates} | {right for _, right in candidates},
    )
    evidence = _StructuralEvidenceBatch(connection, "relationship-batch")
    persisted_conflicts = {
        (str(row[0]), str(row[1]), str(row[2]))
        for row in connection.execute(
            "SELECT left_root, right_root, descendant_relative_path FROM relationship_conflicts "
            "WHERE run_id = ?",
            (run_id,),
        )
    }
    for left, right in sorted(candidates):
        already_persisted = connection.execute(
            "SELECT 1 FROM directory_relationships WHERE run_id = ? AND left_root = ? AND right_root = ?",
            (run_id, left, right),
        ).fetchone()
        classification, qualifications, unverified, conflicts = _classify(
            records_by_root[left], records_by_root[right]
        )
        if not _is_actionable_relationship(
            left,
            right,
            classification,
            records_by_root[left],
            records_by_root[right],
        ):
            continue
        canonical, reason = _canonical_root(left, right, classification)
        if already_persisted is None:
            evidence.write(
                "INSERT INTO directory_relationships VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    left,
                    right,
                    classification,
                    canonical,
                    reason,
                    "\n".join(qualifications),
                    "\n".join(unverified),
                ),
            )
        for path, left_evidence, right_evidence in conflicts:
            if (left, right, path) not in persisted_conflicts:
                evidence.write(
                    "INSERT INTO relationship_conflicts VALUES (?, ?, ?, ?, ?, ?)",
                    (run_id, left, right, path, left_evidence, right_evidence),
                )
    evidence.commit()
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "UPDATE analysis_candidates SET state = 'evaluated' WHERE run_id = ? "
        "AND candidate_kind = 'structural-pair'",
        (run_id,),
    )
    comparison_count = connection.execute(
        "SELECT COUNT(*) FROM analysis_candidates WHERE run_id = ? "
        "AND candidate_kind = 'structural-pair' AND state = 'evaluated'",
        (run_id,),
    ).fetchone()[0]
    connection.execute(
        "UPDATE structural_analysis SET phase = 'finalization', comparison_count = ? WHERE run_id = ?",
        (comparison_count, run_id),
    )
    connection.execute(
        "UPDATE evidence_discovery_progress SET evaluated_candidate_count = ? "
        "WHERE run_id = ?",
        (comparison_count, run_id),
    )
    connection.commit()
    _checkpoint("relationship-phase")


def revalidate_structural_snapshot(
    connection: sqlite3.Connection, run_id: str, selected_root: Path, roots: set[str]
) -> None:
    """Refuse if a planned Structural Union's directory evidence has drifted."""
    non_nested_roots: list[str] = []
    for root in sorted(roots, key=lambda path: (path.count("/"), path)):
        if any(
            _relative_to(existing, root) is not None for existing in non_nested_roots
        ):
            continue
        non_nested_roots.append(root)
    roots = set(non_nested_roots)
    if not roots:
        return
    expected = connection.execute(
        "SELECT relative_path, entry_kind, observed_byte_size, modified_ns, read_outcome "
        "FROM inventory_entries WHERE run_id = ?",
        (run_id,),
    ).fetchall()
    identities = {
        str(row["relative_path"]): row
        for row in connection.execute(
            "SELECT relative_path, byte_size, digest, read_outcome FROM content_identities "
            "WHERE run_id = ?",
            (run_id,),
        )
    }
    expected_by_root: dict[str, dict[str, sqlite3.Row]] = {root: {} for root in roots}
    for row in expected:
        path = str(row["relative_path"])
        for root in roots:
            if _relative_to(root, path) is not None or path == root:
                expected_by_root[root][path] = row
    for root, expected_paths in expected_by_root.items():
        base = selected_root if root == "." else selected_root / root
        live_paths = _live_descendant_paths(base, selected_root)
        if set(expected_paths) != live_paths:
            changed = min(set(expected_paths) ^ live_paths)
            raise RuntimeError(
                f"Structural Snapshot Revalidation failed: added, removed, or renamed evidence at {changed}"
            )
    expected_paths = {
        path: row
        for by_root in expected_by_root.values()
        for path, row in by_root.items()
    }
    for path, row in expected_paths.items():
        target = selected_root if path == "." else selected_root / path
        try:
            metadata = target.lstat()
        except OSError as error:
            raise RuntimeError(
                f"Structural Snapshot Revalidation failed: missing or unreadable {path}"
            ) from error
        kind = _entry_kind(metadata)
        if kind != str(row["entry_kind"]) or metadata.st_mtime_ns != row["modified_ns"]:
            raise RuntimeError(
                f"Structural Snapshot Revalidation failed: changed evidence at {path}"
            )
        if kind != "regular-file":
            continue
        identity = hash_regular_file(
            target,
            path,
            FileObservation.persisted(
                int(row["observed_byte_size"]), row["modified_ns"]
            ),
        )
        if identity.read_outcome != str(row["read_outcome"]):
            raise RuntimeError(
                f"Structural Snapshot Revalidation failed: changed read outcome at {path}"
            )
        recorded_identity = identities.get(path)
        if (
            recorded_identity is not None
            and str(recorded_identity["read_outcome"]) == READ_OUTCOME_SUCCESSFUL
            and (
                identity.read_outcome != READ_OUTCOME_SUCCESSFUL
                or identity.byte_size != int(recorded_identity["byte_size"])
                or identity.digest != recorded_identity["digest"]
            )
        ):
            raise RuntimeError(
                f"Structural Snapshot Revalidation failed: changed content identity at {path}"
            )


def _entry_kind(metadata: os.stat_result) -> str:
    if stat.S_ISDIR(metadata.st_mode):
        return "directory"
    if stat.S_ISREG(metadata.st_mode):
        return "regular-file"
    if stat.S_ISLNK(metadata.st_mode):
        return "symbolic-link"
    return "special-entry"


def _live_descendant_paths(base: Path, selected_root: Path) -> set[str]:
    """Return the complete, non-following live subtree path set."""
    try:
        base.lstat()
    except OSError as error:
        relative = (
            base.relative_to(selected_root).as_posix() if base != selected_root else "."
        )
        raise RuntimeError(
            f"Structural Snapshot Revalidation failed: missing or unreadable {relative}"
        ) from error
    paths = {base.relative_to(selected_root).as_posix() or "."}

    def refuse_walk_error(error: OSError) -> None:
        raise RuntimeError(
            f"Structural Snapshot Revalidation failed: missing or unreadable {error.filename}"
        ) from error

    for directory, directories, filenames in os.walk(
        base, followlinks=False, onerror=refuse_walk_error
    ):
        for name in [*directories, *filenames]:
            paths.add((Path(directory) / name).relative_to(selected_root).as_posix())
    return paths
