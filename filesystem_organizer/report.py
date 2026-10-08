from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from tempfile import TemporaryFile
from typing import TextIO

from .analysis_run import (
    DIRECTORY_EVIDENCE_SCHEMA_VERSION,
    DIRECTORY_EVIDENCE_VERSION,
    STRUCTURAL_RELATIONSHIPS_SCHEMA_VERSION,
    AnalysisRunError,
)
from .content_identity import select_identity_rows
from .exact_duplicate_groups import (
    READ_OUTCOME_SUCCESSFUL,
    derive_exact_duplicate_groups,
)
from .run_workspace import (
    SCHEMA_VERSION,
    RunWorkspaceError,
    open_read_only,
    resolve_run_directory,
)

SUMMARY_SAMPLE_LIMIT = 20
DEFAULT_PAGE_LIMIT = 100


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


def markdown_path(relative_path: str) -> str:
    escaped = relative_path.replace("\\", "\\\\").replace("\n", "\\n")
    return escaped.replace("|", "\\|").replace("`", "&#96;")


def _timestamp(modified_ns: int | None) -> str:
    if modified_ns is None:
        return ""
    return datetime.fromtimestamp(modified_ns / 1_000_000_000, UTC).isoformat()


class StreamingDocument:
    """A line-oriented report assembled on disk rather than in one memory buffer."""

    def __init__(self) -> None:
        self._file = TemporaryFile(mode="w+t", encoding="utf-8")  # noqa: SIM115

    def append(self, line: str) -> None:
        self._file.write(line + "\n")

    def extend(self, lines: Iterable[str]) -> None:
        for line in lines:
            self.append(str(line))

    def render(self) -> str:
        self._file.seek(0)
        return self._file.read()

    def write_to(self, output: TextIO) -> None:
        self._file.seek(0)
        while chunk := self._file.read(64 * 1024):
            output.write(chunk)

    def close(self) -> None:
        self._file.close()


