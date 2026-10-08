from __future__ import annotations

import sqlite3
from bisect import bisect_left
from pathlib import Path
from typing import TextIO

from ..exact_duplicate_groups import READ_OUTCOME_SUCCESSFUL
from ..report import (
    DEFAULT_PAGE_LIMIT,
    SUMMARY_SAMPLE_LIMIT,
    StreamingDocument,
    markdown_path,
)
from ..run_workspace import open_read_only, resolve_run_directory, select_plan_row
from .models import (
    ConsolidationPlanError,
    LosslessConflictProjection,
    StructuralUnion,
    run_workspace_refusal,
)
from .paths import is_within
from .projection import _rooted_unverified_counterpart


def _union_roots_for_path(
    path: str, union_roots: dict[str, tuple[str, ...]]
) -> tuple[str, ...] | None:
    for candidate in (Path(path), *Path(path).parents):
        roots = union_roots.get(candidate.as_posix())
        if roots is not None:
            return roots
    return None


@run_workspace_refusal
def _build_full_plan_report(
    analysis_run: Path, plan_id: str | None = None
) -> StreamingDocument:
    _, database_path = resolve_run_directory(analysis_run)

    connection = open_read_only(database_path)
    try:
        connection.execute("BEGIN")
        plan = select_plan_row(connection, plan_id)
        operations = connection.execute(
            "SELECT * FROM plan_operations WHERE plan_id = ? ORDER BY operation_index",
            (plan["plan_id"],),
        ).fetchall()
        overrides = connection.execute(
            "SELECT * FROM plan_overrides WHERE plan_id = ? ORDER BY override_index",
            (plan["plan_id"],),
        ).fetchall()
        if plan["status"] == "finalized":
            output_entries = connection.execute(
                "SELECT * FROM final_plan_entries WHERE plan_id = ? ORDER BY entry_index",
                (plan["plan_id"],),
            ).fetchall()
        else:
            output_entries = connection.execute(
                "SELECT row_number() OVER (ORDER BY active.output_relative_path)-1 AS entry_index, "
                "active.entry_kind, selection.source_relative_path, active.output_relative_path, "
                "baseline.expected_byte_size, baseline.algorithm, baseline.algorithm_version, "
                "baseline.digest, COALESCE(selection.reason, baseline.reason) AS reason "
                "FROM plan_layout_active_entries AS active JOIN plan_baseline_entries AS baseline "
                "ON baseline.plan_id=active.plan_id AND baseline.entry_id=active.entry_id "
                "LEFT JOIN plan_source_selections AS selection ON selection.plan_id=active.plan_id "
                "AND selection.entry_id=active.entry_id WHERE active.plan_id=? AND active.disposition='place' "
                "UNION ALL SELECT 1000000000 + row_number() OVER (ORDER BY output_relative_path), "
                "'directory', NULL, output_relative_path, NULL, NULL, NULL, NULL, 'Layout-Owned Directory' "
                "FROM plan_layout_active_directories WHERE plan_id=? ORDER BY entry_index",
                (plan["plan_id"], plan["plan_id"]),
            ).fetchall()
        try:
            conflict_projection_total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM plan_conflict_projections WHERE plan_id = ?",
                    (plan["plan_id"],),
                ).fetchone()[0]
            )
            conflict_projections = connection.execute(
                "SELECT group_index, source_relative_path, output_relative_path, "
                "disposition, reason FROM plan_conflict_projections "
                "WHERE plan_id = ? ORDER BY group_index, conflict_index LIMIT ?",
                (plan["plan_id"], SUMMARY_SAMPLE_LIMIT),
            ).fetchall()
        except sqlite3.OperationalError:
            conflict_projection_total = 0
            conflict_projections = []
        try:
            skipped_total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM skipped_entry_findings WHERE run_id = ?",
                    (plan["run_id"],),
                ).fetchone()[0]
            )
            skipped = connection.execute(
                "SELECT relative_path, reason FROM skipped_entry_findings "
                "WHERE run_id = ? ORDER BY relative_path LIMIT ?",
                (plan["run_id"], SUMMARY_SAMPLE_LIMIT),
            ).fetchall()
            # Large finding sets are listed only once in the bounded summary.
            # Small reports retain their contextual omission details.
            remaining_omitted = SUMMARY_SAMPLE_LIMIT if skipped_total <= SUMMARY_SAMPLE_LIMIT else 0
            projection_omitted_totals: dict[tuple[str, ...], int] = {}
            projection_evidence_by_roots: dict[
                tuple[str, ...], tuple[list[str], list[str]]
            ] = {}
            projection_omitted_by_roots: dict[tuple[str, ...], list[sqlite3.Row]] = {}
            projection_rows = connection.execute(
                "SELECT * FROM plan_projection_unions WHERE plan_id = ? ORDER BY union_index",
                (plan["plan_id"],),
            ).fetchall()
            roots_by_index: dict[int, list[str]] = {}
            for root in connection.execute(
                "SELECT union_index, root_relative_path FROM plan_projection_union_roots "
                "WHERE plan_id = ? ORDER BY union_index, root_relative_path",
                (plan["plan_id"],),
            ):
                roots_by_index.setdefault(int(root["union_index"]), []).append(
                    str(root["root_relative_path"])
                )
            structural_unions = []
            for projection in projection_rows:
                roots = tuple(roots_by_index[int(projection["union_index"])])
                structural_unions.append(
                    StructuralUnion(
                        roots,
                        str(projection["canonical_root"]),
                        str(projection["classification"]),
                        str(projection["canonical_reason"]),
                    )
                )
                projection_evidence_by_roots[roots] = (
                    str(projection["qualifications"]).splitlines(),
                    str(projection["unverified_counterparts"]).splitlines(),
                )
                projection_omitted_totals[roots] = int(connection.execute(
                    "SELECT COUNT(*) FROM plan_projection_omitted_entries "
                    "WHERE plan_id = ? AND union_index = ?",
                    (plan["plan_id"], projection["union_index"]),
                ).fetchone()[0])
                projection_omitted_by_roots[roots] = connection.execute(
                    "SELECT relative_path, reason FROM plan_projection_omitted_entries "
                    "WHERE plan_id = ? AND union_index = ? ORDER BY relative_path LIMIT ?",
                    (plan["plan_id"], projection["union_index"], remaining_omitted),
                ).fetchall()
                remaining_omitted -= len(projection_omitted_by_roots[roots])

            conflict_groups: dict[int, LosslessConflictProjection] = {}
            conflict_omitted_by_index: dict[int, list[sqlite3.Row]] = {}
            conflict_omitted_totals: dict[int, int] = {}
            for group_index in sorted({int(row["group_index"]) for row in conflict_projections}):
                group = connection.execute(
                    "SELECT * FROM plan_projection_conflict_groups "
                    "WHERE plan_id = ? AND group_index = ?",
                    (plan["plan_id"], group_index),
                ).fetchone()
                conflict_roots = connection.execute(
                    "SELECT root_relative_path FROM plan_projection_conflict_group_roots "
                    "WHERE plan_id = ? AND group_index = ? ORDER BY root_relative_path",
                    (plan["plan_id"], group_index),
                )
                conflict_groups[group_index] = LosslessConflictProjection(
                    tuple(str(root["root_relative_path"]) for root in conflict_roots),
                    str(group["canonical_root"]),
                    str(group["canonical_reason"]),
                )
                conflict_omitted_totals[group_index] = int(connection.execute(
                    "SELECT COUNT(*) FROM plan_projection_conflict_omitted_entries "
                    "WHERE plan_id = ? AND group_index = ?",
                    (plan["plan_id"], group_index),
                ).fetchone()[0])
                omitted = connection.execute(
                    "SELECT relative_path, reason "
                    "FROM plan_projection_conflict_omitted_entries "
                    "WHERE plan_id = ? AND group_index = ? ORDER BY relative_path LIMIT ?",
                    (plan["plan_id"], group_index, remaining_omitted),
                ).fetchall()
                conflict_omitted_by_index[group_index] = omitted
                remaining_omitted -= len(omitted)
            inventory = connection.execute(
                "SELECT inventory.relative_path, inventory.entry_kind, inventory.read_outcome, "
                "skipped.reason FROM inventory_entries AS inventory "
                "LEFT JOIN skipped_entry_findings AS skipped ON skipped.run_id=inventory.run_id "
                "AND skipped.relative_path=inventory.relative_path "
                "WHERE inventory.run_id = ? AND inventory.read_outcome != ? "
                "ORDER BY inventory.relative_path LIMIT ?",
                (plan["run_id"], READ_OUTCOME_SUCCESSFUL, remaining_omitted),
            ).fetchall()
            empty_directory_paths = {
                str(row["relative_path"])
                for row in connection.execute(
                    "SELECT relative_path FROM directory_evidence "
                    "WHERE run_id = ? AND is_empty = 1 AND read_outcome = ?",
                    (plan["run_id"], READ_OUTCOME_SUCCESSFUL),
                )
            }
            relationship_evidence_by_roots = {
                (str(row["left_root"]), str(row["right_root"])): (
                    str(row["qualifications"]).splitlines(),
                    str(row["unverified_counterparts"]).splitlines(),
                )
                for row in connection.execute(
                    "SELECT left_root, right_root, qualifications, unverified_counterparts "
                    "FROM directory_relationships WHERE run_id = ?",
                    (plan["run_id"],),
                )
            }
            conflict_from = (
                " FROM directory_relationships AS relationship "
                "JOIN relationship_conflicts AS conflict ON "
                "conflict.run_id = relationship.run_id "
                "AND conflict.left_root = relationship.left_root "
                "AND conflict.right_root = relationship.right_root "
                "WHERE relationship.run_id = ? AND relationship.classification = 'conflicting'"
            )
            structural_conflict_total = int(
                connection.execute(
                    "SELECT COUNT(*)" + conflict_from, (plan["run_id"],)
                ).fetchone()[0]
            )
            conflicts = connection.execute(
                "SELECT relationship.left_root, relationship.right_root, "
                "conflict.descendant_relative_path"
                + conflict_from
                + " ORDER BY relationship.left_root, relationship.right_root, "
                "conflict.descendant_relative_path LIMIT ?",
                (plan["run_id"], SUMMARY_SAMPLE_LIMIT),
            ).fetchall()
        except sqlite3.OperationalError:
            structural_unions = []
            conflict_groups = {}
            conflict_omitted_by_index = {}
            projection_evidence_by_roots = {}
            projection_omitted_by_roots = {}
            skipped_total = 0
            skipped = []
            inventory = []
            empty_directory_paths = set()
            relationship_evidence_by_roots = {}
            structural_conflict_total = 0
            conflicts = []
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()

    executable_operations = (
        [entry for entry in output_entries if entry["entry_kind"] == "file"]
        if output_entries
        else operations
    )
    union_roots = {
        root: union.roots for union in structural_unions for root in union.roots
    }
    entries_by_union: dict[tuple[str, ...], list[sqlite3.Row]] = {
        union.roots: [] for union in structural_unions
    }
    for entry in output_entries:
        source = entry["source_relative_path"]
        if source is None:
            continue
        matched_roots = _union_roots_for_path(str(source), union_roots)
        if matched_roots is not None:
            entries_by_union[matched_roots].append(entry)
    empty_directories_by_union: dict[tuple[str, ...], list[str]] = {
        union.roots: [] for union in structural_unions
    }
    for source in empty_directory_paths:
        matched_roots = _union_roots_for_path(source, union_roots)
        if matched_roots is None:
            continue
        canonical = next(
            union.canonical_root
            for union in structural_unions
            if union.roots == matched_roots
        )
        root = next(root for root in matched_roots if is_within(source, root))
        if source == root:
            continue
        relative = source[len(root) + 1 :] if root != "." else source
        empty_directories_by_union[matched_roots].append(
            f"{canonical}/{relative}" if canonical != "." else relative
        )
    ordered_output_paths = sorted(
        str(entry["output_relative_path"]) for entry in output_entries
    )
    inventory_by_path = {str(row["relative_path"]): row for row in inventory}
    ordered_inventory_paths = sorted(inventory_by_path)

    def has_output_at_or_below(path: str) -> bool:
        index = bisect_left(ordered_output_paths, path)
        return index < len(ordered_output_paths) and (
            ordered_output_paths[index] == path
            or ordered_output_paths[index].startswith(f"{path}/")
        )

    def unprocessable_entries_under(root: str) -> list[sqlite3.Row]:
        prefix = "" if root == "." else f"{root}/"
        index = 0 if root == "." else bisect_left(ordered_inventory_paths, prefix)
        entries: list[sqlite3.Row] = []
        while index < len(ordered_inventory_paths):
            path = ordered_inventory_paths[index]
            if prefix and not path.startswith(prefix):
                break
            row = inventory_by_path[path]
            if path != root and row["read_outcome"] != READ_OUTCOME_SUCCESSFUL:
                entries.append(row)
            index += 1
        return entries

    lines = StreamingDocument()
    lines.extend(
        [
            "# Consolidation Plan Report",
            "",
            f"**Plan ID:** `{plan['plan_id']}`  ",
            f"**Run ID:** `{plan['run_id']}`  ",
            f"**Snapshot ID:** `{plan['snapshot_id']}`  ",
            f"**Schema Version:** {plan['schema_version']}  ",
            "**Plan Projection:** persisted  ",
            f"**Status:** {plan['status']}  ",
            f"**Intended Destination:** `{markdown_path(plan['intended_destination'])}`  ",
            f"**Operation Count:** {sum(entry['entry_kind'] == 'file' for entry in output_entries) if output_entries else len(operations)}",
            f"**Explicit directory count:** {sum(entry['entry_kind'] == 'directory' for entry in output_entries)}",
            "",
            "## Skipped Entry Findings",
            "",
        ]
    )
    if skipped:
        lines.extend(
            f"- `{markdown_path(row['relative_path'])}`: {row['reason']}"
            for row in skipped
        )
    else:
        lines.append("None.")
    if skipped_total > len(skipped):
        lines.append(
            f"Showing the first {len(skipped)} of {skipped_total}. Complete evidence: "
            "`--section skipped --offset N --limit N`."
        )
    lines.extend(
        [
            "",
            "## Consolidation Plan Operations",
            "",
            "| Source relative path | Output relative path | Bytes | Digest | Canonical reason |",
            "| --- | --- | ---: | --- | --- |",
        ]
    )
    for operation in executable_operations:
        canonical_reason = (
            operation["reason"] if output_entries else operation["canonical_reason"]
        ) or ""
        lines.append(
            f"| `{markdown_path(operation['source_relative_path'])}` | "
            f"`{markdown_path(operation['output_relative_path'])}` | "
            f"{operation['expected_byte_size']} | `{operation['digest']}` | "
            f"{canonical_reason} |"
        )

    if output_entries:
        lines.extend(["", "## Plan Output Entries", ""])
        lines.extend(
            [
                "| Kind | Source relative path | Output relative path | Bytes | Reason |",
                "| --- | --- | --- | ---: | --- |",
            ]
        )
        lines.extend(
            f"| {entry['entry_kind']} | `{markdown_path(entry['source_relative_path'] or '')}` | "
            f"`{markdown_path(entry['output_relative_path'])}` | "
            f"{entry['expected_byte_size'] or ''} | {entry['reason']} |"
            for entry in output_entries
        )

    if conflict_groups:
        lines.extend(["", "## Lossless Conflict Projections", ""])
        lines.append(
            "Every conflicting source variant is retained with deterministic source provenance."
        )
        if conflict_projection_total > len(conflict_projections):
            lines.append(
                f"Showing the first {len(conflict_projections)} of "
                f"{conflict_projection_total} retained source mappings. Complete evidence: "
                "`--section conflicts --offset N --limit N`."
            )
        mappings_by_group: dict[int, list[sqlite3.Row]] = {}
        for mapping in conflict_projections:
            mappings_by_group.setdefault(int(mapping["group_index"]), []).append(
                mapping
            )
        for group_index, group in conflict_groups.items():
            if group_index not in mappings_by_group:
                continue
            lines.extend(
                [
                    "",
                    f"### Lossless Conflict Projection: `{markdown_path(group.canonical_root)}`",
                    "",
                    "**Participating Directory Trees:** "
                    + ", ".join(f"`{markdown_path(root)}`" for root in group.roots)
                    + "  ",
                    f"**Conventional derived root:** `{markdown_path(group.canonical_root)}`  ",
                    f"**Canonical selection reason:** {group.canonical_reason}",
                    "",
                    "#### Retained source mappings",
                    "",
                ]
            )
            lines.extend(
                f"- `{markdown_path(row['source_relative_path'])}` → "
                f"`{markdown_path(row['output_relative_path'])}` "
                f"({row['disposition']}; {row['reason']})"
                for row in mappings_by_group.get(group_index, [])
            )
            lines.extend(["", "#### Omitted unprocessable entries", ""])
            omitted = conflict_omitted_by_index.get(group_index, [])
            if omitted:
                lines.extend(
                    f"- `{markdown_path(row['relative_path'])}`: {row['reason']}"
                    for row in omitted
                )
            elif not conflict_omitted_totals[group_index]:
                lines.append("None.")
            if conflict_omitted_totals[group_index] > len(omitted):
                lines.append(
                    f"Showing the first {len(omitted)} of {conflict_omitted_totals[group_index]}. "
                    "Complete skipped evidence: `plan-report --section skipped --offset N --limit N`."
                )

    if structural_unions:
        lines.extend(["", "## Structural Unions", ""])
        for union in structural_unions:
            roots = union.roots
            canonical = union.canonical_root
            classification = union.classification
            canonical_reason = union.canonical_reason
            union_entries = entries_by_union.get(roots, [])
            copyable_entries = [
                entry for entry in union_entries if entry["entry_kind"] == "file"
            ]
            empty_directories = empty_directories_by_union.get(roots, [])
            if roots in projection_evidence_by_roots:
                qualifications, unverified = projection_evidence_by_roots[roots]
                omitted_entries = projection_omitted_by_roots[roots]
            else:
                qualifications, unverified = relationship_evidence_by_roots.get(
                    (roots[0], roots[1]), ([], [])
                )
                omitted_entries = [
                    row for root in roots for row in unprocessable_entries_under(root)
                ]
            redundant_roots = [root for root in roots if root != canonical]
            pruned_wrapper_ancestors: list[str] = []
            for redundant_root in redundant_roots:
                for ancestor in Path(redundant_root).parents:
                    ancestor_path = ancestor.as_posix()
                    if ancestor_path == ".":
                        break
                    if has_output_at_or_below(ancestor_path):
                        break
                    pruned_wrapper_ancestors.append(ancestor_path)
            lines.extend(
                [
                    f"### Structural Union: `{markdown_path(canonical)}`",
                    "",
                    f"**Classification:** {classification}  ",
                    "**Participating Directory Trees:** "
                    + ", ".join(f"`{markdown_path(root)}`" for root in roots)
                    + "  ",
                    f"**Canonical Directory Root:** `{markdown_path(canonical)}`  ",
                    f"**Canonical selection reason:** {canonical_reason}",
                    "",
                    "### Retained Copyable Descendants",
                    "",
                ]
            )
            if copyable_entries:
                lines.extend(
                    f"- `{markdown_path(entry['source_relative_path'])}` → "
                    f"`{markdown_path(entry['output_relative_path'])}`"
                    for entry in copyable_entries
                )
            else:
                lines.append("None.")
            lines.extend(["", "### Explicit Empty Directories", ""])
            if empty_directories:
                lines.extend(
                    f"- `{markdown_path(path)}`" for path in sorted(empty_directories)
                )
            else:
                lines.append("None.")
            lines.extend(["", "### Wrapper Pruning", ""])
            if redundant_roots:
                lines.append(
                    "Redundant peer roots omitted from the Derived Workspace: "
                    + ", ".join(f"`{markdown_path(root)}`" for root in redundant_roots)
                    + "."
                )
            else:
                lines.append("No redundant peer roots are omitted.")
            if pruned_wrapper_ancestors:
                lines.append(
                    "Newly empty wrapper ancestors omitted after peer pruning: "
                    + ", ".join(
                        f"`{markdown_path(path)}`"
                        for path in sorted(set(pruned_wrapper_ancestors))
                    )
                    + "."
                )
            else:
                lines.append(
                    "No newly empty wrapper ancestors are omitted by this projection."
                )
            lines.append(
                "The canonical root, listed explicit directories, and unrelated output paths remain preserved."
            )
            lines.extend(["", "### Evidence Qualifications and Omitted Entries", ""])
            if qualifications:
                lines.append(
                    "**Persisted qualification paths:** "
                    + ", ".join(f"`{markdown_path(path)}`" for path in qualifications)
                )
            if omitted_entries:
                lines.extend(
                    f"- `{markdown_path(row['relative_path'])}`: "
                    f"{row['reason'] or str(row['entry_kind']) + ': ' + str(row['read_outcome'])}"
                    for row in omitted_entries
                )
            elif not projection_omitted_totals.get(roots, 0):
                lines.append("None.")
            if projection_omitted_totals.get(roots, 0) > len(omitted_entries):
                lines.append(
                    f"Showing {len(omitted_entries)} of {projection_omitted_totals[roots]} omitted entries. "
                    "Complete skipped evidence: `plan-report --section skipped --offset N --limit N`."
                )
            lines.extend(["", "### Unverified Counterparts", ""])
            if unverified:
                lines.extend(
                    f"- `{markdown_path(_rooted_unverified_counterpart(roots, path))}`"
                    for path in unverified
                )
            else:
                lines.append("None.")

    if structural_unions or conflict_groups:
        lines.extend(["", "## Evidence Qualifications and Omitted Entries", ""])
        lines.append(
            "The Structural Union and Lossless Conflict Projection sections summarize unprocessable "
            "entries omitted from the Derived Workspace; complete findings use "
            "`plan-report --section skipped --offset N --limit N`. This evidence does not alter "
            "any proven copy."
        )

    if conflicts and not conflict_groups:
        lines.extend(["", "## Structural Conflicts Left Separate", ""])
        lines.append(
            "These relationships are neither actionable nor resolved by this plan; "
            "their Directory Trees remain separate."
        )
        lines.append("")
        if structural_conflict_total > len(conflicts):
            lines.extend(
                [
                    (
                        f"Showing the first {len(conflicts)} of {structural_conflict_total}. "
                        "Complete evidence: "
                        "`report --section conflicts --offset N --limit N`."
                    ),
                    "",
                ]
            )
        conflicts_by_roots: dict[tuple[str, str], list[str]] = {}
        for conflict in conflicts:
            roots = (str(conflict["left_root"]), str(conflict["right_root"]))
            conflicts_by_roots.setdefault(roots, []).append(
                str(conflict["descendant_relative_path"])
            )
        for roots, descendant_paths in conflicts_by_roots.items():
            qualifications, unverified = relationship_evidence_by_roots.get(
                roots, ([], [])
            )
            omitted_entries = [
                row for root in roots for row in unprocessable_entries_under(root)
            ]
            lines.extend(
                [
                    f"### `{markdown_path(roots[0])}` ↔ `{markdown_path(roots[1])}`",
                    "",
                    "Conflicting descendant paths: "
                    + ", ".join(
                        f"`{markdown_path(path)}`" for path in descendant_paths
                    ),
                ]
            )
            if qualifications:
                lines.append(
                    "**Evidence Qualifications:** "
                    + ", ".join(f"`{markdown_path(path)}`" for path in qualifications)
                )
            if omitted_entries:
                lines.append("**Omitted unprocessable entries:**")
                lines.extend(
                    f"- `{markdown_path(row['relative_path'])}`: "
                    f"{row['reason'] or str(row['entry_kind']) + ': ' + str(row['read_outcome'])}"
                    for row in omitted_entries
                )
            if unverified:
                lines.append("**Unverified Counterparts:**")
                lines.extend(
                    f"- `{markdown_path(_rooted_unverified_counterpart(roots, path))}`"
                    for path in unverified
                )
            lines.append("")

    if overrides:
        lines.extend(
            [
                "",
                "## Canonical Copy Overrides",
                "",
                "| Selected relative path | Prior relative path | Reason |",
                "| --- | --- | --- |",
            ]
        )
        for override in overrides:
            lines.append(
                f"| `{markdown_path(override['selected_relative_path'])}` | "
                f"`{markdown_path(override['prior_relative_path'])}` | "
                f"{override['reason']} |"
            )

    return lines


