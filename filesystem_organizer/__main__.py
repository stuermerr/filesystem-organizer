from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path

from .analysis_run import AnalysisRunError, create_analysis_run, resume_analysis_run
from .consolidation_plan import (
    ConsolidationPlanError,
    apply_layout,
    create_consolidation_plan,
    export_layout,
    finalize_consolidation_plan,
    override_canonical_copy,
    rebuild_active_projection,
    render_plan_report,
    validate_layout,
    write_full_plan_report,
)
from .control_plane import (
    ControlPlaneError,
    discover_analysis_runs,
    inspect_analysis_run,
)
from .materialization import (
    MaterializationError,
    content_empty_result,
    materialize_consolidation_plan,
    preflight_materialization,
    preflight_materialization_for_execution,
    render_preflight_summary,
)
from .progress import TqdmProgressReporter
from .report import render_report, write_full_report

SUCCESS = 0
SAFE_REFUSAL = 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="filesystem-organizer")
    commands = parser.add_subparsers(dest="command", required=True)

    scan = commands.add_parser("scan", help="create a persisted Analysis Run")
    scan.add_argument("selected_backup_root", type=Path)
    scan.add_argument(
        "--output-root",
        type=Path,
        default=Path.cwd() / "output",
        help="Run Output Root (default: ./output)",
    )

    report = commands.add_parser("report", help="report one explicit Analysis Run")
    report.add_argument("analysis_run", type=Path)
    report.add_argument("--detail", choices=("summary", "full"), default="summary")
    report.add_argument(
        "--section", choices=("inventory", "skipped", "conflicts"), default=None
    )
    report.add_argument("--offset", type=int, default=0)
    report.add_argument("--limit", type=int, default=None)

    resume = commands.add_parser(
        "resume", help="resume an interrupted Analysis Run from its checkpoint"
    )
    resume.add_argument("analysis_run", type=Path)

    runs = commands.add_parser(
        "runs", help="discover Analysis Runs beneath one Run Output Root"
    )
    runs.add_argument(
        "run_output_root",
        type=Path,
        nargs="?",
        default=Path.cwd() / "output",
        help="Run Output Root (default: ./output)",
    )

    status = commands.add_parser(
        "status", help="report persisted state for one explicit Analysis Run"
    )
    status.add_argument("analysis_run", type=Path)

    plan = commands.add_parser(
        "plan", help="create a persisted draft Consolidation Plan"
    )
    plan.add_argument("analysis_run", type=Path)

    plan_report = commands.add_parser(
        "plan-report", help="report one persisted Consolidation Plan"
    )
    plan_report.add_argument("analysis_run", type=Path)
    plan_report.add_argument(
        "--plan-id",
        default=None,
        help="Consolidation Plan identifier (default: most recently created)",
    )
    plan_report.add_argument("--detail", choices=("summary", "full"), default="summary")
    plan_report.add_argument(
        "--section",
        choices=("operations", "structure", "exclusions", "skipped", "conflicts"),
        default=None,
    )
    plan_report.add_argument("--depth", type=int, default=3)
    plan_report.add_argument("--offset", type=int, default=0)
    plan_report.add_argument("--limit", type=int, default=None)

    plan_override = commands.add_parser(
        "plan-override",
        help="override a draft Consolidation Plan's Canonical Copy choice",
    )
    plan_override.add_argument("analysis_run", type=Path)
    plan_override.add_argument("source_relative_path")
    plan_override.add_argument("reason")
    plan_override.add_argument(
        "--plan-id",
        default=None,
        help="Consolidation Plan identifier (default: most recently created)",
    )

    plan_finalize = commands.add_parser(
        "plan-finalize", help="freeze a draft Consolidation Plan"
    )
    plan_finalize.add_argument("analysis_run", type=Path)
    plan_finalize.add_argument(
        "--plan-id",
        default=None,
        help="Consolidation Plan identifier (default: most recently created)",
    )

    layout_export = commands.add_parser(
        "plan-layout-export", help="export a Custom Layout draft"
    )
    layout_export.add_argument("analysis_run", type=Path)
    layout_export.add_argument("plan_id")
    layout_export.add_argument("--output", type=Path, required=True)
    layout_validate = commands.add_parser(
        "plan-layout-validate", help="validate a Custom Layout draft"
    )
    layout_validate.add_argument("analysis_run", type=Path)
    layout_validate.add_argument("plan_id")
    layout_validate.add_argument("layout", type=Path)
    layout_apply = commands.add_parser(
        "plan-layout-apply", help="atomically apply a Custom Layout draft"
    )
    layout_apply.add_argument("analysis_run", type=Path)
    layout_apply.add_argument("plan_id")
    layout_apply.add_argument("layout", type=Path)
    layout_apply.add_argument("--acknowledge-exclusions", action="store_true")
    layout_apply.add_argument("--acknowledge-content-empty", action="store_true")
    layout_rebuild = commands.add_parser(
        "plan-layout-rebuild-active",
        help="verify or explicitly repair the rebuildable active projection",
    )
    layout_rebuild.add_argument("analysis_run", type=Path)
    layout_rebuild.add_argument("plan_id")
    rebuild_mode = layout_rebuild.add_mutually_exclusive_group(required=True)
    rebuild_mode.add_argument("--verify", action="store_true")
    rebuild_mode.add_argument("--repair", action="store_true")

    materialize = commands.add_parser(
        "materialize",
        help="materialize an explicitly approved, finalized Consolidation Plan",
    )
    materialize.add_argument("analysis_run", type=Path)
    materialize.add_argument(
        "plan_id", help="finalized Consolidation Plan identifier (no default)"
    )
    materialize.add_argument(
        "--revision",
        type=int,
        help="required with --yes when finalizing a draft source-preserving plan",
    )
    materialize.add_argument(
        "--acknowledge-content-empty",
        action="store_true",
        help="required for a Content-Empty Result after current preflight",
    )
    materialize_destination = materialize.add_mutually_exclusive_group()
    materialize_destination.add_argument(
        "--destination",
        type=Path,
        default=None,
        help="Materialized Consolidation destination (default: the plan's intended destination)",
    )
    materialize_destination.add_argument(
        "--in-place",
        action="store_true",
        help="destructively apply the finalized plan inside the Selected Backup Root",
    )
    materialize.add_argument(
        "--yes",
        action="store_true",
        help="approve headlessly without an interactive confirmation prompt",
    )
    materialize.add_argument(
        "--acknowledge-destructive",
        action="store_true",
        help="required with --in-place in addition to approval",
    )

    materialize_preflight = commands.add_parser(
        "materialize-preflight",
        help="validate one finalized Consolidation Plan without copying",
    )
    materialize_preflight.add_argument("analysis_run", type=Path)
    materialize_preflight.add_argument(
        "plan_id", help="finalized Consolidation Plan identifier (no default)"
    )
    preflight_destination = materialize_preflight.add_mutually_exclusive_group()
    preflight_destination.add_argument(
        "--destination",
        type=Path,
        default=None,
        help="Materialized Consolidation destination (default: the plan's intended destination)",
    )
    preflight_destination.add_argument("--in-place", action="store_true")
    return parser