@_run_workspace_refusal
def _build_full_report(analysis_run: Path) -> StreamingDocument:
    run_path, database_path = resolve_run_directory(analysis_run)

    connection = open_read_only(database_path)
    try:
        connection.execute("BEGIN")
        runs = connection.execute("SELECT * FROM analysis_runs").fetchall()
        if len(runs) != 1:
            raise AnalysisRunError(
                f"Analysis Run directory must contain exactly one run identity: {run_path}"
            )
        run = runs[0]
        if int(run["schema_version"]) != SCHEMA_VERSION:
            raise AnalysisRunError(
                "Analysis Run uses unsupported clean-cutover evidence schema; "
                f"create a new Analysis Run: {run_path}"
            )
        has_directory_evidence = (
            int(run["schema_version"]) >= DIRECTORY_EVIDENCE_SCHEMA_VERSION
        )
        entries = connection.execute(
            "SELECT relative_path, entry_kind, observed_byte_size, modified_ns, read_outcome "
            "FROM inventory_entries WHERE run_id = ? ORDER BY relative_path",
            (run["run_id"],),
        ).fetchall()
        skipped_total = int(
            connection.execute(
                "SELECT COUNT(*) FROM skipped_entry_findings WHERE run_id = ?",
                (run["run_id"],),
            ).fetchone()[0]
        )
        skipped = connection.execute(
            "SELECT relative_path, reason FROM skipped_entry_findings "
            "WHERE run_id = ? ORDER BY relative_path LIMIT ?",
            (run["run_id"], SUMMARY_SAMPLE_LIMIT),
        ).fetchall()
        if has_directory_evidence:
            directories = connection.execute(
                "SELECT relative_path, entry_kind, is_empty, read_outcome "
                "FROM directory_evidence WHERE run_id = ? ORDER BY relative_path",
                (run["run_id"],),
            ).fetchall()
        else:
            directories = []
        if int(run["schema_version"]) >= STRUCTURAL_RELATIONSHIPS_SCHEMA_VERSION:
            structural = connection.execute(
                "SELECT candidate_count, comparison_count, status FROM structural_analysis "
                "WHERE run_id = ?",
                (run["run_id"],),
            ).fetchone()
            relationships = connection.execute(
                "SELECT left_root, right_root, classification, canonical_root, canonical_reason, "
                "qualifications, unverified_counterparts FROM directory_relationships "
                "WHERE run_id = ? ORDER BY left_root, right_root",
                (run["run_id"],),
            ).fetchall()
            component_rows = connection.execute(
                "SELECT component_id, root_relative_path "
                "FROM actionable_exact_component_members "
                "WHERE run_id = ? ORDER BY component_id, root_relative_path",
                (run["run_id"],),
            ).fetchall()
            components: dict[str, list[str]] = {}
            for component in component_rows:
                components.setdefault(str(component["component_id"]), []).append(
                    str(component["root_relative_path"])
                )
            conflict_total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM relationship_conflicts WHERE run_id = ?",
                    (run["run_id"],),
                ).fetchone()[0]
            )
            conflicts_by_relationship: dict[
                tuple[str, str], list[tuple[str, str, str]]
            ] = {}
            for conflict in connection.execute(
                "SELECT left_root, right_root, descendant_relative_path, left_evidence, "
                "right_evidence FROM relationship_conflicts WHERE run_id = ? "
                "ORDER BY left_root, right_root, descendant_relative_path LIMIT ?",
                (run["run_id"], SUMMARY_SAMPLE_LIMIT),
            ):
                conflicts_by_relationship.setdefault(
                    (str(conflict["left_root"]), str(conflict["right_root"])), []
                ).append(
                    (
                        str(conflict["descendant_relative_path"]),
                        str(conflict["left_evidence"]),
                        str(conflict["right_evidence"]),
                    )
                )
        else:
            structural = None
            relationships = []
            components = {}
            conflict_total = 0
            conflicts_by_relationship = {}
        identities = select_identity_rows(connection, str(run["run_id"]))
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()

    successful_count = sum(
        entry["entry_kind"] == "regular-file"
        and entry["read_outcome"] == READ_OUTCOME_SUCCESSFUL
        for entry in entries
    )
    lines = StreamingDocument()
    lines.extend(
        [
            "# Analysis Run Report",
            "",
            f"**Run ID:** `{run['run_id']}`  ",
            f"**Snapshot ID:** `{run['snapshot_id']}`  ",
            f"**Selected Backup Root:** `{markdown_path(run['selected_backup_root'])}`  ",
            f"**Status:** {run['status']}  ",
            (
                f"**Structural evidence version:** {DIRECTORY_EVIDENCE_VERSION}  "
                if has_directory_evidence
                else "**Structural evidence version:** not recorded (pre-M2 Analysis Run)  "
            ),
            (
                f"**Directories:** {len(directories)}  "
                if has_directory_evidence
                else "**Directories:** not recorded  "
            ),
            (
                f"**Empty directories:** {sum(entry['is_empty'] == 1 for entry in directories)}  "
                if has_directory_evidence
                else "**Empty directories:** not recorded  "
            ),
            f"**Readable regular files:** {successful_count}  ",
            f"**Skipped entries:** {skipped_total}",
            (
                f"**Structural candidates:** {structural['candidate_count']}  "
                if structural is not None
                else "**Structural candidates:** not recorded  "
            ),
            (
                f"**Completed structural comparisons:** {structural['comparison_count']}"
                if structural is not None
                else "**Completed structural comparisons:** not recorded"
            ),
            "",
            "## Directory Tree Evidence",
            "",
        ]
    )
    if has_directory_evidence:
        lines.extend(
            [
                "| Relative path | Kind | Empty | Read outcome |",
                "| --- | --- | --- | --- |",
            ]
        )
        lines.extend(
            f"| `{markdown_path(entry['relative_path'])}` | {entry['entry_kind']} | "
            f"{'yes' if entry['is_empty'] == 1 else 'no' if entry['is_empty'] == 0 else ''} | "
            f"{entry['read_outcome']} |"
            for entry in directories
        )
    else:
        lines.append("Not recorded for this pre-M2 Analysis Run.")
    lines.extend(
        [
            "",
            "## Persisted Inventory Evidence",
            "",
            "| Relative path | Kind | Bytes | Modified (UTC) | Read outcome |",
            "| --- | --- | ---: | --- | --- |",
        ]
    )
    for entry in entries:
        size = (
            ""
            if entry["observed_byte_size"] is None
            else str(entry["observed_byte_size"])
        )
        lines.append(
            f"| `{markdown_path(entry['relative_path'])}` | {entry['entry_kind']} | {size} | "
            f"{_timestamp(entry['modified_ns'])} | {entry['read_outcome']} |"
        )
    lines.extend(["", "## Skipped Entry Findings", ""])
    if skipped:
        lines.extend(["| Relative path | Reason |", "| --- | --- |"])
        lines.extend(
            f"| `{markdown_path(finding['relative_path'])}` | {finding['reason']} |"
            for finding in skipped
        )
    else:
        lines.append("None.")
    if skipped_total > len(skipped):
        lines.append("")
        lines.append(
            f"Showing the first {len(skipped)} of {skipped_total}. Complete evidence: "
            "`--section skipped --offset N --limit N`."
        )

    duplicate_groups = [
        group
        for group in derive_exact_duplicate_groups(identities)
        if len(group.member_relative_paths) > 1
    ]

    lines.extend(["", "## Exact Duplicate Groups", ""])
    if duplicate_groups:
        for group in duplicate_groups:
            lines.append(
                f"### `{markdown_path(group.key.digest)}` "
                f"({group.key.algorithm} v{group.key.algorithm_version}, "
                f"{group.key.byte_size} bytes)"
            )
            lines.append("")
            lines.append(
                f"**Canonical Copy:** `{markdown_path(group.canonical_relative_path)}`  "
            )
            lines.append(f"**Reason:** {group.canonical_reason}")
            lines.append("")
            lines.append("| Relative path | Bytes | Digest | Canonical |")
            lines.append("| --- | ---: | --- | --- |")
            for member_relative_path in group.member_relative_paths:
                is_canonical = (
                    "yes"
                    if member_relative_path == group.canonical_relative_path
                    else ""
                )
                lines.append(
                    f"| `{markdown_path(member_relative_path)}` | "
                    f"{group.key.byte_size} | `{group.key.digest}` | {is_canonical} |"
                )
            lines.append("")
    else:
        lines.append("None.")
        lines.append("")

    exact_components = [roots for roots in components.values() if len(roots) > 1]
    lines.extend(["## Exact Directory Identity Components", ""])
    if exact_components:
        for roots in sorted(exact_components, key=lambda roots: tuple(roots)):
            canonical = min(roots, key=lambda path: (path.count("/"), path))
            lines.extend(
                [
                    "### Exact Directory Identity Component",
                    "",
                    "**Classification:** exact-identity component  ",
                    "**Member roots:** "
                    + ", ".join(f"`{markdown_path(root)}`" for root in roots)
                    + "  ",
                    f"**Canonical Directory Root:** `{markdown_path(canonical)}`  ",
                    "**Reason:** shallowest relative path, then lexical relative-path order",
                    "",
                ]
            )
    else:
        lines.extend(["None.", ""])

    lines.extend(["## Directory Relationship Graph", ""])
    if conflict_total > SUMMARY_SAMPLE_LIMIT:
        lines.extend(
            [
                (
                    f"Structural conflict details show the first {SUMMARY_SAMPLE_LIMIT} of "
                    f"{conflict_total}. Complete evidence: "
                    "`--section conflicts --offset N --limit N`."
                ),
                "",
            ]
        )
    if relationships:
        for relationship in relationships:
            lines.extend(
                [
                    (
                        f"### `{markdown_path(relationship['left_root'])}` ↔ "
                        f"`{markdown_path(relationship['right_root'])}`"
                    ),
                    "",
                    f"**Classification:** {relationship['classification']}  ",
                    f"**Canonical Directory Root:** `{markdown_path(relationship['canonical_root'])}`  ",
                    f"**Reason:** {relationship['canonical_reason']}",
                ]
            )
            qualifications = str(relationship["qualifications"]).splitlines()
            unverified = str(relationship["unverified_counterparts"]).splitlines()
            if qualifications:
                lines.append(
                    "**Evidence Qualifications:** "
                    + ", ".join(f"`{markdown_path(path)}`" for path in qualifications)
                )
            if unverified:
                lines.append(
                    "**Unverified Counterparts:** "
                    + ", ".join(f"`{markdown_path(path)}`" for path in unverified)
                )
            conflicts = conflicts_by_relationship.get(
                (str(relationship["left_root"]), str(relationship["right_root"])), []
            )
            if conflicts:
                lines.append("**Structural Conflicts:**")
                lines.extend(
                    f"- `{markdown_path(path)}`: `{markdown_path(left_evidence)}` ↔ "
                    f"`{markdown_path(right_evidence)}`"
                    for path, left_evidence, right_evidence in conflicts
                )
            lines.append("")
    else:
        lines.extend(["None.", ""])

    unproven_identities = [
        row for row in identities if row.read_outcome != READ_OUTCOME_SUCCESSFUL
    ]
    lines.extend(["## Unproven Content Identities", ""])
    if unproven_identities:
        lines.append(
            "Size-collision candidates that could not be streamed into a proven "
            "content identity; none contribute to an Exact Duplicate group."
        )
        lines.append("")
        lines.append("| Relative path | Bytes | Read outcome |")
        lines.append("| --- | ---: | --- |")
        lines.extend(
            f"| `{markdown_path(identity.relative_path)}` | "
            f"{identity.byte_size} | {identity.read_outcome} |"
            for identity in unproven_identities
        )
    else:
        lines.append("None.")
    return lines


