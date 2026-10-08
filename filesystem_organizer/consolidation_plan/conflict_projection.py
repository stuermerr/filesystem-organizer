from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass

from ..exact_duplicate_groups import READ_OUTCOME_SUCCESSFUL
from .models import ConflictSourceMapping, PlanOutputEntry
from .paths import is_within


@dataclass(frozen=True)
class _CopyableRecord:
    source: str
    relative: str
    entry_kind: str
    algorithm: str | None
    algorithm_version: int | None
    byte_size: int | None
    digest: str | None
    modified_ns: int | None

    @property
    def identity_key(self) -> tuple[str, int, int, str] | None:
        if self.entry_kind != "file":
            return None
        assert self.algorithm is not None
        assert self.algorithm_version is not None
        assert self.byte_size is not None
        assert self.digest is not None
        return (
            self.algorithm,
            self.algorithm_version,
            self.byte_size,
            self.digest,
        )


@dataclass(frozen=True)
class ConflictProjectionResult:
    entries: tuple[PlanOutputEntry, ...]
    covered_files: frozenset[str]
    mappings: tuple[ConflictSourceMapping, ...]


def _load_records(
    connection: sqlite3.Connection, run_id: str, roots: tuple[str, ...]
) -> list[_CopyableRecord]:
    rows = connection.execute(
        "SELECT inventory.relative_path, inventory.entry_kind, inventory.read_outcome, "
        "inventory.modified_ns, identity.algorithm, identity.algorithm_version, "
        "identity.byte_size, identity.digest, identity.read_outcome AS identity_outcome "
        "FROM inventory_entries AS inventory LEFT JOIN content_identities AS identity "
        "ON identity.run_id = inventory.run_id "
        "AND identity.relative_path = inventory.relative_path "
        "WHERE inventory.run_id = ? ORDER BY inventory.relative_path",
        (run_id,),
    ).fetchall()
    records: list[_CopyableRecord] = []
    for root in roots:
        for row in rows:
            source = str(row["relative_path"])
            if source == root or not is_within(source, root):
                continue
            relative = source if root == "." else source[len(root) + 1 :]
            if (
                row["entry_kind"] == "directory"
                and row["read_outcome"] == READ_OUTCOME_SUCCESSFUL
            ):
                records.append(
                    _CopyableRecord(
                        source, relative, "directory", None, None, None, None, None
                    )
                )
            elif (
                row["entry_kind"] == "regular-file"
                and row["read_outcome"] == READ_OUTCOME_SUCCESSFUL
                and row["identity_outcome"] == READ_OUTCOME_SUCCESSFUL
            ):
                records.append(
                    _CopyableRecord(
                        source,
                        relative,
                        "file",
                        str(row["algorithm"]),
                        int(row["algorithm_version"]),
                        int(row["byte_size"]),
                        str(row["digest"]),
                        None if row["modified_ns"] is None else int(row["modified_ns"]),
                    )
                )
    return records


def component_has_conflict(
    connection: sqlite3.Connection, run_id: str, roots: tuple[str, ...]
) -> bool:
    """Independently validate all corresponding paths in a proposed group."""
    evidence_by_relative: dict[str, set[tuple[object, ...]]] = defaultdict(set)
    for record in _load_records(connection, run_id, roots):
        if record.entry_kind == "directory":
            evidence: tuple[object, ...] = ("directory",)
        else:
            identity_key = record.identity_key
            assert identity_key is not None
            evidence = ("file", *identity_key)
        evidence_by_relative[record.relative].add(evidence)
    return any(len(evidence) > 1 for evidence in evidence_by_relative.values())


def _canonical_source(records: list[_CopyableRecord]) -> _CopyableRecord:
    newest = max(
        (record.modified_ns for record in records if record.modified_ns is not None),
        default=None,
    )
    candidates = (
        [record for record in records if record.modified_ns == newest]
        if newest is not None
        else records
    )
    return min(candidates, key=lambda record: record.source)


