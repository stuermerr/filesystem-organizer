"""The stable Consolidation Plan interface."""

from .custom_layout import (
    apply_layout,
    export_layout,
    rebuild_active_projection,
    validate_layout,
)
from .lifecycle import (
    create_consolidation_plan,
    finalize_consolidation_plan,
    override_canonical_copy,
)
from .models import (
    PLAN_SCHEMA_VERSION,
    PLAN_STATUS_DRAFT,
    PLAN_STATUS_FINALIZED,
    ConsolidationPlanError,
)
from .reporting import render_plan_report, write_full_plan_report

__all__ = [
    "PLAN_SCHEMA_VERSION",
    "PLAN_STATUS_DRAFT",
    "PLAN_STATUS_FINALIZED",
    "ConsolidationPlanError",
    "apply_layout",
    "create_consolidation_plan",
    "export_layout",
    "finalize_consolidation_plan",
    "override_canonical_copy",
    "rebuild_active_projection",
    "render_plan_report",
    "validate_layout",
    "write_full_plan_report",
]