def render_full_report(analysis_run: Path) -> str:
    document = _build_full_report(analysis_run)
    try:
        return document.render()
    finally:
        document.close()


def write_full_report(analysis_run: Path, output: TextIO) -> None:
    document = _build_full_report(analysis_run)
    try:
        document.write_to(output)
    finally:
        document.close()


@_run_workspace_refusal
def render_report(
    analysis_run: Path,
    *,
    detail: str = "summary",
    section: str | None = None,
    offset: int = 0,
    limit: int | None = None,
) -> str:
    """Render operational facts or one explicitly paged evidence section.

    Skipped findings and structural conflicts remain sampled in every detail
    mode; their complete evidence is available only through section paging.
    """

    if detail == "full":
        if section is not None or offset or limit is not None:
            raise AnalysisRunError(
                "--detail full cannot be combined with section paging"
            )
        return render_full_report(analysis_run)
    if detail != "summary":
        raise AnalysisRunError(f"Unknown report detail mode: {detail}")
    if offset < 0 or limit is not None and limit < 1:
        raise AnalysisRunError(
            "Report offset must be non-negative and limit must be positive"
        )

    run_path, database_path = resolve_run_directory(analysis_run)
    connection = open_read_only(database_path)
    try:
        connection.execute("BEGIN")
        runs = connection.execute("SELECT * FROM analysis_runs").fetchall()
        if len(runs) != 1:
            raise AnalysisRunError(
                f"Analysis Run directory must contain exactly one run identity: {run_path}"
            )
        run = runs[0]
        if int(run["schema_version"]) != SCHEMA_VERSION:
            raise AnalysisRunError(
                "Analysis Run uses unsupported clean-cutover evidence schema; "
                f"create a new Analysis Run: {run_path}"
            )
        run_id = str(run["run_id"])
        if section == "inventory":
            page_limit = limit if limit is not None else DEFAULT_PAGE_LIMIT
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM inventory_entries WHERE run_id = ?",
                    (run_id,),
                ).fetchone()[0]
            )
            entries = connection.execute(
                "SELECT relative_path, entry_kind, observed_byte_size, modified_ns, read_outcome "
                "FROM inventory_entries WHERE run_id = ? ORDER BY relative_path LIMIT ? OFFSET ?",
                (run_id, page_limit, offset),
            ).fetchall()
            lines = [
                "# Analysis Run Inventory Evidence",
                "",
                f"**Total:** {total}  ",
                f"**Offset:** {offset}  ",
                f"**Limit:** {page_limit}",
                "",
                "| Relative path | Kind | Bytes | Modified (UTC) | Read outcome |",
                "| --- | --- | ---: | --- | --- |",
            ]
            lines.extend(
                f"| `{markdown_path(row['relative_path'])}` | {row['entry_kind']} | "
                f"{row['observed_byte_size'] if row['observed_byte_size'] is not None else ''} | "
                f"{_timestamp(row['modified_ns'])} | {row['read_outcome']} |"
                for row in entries
            )
            return "\n".join(lines) + "\n"
        if section == "skipped":
            page_limit = limit if limit is not None else DEFAULT_PAGE_LIMIT
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM skipped_entry_findings WHERE run_id = ?",
                    (run_id,),
                ).fetchone()[0]
            )
            findings = connection.execute(
                "SELECT relative_path, reason FROM skipped_entry_findings "
                "WHERE run_id = ? ORDER BY relative_path LIMIT ? OFFSET ?",
                (run_id, page_limit, offset),
            ).fetchall()
            lines = [
                "# Analysis Run Skipped Entry Findings",
                "",
                f"**Total:** {total}  ",
                f"**Offset:** {offset}  ",
                f"**Limit:** {page_limit}",
                "",
                "| Relative path | Reason |",
                "| --- | --- |",
            ]
            lines.extend(
                f"| `{markdown_path(row['relative_path'])}` | {row['reason']} |"
                for row in findings
            )
            return "\n".join(lines) + "\n"
        if section == "conflicts":
            page_limit = limit if limit is not None else DEFAULT_PAGE_LIMIT
            conflict_from = (
                " FROM directory_relationships AS relationship "
                "JOIN relationship_conflicts AS conflict ON conflict.run_id = relationship.run_id "
                "AND conflict.left_root = relationship.left_root "
                "AND conflict.right_root = relationship.right_root "
                "WHERE relationship.run_id = ? AND relationship.classification = 'conflicting'"
            )
            total = int(
                connection.execute("SELECT COUNT(*)" + conflict_from, (run_id,)).fetchone()[0]
            )
            conflicts = connection.execute(
                "SELECT relationship.left_root, relationship.right_root, "
                "conflict.descendant_relative_path, conflict.left_evidence, "
                "conflict.right_evidence"
                + conflict_from
                + " ORDER BY relationship.left_root, relationship.right_root, "
                "conflict.descendant_relative_path LIMIT ? OFFSET ?",
                (run_id, page_limit, offset),
            ).fetchall()
            lines = [
                "# Analysis Run Structural Conflicts",
                "",
                f"**Total:** {total}  ",
                f"**Offset:** {offset}  ",
                f"**Limit:** {page_limit}",
                "",
            ]
            lines.extend(
                f"- `{markdown_path(row['left_root'])}` ↔ "
                f"`{markdown_path(row['right_root'])}`: "
                f"`{markdown_path(row['descendant_relative_path'])}` "
                f"(left: {row['left_evidence']}; right: {row['right_evidence']})"
                for row in conflicts
            )
            if not conflicts:
                lines.append("None.")
            return "\n".join(lines) + "\n"
        if section is not None:
            raise AnalysisRunError(f"Unknown report section: {section}")

        counts = connection.execute(
            "SELECT "
            "COUNT(*) AS entries, "
            "SUM(entry_kind = 'regular-file' AND read_outcome = ?) AS readable_files "
            "FROM inventory_entries WHERE run_id = ?",
            (READ_OUTCOME_SUCCESSFUL, run_id),
        ).fetchone()
        skipped_total = int(
            connection.execute(
                "SELECT COUNT(*) FROM skipped_entry_findings WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
        )
        skipped = connection.execute(
            "SELECT relative_path, reason FROM skipped_entry_findings "
            "WHERE run_id = ? ORDER BY relative_path LIMIT ?",
            (run_id, SUMMARY_SAMPLE_LIMIT),
        ).fetchall()
        structural = connection.execute(
            "SELECT candidate_count, comparison_count FROM structural_analysis WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        conflict_from = (
            " FROM directory_relationships AS relationship "
            "JOIN relationship_conflicts AS conflict ON conflict.run_id = relationship.run_id "
            "AND conflict.left_root = relationship.left_root "
            "AND conflict.right_root = relationship.right_root "
            "WHERE relationship.run_id = ? AND relationship.classification = 'conflicting'"
        )
        conflict_total = int(
            connection.execute("SELECT COUNT(*)" + conflict_from, (run_id,)).fetchone()[0]
        )
        conflicts = connection.execute(
            "SELECT relationship.left_root, relationship.right_root, "
            "conflict.descendant_relative_path"
            + conflict_from
            + " ORDER BY relationship.left_root, relationship.right_root, "
            "conflict.descendant_relative_path LIMIT ?",
            (run_id, SUMMARY_SAMPLE_LIMIT),
        ).fetchall()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()

    lines = [
        "# Analysis Run Report",
        "",
        f"**Run ID:** `{run['run_id']}`  ",
        f"**Snapshot ID:** `{run['snapshot_id']}`  ",
        f"**Selected Backup Root:** `{markdown_path(run['selected_backup_root'])}`  ",
        f"**Status:** {run['status']}  ",
        f"**Inventory entries:** {counts['entries']}  ",
        f"**Readable regular files:** {counts['readable_files'] or 0}  ",
        f"**Skipped entries:** {skipped_total}  ",
        f"**Structural candidates:** {structural['candidate_count'] if structural else 0}  ",
        f"**Completed structural comparisons:** {structural['comparison_count'] if structural else 0}",
        "",
        "## Skipped Entry Findings",
        "",
    ]
    if skipped:
        lines.extend(["| Relative path | Reason |", "| --- | --- |"])
        lines.extend(
            f"| `{markdown_path(row['relative_path'])}` | {row['reason']} |"
            for row in skipped
        )
    else:
        lines.append("None.")
    if skipped_total > len(skipped):
        lines.append(
            f"Showing the first {len(skipped)} of {skipped_total}. Complete evidence: "
            "`--section skipped --offset N --limit N`."
        )
    lines.extend(["", "## Unresolved Structural Conflicts", ""])
    if conflicts:
        lines.extend(
            f"- `{markdown_path(row['left_root'])}` ↔ `{markdown_path(row['right_root'])}`: "
            f"`{markdown_path(row['descendant_relative_path'])}`"
            for row in conflicts
        )
    else:
        lines.append("None.")
    if conflict_total > len(conflicts):
        lines.append(
            f"Showing the first {len(conflicts)} of {conflict_total}. Complete evidence: "
            "`--section conflicts --offset N --limit N`."
        )
    lines.extend(
        [
            "",
            (
                "Full audit evidence: rerun with `--detail full`. Complete high-cardinality "
                "evidence: `--section inventory|skipped|conflicts --offset N --limit N`."
            ),
        ]
    )
    return "\n".join(lines) + "\n"