def project_lossless_conflicts(
    connection: sqlite3.Connection,
    run_id: str,
    roots: tuple[str, ...],
    canonical_root: str,
    conflict_namespace: str,
) -> ConflictProjectionResult:
    """Apply the latest-primary policy after complete group validation."""
    records = _load_records(connection, run_id, roots)
    kinds_by_relative: dict[str, set[str]] = defaultdict(set)
    for record in records:
        kinds_by_relative[record.relative].add(record.entry_kind)
    type_conflicts = tuple(
        sorted(
            (
                relative
                for relative, kinds in kinds_by_relative.items()
                if len(kinds) > 1
            ),
            key=lambda candidate: (candidate.count("/"), candidate),
        )
    )

    def has_type_conflict(relative: str) -> bool:
        return any(
            relative == conflict or relative.startswith(f"{conflict}/")
            for conflict in type_conflicts
        )

    entries: list[PlanOutputEntry] = []
    mappings: list[ConflictSourceMapping] = []
    covered_files = frozenset(
        record.source for record in records if record.entry_kind == "file"
    )
    conventional_files: dict[str, list[_CopyableRecord]] = defaultdict(list)
    conventional_directories: set[str] = {canonical_root}

    for record in records:
        if has_type_conflict(record.relative):
            output = f"{conflict_namespace}/{record.source}"
            reason = (
                "file-versus-directory conflicting variant; "
                f"source provenance: {record.source}"
            )
            if record.entry_kind == "directory":
                entries.append(
                    PlanOutputEntry(
                        "directory", output, None, None, None, None, None, reason
                    )
                )
            else:
                assert record.identity_key is not None
                entries.append(
                    PlanOutputEntry(
                        "file",
                        output,
                        record.source,
                        record.byte_size,
                        record.algorithm,
                        record.algorithm_version,
                        record.digest,
                        reason,
                    )
                )
            mappings.append(
                ConflictSourceMapping(
                    record.source, output, record.entry_kind, "variant", reason
                )
            )
            continue

        output = (
            f"{canonical_root}/{record.relative}"
            if canonical_root != "."
            else record.relative
        )
        if record.entry_kind == "directory":
            conventional_directories.add(output)
        else:
            conventional_files[output].append(record)

    group_reason = (
        "Lossless Conflict Projection; canonical directory root: " + canonical_root
    )
    entries.extend(
        PlanOutputEntry("directory", path, None, None, None, None, None, group_reason)
        for path in sorted(conventional_directories)
        if path != "."
    )

    for output, output_records in sorted(conventional_files.items()):
        by_identity: dict[tuple[str, int, int, str], list[_CopyableRecord]] = (
            defaultdict(list)
        )
        for record in output_records:
            assert record.identity_key is not None
            by_identity[record.identity_key].append(record)
        if len(by_identity) == 1:
            representative = _canonical_source(output_records)
            entries.append(
                PlanOutputEntry(
                    "file",
                    output,
                    representative.source,
                    representative.byte_size,
                    representative.algorithm,
                    representative.algorithm_version,
                    representative.digest,
                    group_reason,
                )
            )
            continue

        variants = [
            (_canonical_source(identity_records), identity_records)
            for identity_records in by_identity.values()
        ]
        primary: _CopyableRecord | None = None
        if all(record.modified_ns is not None for record in output_records):
            variant_timestamps = {
                representative: representative.modified_ns
                for representative, _records in variants
                if representative.modified_ns is not None
            }
            newest_timestamp = max(variant_timestamps.values())
            newest_variants = [
                representative
                for representative, _records in variants
                if variant_timestamps[representative] == newest_timestamp
            ]
            if len(newest_variants) == 1:
                primary = newest_variants[0]

        for representative, identity_records in sorted(
            variants, key=lambda variant: variant[0].source
        ):
            if representative == primary:
                variant_output = output
                disposition = "primary"
                reason = "uniquely newest modification timestamp"
            else:
                variant_output = f"{conflict_namespace}/{representative.source}"
                disposition = "variant"
                reason = (
                    f"older conflicting variant; source provenance: {representative.source}"
                    if primary is not None
                    else "conflicting variant without unique newest timestamp; "
                    f"source provenance: {representative.source}"
                )
            entries.append(
                PlanOutputEntry(
                    "file",
                    variant_output,
                    representative.source,
                    representative.byte_size,
                    representative.algorithm,
                    representative.algorithm_version,
                    representative.digest,
                    reason,
                )
            )
            for record in sorted(
                identity_records, key=lambda candidate: candidate.source
            ):
                mapping_disposition = (
                    disposition if record == representative else "collapsed"
                )
                mapping_reason = (
                    reason
                    if record == representative
                    else "identical full content identity collapsed to retained variant; "
                    f"source provenance: {record.source}"
                )
                mappings.append(
                    ConflictSourceMapping(
                        record.source,
                        variant_output,
                        "file",
                        mapping_disposition,
                        mapping_reason,
                    )
                )

    entries.sort(key=lambda entry: (entry.output_relative_path, entry.entry_kind))
    mappings.sort(
        key=lambda mapping: (mapping.output_relative_path, mapping.source_relative_path)
    )
    return ConflictProjectionResult(tuple(entries), covered_files, tuple(mappings))
