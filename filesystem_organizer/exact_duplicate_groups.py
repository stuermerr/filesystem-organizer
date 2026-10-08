from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

CANONICAL_REASON_NEWEST_MODIFIED = "newest modification timestamp"
CANONICAL_REASON_RELATIVE_PATH_FALLBACK = (
    "deterministic relative-path fallback (tied or unavailable modification timestamps)"
)

READ_OUTCOME_SUCCESSFUL = "successful"
READ_OUTCOME_UNREADABLE = "unreadable"
READ_OUTCOME_CHANGED_DURING_READ = "changed-during-read"
READ_OUTCOME_CHANGED_SINCE_SCAN = "changed-since-scan"


@dataclass(frozen=True)
class IdentityRow:
    """One content identity observed for a relative path during an Analysis Run.

    ``read_outcome`` marks whether the identity was proven by a stable, full
    content read (``"successful"``) or not; only proven identities may form an
    Exact Duplicate group.
    """

    relative_path: str
    algorithm: str
    algorithm_version: int
    byte_size: int
    digest: str
    read_outcome: str
    modified_ns: int | None


@dataclass(frozen=True)
class GroupKey:
    """The full content-identity key that defines one Exact Duplicate group."""

    algorithm: str
    algorithm_version: int
    byte_size: int
    digest: str


@dataclass(frozen=True)
class ExactDuplicateGroup:
    """A proven byte-identical group and its Canonical Copy choice.

    ``member_relative_paths`` is sorted lexicographically. A group of one has
    the single member as its Canonical Copy with no canonical reason.
    """

    key: GroupKey
    member_relative_paths: tuple[str, ...]
    canonical_relative_path: str
    canonical_reason: str | None


def _select_canonical_copy(
    member_relative_paths: tuple[str, ...], modified_ns_by_path: dict[str, int | None]
) -> tuple[str, str | None]:
    """Choose the Canonical Copy for one group, returning (path, reason).

    The rule is: the newest modification timestamp; on a tie or unavailable
    timestamp, the lexicographically smallest relative path, recorded with its
    reason. A group of one is its own Canonical Copy with no reason.
    """
    if len(member_relative_paths) == 1:
        return member_relative_paths[0], None

    modified_values = [modified_ns_by_path[path] for path in member_relative_paths]
    newest_modified = max(
        (value for value in modified_values if value is not None), default=None
    )
    newest_members = (
        [path for path in member_relative_paths if modified_ns_by_path[path] == newest_modified]
        if newest_modified is not None
        else list(member_relative_paths)
    )
    if newest_modified is not None and len(newest_members) == 1:
        return newest_members[0], CANONICAL_REASON_NEWEST_MODIFIED
    return min(newest_members), CANONICAL_REASON_RELATIVE_PATH_FALLBACK


def derive_exact_duplicate_groups(
    rows: Iterable[IdentityRow],
) -> list[ExactDuplicateGroup]:
    """Derive Exact Duplicate groups from proven content identities.

    Only rows with ``read_outcome == "successful"`` participate. Members sharing
    a full content-identity key form one group; a key occurring once forms a
    singleton group whose sole member is its own Canonical Copy. Groups are
    returned ordered by digest and then Canonical Copy relative path so report
    output stays deterministic.
    """
    members_by_key: dict[GroupKey, dict[str, int | None]] = {}
    for row in rows:
        if row.read_outcome != READ_OUTCOME_SUCCESSFUL:
            continue
        key = GroupKey(
            row.algorithm, row.algorithm_version, row.byte_size, row.digest
        )
        members_by_key.setdefault(key, {})[row.relative_path] = row.modified_ns

    groups: list[ExactDuplicateGroup] = []
    for key, modified_ns_by_path in members_by_key.items():
        member_relative_paths = tuple(sorted(modified_ns_by_path))
        canonical_relative_path, canonical_reason = _select_canonical_copy(
            member_relative_paths, modified_ns_by_path
        )
        groups.append(
            ExactDuplicateGroup(
                key=key,
                member_relative_paths=member_relative_paths,
                canonical_relative_path=canonical_relative_path,
                canonical_reason=canonical_reason,
            )
        )

    groups.sort(key=lambda group: (group.key.digest, group.canonical_relative_path))
    return groups