def render_full_plan_report(analysis_run: Path, plan_id: str | None = None) -> str:
    document = _build_full_plan_report(analysis_run, plan_id)
    try:
        return document.render()
    finally:
        document.close()


def write_full_plan_report(
    analysis_run: Path, plan_id: str | None, output: TextIO
) -> None:
    document = _build_full_plan_report(analysis_run, plan_id)
    try:
        document.write_to(output)
    finally:
        document.close()


@run_workspace_refusal
def render_plan_report(
    analysis_run: Path,
    plan_id: str | None = None,
    *,
    detail: str = "summary",
    section: str | None = None,
    offset: int = 0,
    limit: int | None = None,
    depth: int = 3,
) -> str:
    """Render plan facts while keeping findings and conflicts explicitly paged."""

    if detail == "full":
        if section is not None or offset or limit is not None:
            raise ConsolidationPlanError(
                "--detail full cannot be combined with section paging"
            )
        return render_full_plan_report(analysis_run, plan_id)
    if detail != "summary":
        raise ConsolidationPlanError(f"Unknown plan report detail mode: {detail}")
    if offset < 0 or limit is not None and limit < 1 or depth < 1:
        raise ConsolidationPlanError(
            "Plan report offset must be non-negative; limit and depth must be positive"
        )
    _, database_path = resolve_run_directory(analysis_run)
    connection = open_read_only(database_path)
    try:
        connection.execute("BEGIN")
        plan = select_plan_row(connection, plan_id)
        if section == "structure":
            page_limit = limit if limit is not None else -1
            if plan["status"] == "finalized":
                entry_query = (
                    "SELECT entry_kind, output_relative_path FROM final_plan_entries "
                    "WHERE plan_id=?"
                )
                entry_parameters: tuple[object, ...] = (plan["plan_id"],)
            else:
                entry_query = (
                    "SELECT entry_kind, output_relative_path FROM plan_layout_active_entries "
                    "WHERE plan_id=? AND disposition='place' UNION ALL "
                    "SELECT 'directory', output_relative_path FROM plan_layout_active_directories "
                    "WHERE plan_id=?"
                )
                entry_parameters = (plan["plan_id"], plan["plan_id"])
            # Derive implicit directories and aggregate their file counts in
            # SQLite. Direct destination directories are level 1; files stay
            # in the paged operations view instead of flooding this overview.
            entries = connection.execute(
                "WITH RECURSIVE entries(entry_kind, output_relative_path) AS ("
                + entry_query
                + "), walk(entry_kind, output_relative_path, prefix, rest, path_depth) AS ("
                "SELECT entry_kind, output_relative_path, '', output_relative_path, -1 FROM entries "
                "UNION ALL SELECT entry_kind, output_relative_path, "
                "CASE WHEN prefix='' THEN CASE WHEN instr(rest, '/')=0 THEN rest ELSE substr(rest, 1, instr(rest, '/')-1) END "
                "ELSE prefix || '/' || CASE WHEN instr(rest, '/')=0 THEN rest ELSE substr(rest, 1, instr(rest, '/')-1) END END, "
                "CASE WHEN instr(rest, '/')=0 THEN '' ELSE substr(rest, instr(rest, '/')+1) END, path_depth+1 "
                "FROM walk WHERE rest<>'' AND path_depth<?), directories(output_relative_path) AS ("
                "SELECT prefix FROM walk WHERE path_depth BETWEEN 0 AND ? "
                "AND (rest<>'' OR entry_kind='directory') GROUP BY prefix), "
                "file_counts(output_relative_path, direct_file_count, descendant_file_count) AS ("
                "SELECT prefix, SUM(instr(rest, '/')=0), COUNT(*) FROM walk "
                "WHERE entry_kind='file' AND rest<>'' AND path_depth BETWEEN 0 AND ? GROUP BY prefix) "
                "SELECT directories.output_relative_path, "
                "COALESCE(file_counts.direct_file_count, 0) AS direct_file_count, "
                "COALESCE(file_counts.descendant_file_count, 0) AS descendant_file_count "
                "FROM directories LEFT JOIN file_counts USING(output_relative_path) "
                "ORDER BY directories.output_relative_path LIMIT ? OFFSET ?",
                (
                    *entry_parameters,
                    depth - 1,
                    depth - 1,
                    depth - 1,
                    page_limit,
                    offset,
                ),
            ).fetchall()
            root_file_count = connection.execute(
                "WITH entries(entry_kind, output_relative_path) AS ("
                + entry_query
                + ") SELECT COUNT(*) FROM entries WHERE entry_kind='file' "
                "AND instr(output_relative_path, '/')=0",
                entry_parameters,
            ).fetchone()[0]
            revision = connection.execute(
                "SELECT active_revision FROM plan_layout_state WHERE plan_id = ?",
                (plan["plan_id"],),
            ).fetchone()
            excluded = connection.execute(
                "SELECT excluded_entry_count FROM plan_layout_state WHERE plan_id=?",
                (plan["plan_id"],),
            ).fetchone()[0]
            lines = [
                "# Consolidation Plan Structure",
                "",
                f"**Plan ID:** `{plan['plan_id']}`  ",
                f"**Layout Revision:** {revision['active_revision']}  ",
                f"**Directory Levels:** {depth}  ",
                f"**User Exclusions:** {excluded}  ",
                f"**Files directly in destination root:** {root_file_count}",
                "",
                "| Output directory | Level | Direct files | Files in subtree |",
                "| --- | ---: | ---: | ---: |",
            ]
            lines.extend(
                f"| `{markdown_path(row['output_relative_path'])}` | "
                f"{str(row['output_relative_path']).count('/') + 1} | "
                f"{row['direct_file_count']} | {row['descendant_file_count']} |"
                for row in entries
            )
            return "\n".join(lines) + "\n"
        if section == "operations":
            page_limit = limit if limit is not None else DEFAULT_PAGE_LIMIT
            table = (
                "final_plan_entries"
                if plan["status"] == "finalized"
                else "plan_layout_active_entries"
            )
            if table == "final_plan_entries":
                operations = connection.execute(
                    "SELECT source_relative_path, output_relative_path, expected_byte_size, digest, reason, entry_id "
                    "FROM final_plan_entries WHERE plan_id=? AND entry_kind='file' ORDER BY entry_index LIMIT ? OFFSET ?",
                    (plan["plan_id"], page_limit, offset),
                ).fetchall()
            else:
                operations = connection.execute(
                    "SELECT selection.source_relative_path, active.output_relative_path, baseline.expected_byte_size, "
                    "baseline.digest, selection.reason, active.entry_id FROM plan_layout_active_entries AS active "
                    "JOIN plan_baseline_entries AS baseline ON baseline.plan_id=active.plan_id AND baseline.entry_id=active.entry_id "
                    "JOIN plan_source_selections AS selection ON selection.plan_id=active.plan_id AND selection.entry_id=active.entry_id "
                    "WHERE active.plan_id=? AND active.disposition='place' AND active.entry_kind='file' "
                    "ORDER BY active.output_relative_path LIMIT ? OFFSET ?",
                    (plan["plan_id"], page_limit, offset),
                ).fetchall()
            lines = [
                "# Consolidation Plan Operations",
                "",
                f"**Plan ID:** `{plan['plan_id']}`  ",
                f"**Offset:** {offset}  ",
                f"**Limit:** {page_limit}",
                "",
                "| Source relative path | Output relative path | Stable entry ID | Bytes | Digest | Reason |",
                "| --- | --- | --- | ---: | --- | --- |",
            ]
            lines.extend(
                f"| `{markdown_path(row['source_relative_path'])}` | "
                f"`{markdown_path(row['output_relative_path'])}` | "
                f"`{row['entry_id']}` | "
                f"{row['expected_byte_size']} | `{row['digest']}` | {row['reason']} |"
                for row in operations
            )
            return "\n".join(lines) + "\n"
        if section == "exclusions":
            page_limit = limit if limit is not None else DEFAULT_PAGE_LIMIT
            revision = connection.execute(
                "SELECT active_revision FROM plan_layout_state WHERE plan_id = ?",
                (plan["plan_id"],),
            ).fetchone()
            if plan["status"] == "finalized":
                exclusions = connection.execute(
                    "SELECT DISTINCT baseline.entry_id, baseline.baseline_path, baseline.source_relative_path, baseline.expected_byte_size "
                    "FROM final_plan_exclusions AS final JOIN plan_baseline_entries AS baseline "
                    "ON baseline.plan_id=final.plan_id AND baseline.entry_id=final.entry_id "
                    "WHERE final.plan_id=? ORDER BY baseline.baseline_path LIMIT ? OFFSET ?",
                    (plan["plan_id"], page_limit, offset),
                ).fetchall()
            else:
                exclusions = connection.execute(
                    "SELECT baseline.entry_id, baseline.baseline_path, baseline.source_relative_path, baseline.expected_byte_size "
                    "FROM plan_layout_active_entries AS active JOIN plan_baseline_entries AS baseline "
                    "ON baseline.plan_id=active.plan_id AND baseline.entry_id=active.entry_id "
                    "WHERE active.plan_id=? AND active.disposition='exclude' ORDER BY baseline.baseline_path LIMIT ? OFFSET ?",
                    (plan["plan_id"], page_limit, offset),
                ).fetchall()
            lines = [
                "# Consolidation Plan Exclusions",
                "",
                f"**Plan ID:** `{plan['plan_id']}`  ",
                f"**Offset:** {offset}  ",
                f"**Limit:** {page_limit}",
                "",
                "| Stable entry ID | Baseline path | Source relative path | Bytes |",
                "| --- | --- | --- | ---: |",
            ]
            lines.extend(
                f"| `{row['entry_id']}` | `{markdown_path(row['baseline_path'])}` | "
                f"`{markdown_path(row['source_relative_path'] or '-')}` | {row['expected_byte_size'] or '-'} |"
                for row in exclusions
            )
            return "\n".join(lines) + "\n"
        if section == "skipped":
            page_limit = limit if limit is not None else DEFAULT_PAGE_LIMIT
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM skipped_entry_findings WHERE run_id = ?",
                    (plan["run_id"],),
                ).fetchone()[0]
            )
            findings = connection.execute(
                "SELECT relative_path, reason FROM skipped_entry_findings "
                "WHERE run_id = ? ORDER BY relative_path LIMIT ? OFFSET ?",
                (plan["run_id"], page_limit, offset),
            ).fetchall()
            lines = [
                "# Consolidation Plan Skipped Entry Findings",
                "",
                f"**Plan ID:** `{plan['plan_id']}`  ",
                f"**Total:** {total}  ",
                f"**Offset:** {offset}  ",
                f"**Limit:** {page_limit}",
                "",
            ]
            lines.extend(
                f"- `{markdown_path(row['relative_path'])}`: {row['reason']}"
                for row in findings
            )
            if not findings:
                lines.append("None.")
            return "\n".join(lines) + "\n"
        if section == "conflicts":
            page_limit = limit if limit is not None else DEFAULT_PAGE_LIMIT
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM plan_conflict_projections WHERE plan_id = ?",
                    (plan["plan_id"],),
                ).fetchone()[0]
            )
            conflicts = connection.execute(
                "SELECT source_relative_path, output_relative_path, disposition, reason "
                "FROM plan_conflict_projections WHERE plan_id = ? "
                "ORDER BY group_index, conflict_index LIMIT ? OFFSET ?",
                (plan["plan_id"], page_limit, offset),
            ).fetchall()
            lines = [
                "# Consolidation Plan Lossless Conflict Projections",
                "",
                f"**Plan ID:** `{plan['plan_id']}`  ",
                f"**Total:** {total}  ",
                f"**Offset:** {offset}  ",
                f"**Limit:** {page_limit}",
                "",
            ]
            lines.extend(
                f"- `{markdown_path(row['source_relative_path'])}` → "
                f"`{markdown_path(row['output_relative_path'])}` "
                f"({row['disposition']}; {row['reason']})"
                for row in conflicts
            )
            if not conflicts:
                lines.append("None.")
            return "\n".join(lines) + "\n"
        if section is not None:
            raise ConsolidationPlanError(f"Unknown plan report section: {section}")
        if plan["status"] == "finalized":
            counts = connection.execute(
                "SELECT SUM(entry_kind='file') AS files, SUM(entry_kind='directory') AS directories FROM final_plan_entries WHERE plan_id=?",
                (plan["plan_id"],),
            ).fetchone()
        else:
            counts = connection.execute(
                "SELECT placed_content_entry_count AS files, (SELECT COUNT(*) FROM plan_layout_active_entries WHERE plan_id=? AND disposition='place' AND entry_kind='directory') + (SELECT COUNT(*) FROM plan_layout_active_directories WHERE plan_id=?) AS directories FROM plan_layout_state WHERE plan_id=?",
                (plan["plan_id"], plan["plan_id"], plan["plan_id"]),
            ).fetchone()
        skipped_total = int(
            connection.execute(
                "SELECT COUNT(*) FROM skipped_entry_findings WHERE run_id = ?",
                (plan["run_id"],),
            ).fetchone()[0]
        )
        skipped = connection.execute(
            "SELECT relative_path, reason FROM skipped_entry_findings WHERE run_id = ? "
            "ORDER BY relative_path LIMIT ?",
            (plan["run_id"], SUMMARY_SAMPLE_LIMIT),
        ).fetchall()
        conflict_total = int(
            connection.execute(
                "SELECT COUNT(*) FROM plan_conflict_projections WHERE plan_id = ?",
                (plan["plan_id"],),
            ).fetchone()[0]
        )
        conflicts = connection.execute(
            "SELECT source_relative_path, output_relative_path, disposition, reason "
            "FROM plan_conflict_projections WHERE plan_id = ? "
            "ORDER BY group_index, conflict_index LIMIT ?",
            (plan["plan_id"], SUMMARY_SAMPLE_LIMIT),
        ).fetchall()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    lines = [
        "# Consolidation Plan Report",
        "",
        f"**Plan ID:** `{plan['plan_id']}`  ",
        f"**Run ID:** `{plan['run_id']}`  ",
        f"**Snapshot ID:** `{plan['snapshot_id']}`  ",
        f"**Status:** {plan['status']}  ",
        f"**Intended Destination:** `{markdown_path(plan['intended_destination'])}`  ",
        f"**Operation Count:** {counts['files'] or 0}  ",
        f"**Explicit directory count:** {counts['directories'] or 0}",
        "",
        "## Skipped Entry Findings",
        "",
    ]
    if skipped:
        lines.extend(
            f"- `{markdown_path(row['relative_path'])}`: {row['reason']}"
            for row in skipped
        )
    else:
        lines.append("None.")
    if skipped_total > len(skipped):
        lines.append(
            f"Showing the first {len(skipped)} of {skipped_total}. Complete evidence: "
            "`--section skipped --offset N --limit N`."
        )
    lines.extend(["", "## Lossless Conflict Projections", ""])
    if conflicts:
        lines.extend(
            f"- `{markdown_path(row['source_relative_path'])}` → "
            f"`{markdown_path(row['output_relative_path'])}` ({row['disposition']}; {row['reason']})"
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
                "evidence: `--section operations|exclusions|skipped|conflicts "
                "--offset N --limit N`."
            ),
        ]
    )
    return "\n".join(lines) + "\n"
