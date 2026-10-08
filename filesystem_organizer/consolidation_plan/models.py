from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps

from ..run_workspace import PLAN_SCHEMA_VERSION as CURRENT_PLAN_SCHEMA_VERSION
from ..run_workspace import RunWorkspaceError

PLAN_SCHEMA_VERSION = CURRENT_PLAN_SCHEMA_VERSION

PLAN_STATUS_DRAFT = "draft"
PLAN_STATUS_FINALIZED = "finalized"


class ConsolidationPlanError(Exception):
    """A safe, user-facing refusal of a Consolidation Plan operation."""


def run_workspace_refusal[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    """Translate Run Workspace refusals at the Consolidation Plan seam."""

    @wraps(function)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return function(*args, **kwargs)
        except RunWorkspaceError as error:
            raise ConsolidationPlanError(str(error)) from error

    return wrapper


@dataclass(frozen=True)
class PlanOperation:
    source_relative_path: str
    output_relative_path: str
    expected_byte_size: int
    algorithm: str | None
    algorithm_version: int | None
    digest: str | None
    canonical_reason: str | None
    modified_ns: int | None = None
    evidence_kind: str = "content-identity"


@dataclass(frozen=True)
class PlanOutputEntry:
    entry_kind: str
    output_relative_path: str
    source_relative_path: str | None
    expected_byte_size: int | None
    algorithm: str | None
    algorithm_version: int | None
    digest: str | None
    reason: str
    modified_ns: int | None = None
    evidence_kind: str = "content-identity"


@dataclass(frozen=True)
class StructuralUnion:
    roots: tuple[str, ...]
    canonical_root: str
    classification: str
    canonical_reason: str


@dataclass(frozen=True)
class ConflictSourceMapping:
    source_relative_path: str
    output_relative_path: str
    entry_kind: str
    disposition: str
    reason: str


@dataclass(frozen=True)
class LosslessConflictProjection:
    roots: tuple[str, ...]
    canonical_root: str
    canonical_reason: str