def main(arguments: list[str] | None = None) -> int:
    options = build_parser().parse_args(arguments)
    try:
        if options.command == "scan":
            with TqdmProgressReporter() as progress:
                result = create_analysis_run(
                    options.selected_backup_root,
                    options.output_root,
                    progress=progress,
                )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        elif options.command == "runs":
            result = discover_analysis_runs(options.run_output_root)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        elif options.command == "status":
            result = inspect_analysis_run(options.analysis_run)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        elif options.command == "resume":
            with TqdmProgressReporter() as progress:
                result = resume_analysis_run(options.analysis_run, progress=progress)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        elif options.command == "plan":
            with TqdmProgressReporter() as progress:
                result = create_consolidation_plan(
                    options.analysis_run, progress=progress
                )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        elif options.command == "plan-report":
            if options.detail == "full":
                if (
                    options.section is not None
                    or options.offset
                    or options.limit is not None
                ):
                    raise ConsolidationPlanError(
                        "--detail full cannot be combined with section paging"
                    )
                write_full_plan_report(
                    options.analysis_run, options.plan_id, sys.stdout
                )
            else:
                print(
                    render_plan_report(
                        options.analysis_run,
                        options.plan_id,
                        detail=options.detail,
                        section=options.section,
                        offset=options.offset,
                        limit=options.limit,
                        depth=options.depth,
                    ),
                    end="",
                )
        elif options.command == "plan-override":
            result = override_canonical_copy(
                options.analysis_run,
                options.source_relative_path,
                options.reason,
                options.plan_id,
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        elif options.command == "plan-finalize":
            result = finalize_consolidation_plan(options.analysis_run, options.plan_id)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        elif options.command == "plan-layout-export":
            result = export_layout(
                options.analysis_run, options.plan_id, options.output
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        elif options.command == "plan-layout-validate":
            result = validate_layout(
                options.analysis_run, options.plan_id, options.layout
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        elif options.command == "plan-layout-apply":
            result = apply_layout(
                options.analysis_run,
                options.plan_id,
                options.layout,
                options.acknowledge_exclusions,
                options.acknowledge_content_empty,
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        elif options.command == "plan-layout-rebuild-active":
            result = rebuild_active_projection(
                options.analysis_run, options.plan_id, repair=options.repair
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            if not result["consistent"]:
                return SAFE_REFUSAL
        elif options.command == "materialize-preflight":
            preflight = preflight_materialization(
                options.analysis_run,
                options.plan_id,
                options.destination,
                in_place=options.in_place,
            )
            payload = asdict(preflight)
            if not options.in_place:
                for field in ("mode", "recovery_state", "staging_metadata_bytes"):
                    del payload[field]
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        elif options.command == "materialize":
            if not options.in_place and options.revision is not None:
                try:
                    finalize_consolidation_plan(
                        options.analysis_run,
                        options.plan_id,
                        expected_revision=options.revision,
                    )
                except ConsolidationPlanError as error:
                    if "already finalized" not in str(error):
                        raise
            preflight = preflight_materialization_for_execution(
                options.analysis_run,
                options.plan_id,
                options.destination,
                in_place=options.in_place,
            )
            if not preflight.sufficient_free_space:
                required_bytes = (
                    preflight.staging_metadata_bytes
                    if options.in_place
                    else preflight.total_bytes
                )
                print(
                    "error: insufficient free space at destination: "
                    f"{preflight.free_bytes} bytes free, "
                    f"{required_bytes} bytes required",
                    file=sys.stderr,
                )
                return SAFE_REFUSAL
            if options.in_place and not options.acknowledge_destructive:
                print(
                    "error: in-place materialization requires a destructive acknowledgement "
                    "(--acknowledge-destructive) "
                    "in addition to --yes or path-specific confirmation",
                    file=sys.stderr,
                )
                return SAFE_REFUSAL
            if (
                content_empty_result(options.analysis_run, options.plan_id)
                and not options.acknowledge_content_empty
            ):
                print(
                    "error: Content-Empty Result requires --acknowledge-content-empty after preflight",
                    file=sys.stderr,
                )
                return SAFE_REFUSAL
            if options.yes:
                approved = True
            elif sys.stdin.isatty():
                sys.stdout.write(render_preflight_summary(preflight))
                prompt = (
                    f"Type the Selected Backup Root exactly to confirm destructive materialization: {preflight.selected_backup_root}: "
                    if options.in_place
                    else "Type 'yes' to confirm materialization: "
                )
                response = input(prompt)
                approved = (
                    response.strip() == preflight.selected_backup_root
                    if options.in_place
                    else response.strip().lower() == "yes"
                )
            else:
                print(
                    "error: headless materialization requires --yes with the "
                    "exact plan identifier",
                    file=sys.stderr,
                )
                return SAFE_REFUSAL
            if not approved:
                print("error: materialization was not approved", file=sys.stderr)
                return SAFE_REFUSAL
            with TqdmProgressReporter() as progress:
                result = materialize_consolidation_plan(
                    options.analysis_run,
                    options.plan_id,
                    options.destination,
                    in_place=options.in_place,
                    progress=progress,
                )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            if options.detail == "full":
                if (
                    options.section is not None
                    or options.offset
                    or options.limit is not None
                ):
                    raise AnalysisRunError(
                        "--detail full cannot be combined with section paging"
                    )
                write_full_report(options.analysis_run, sys.stdout)
            else:
                print(
                    render_report(
                        options.analysis_run,
                        detail=options.detail,
                        section=options.section,
                        offset=options.offset,
                        limit=options.limit,
                    ),
                    end="",
                )
    except (
        AnalysisRunError,
        ConsolidationPlanError,
        ControlPlaneError,
        MaterializationError,
        OSError,
        sqlite3.Error,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return SAFE_REFUSAL
    return SUCCESS


if __name__ == "__main__":
    raise SystemExit(main())
