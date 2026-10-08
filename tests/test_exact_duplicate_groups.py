from __future__ import annotations

from filesystem_organizer.exact_duplicate_groups import (
    CANONICAL_REASON_NEWEST_MODIFIED,
    CANONICAL_REASON_RELATIVE_PATH_FALLBACK,
    ExactDuplicateGroup,
    GroupKey,
    IdentityRow,
    derive_exact_duplicate_groups,
)

ALGORITHM = "BLAKE3-256"
ALGORITHM_VERSION = 1
DIGEST = "d" * 64


def _row(
    relative_path: str,
    *,
    digest: str = DIGEST,
    byte_size: int = 10,
    modified_ns: int | None = 1_700_000_000_000_000_000,
    read_outcome: str = "successful",
) -> IdentityRow:
    return IdentityRow(
        relative_path=relative_path,
        algorithm=ALGORITHM,
        algorithm_version=ALGORITHM_VERSION,
        byte_size=byte_size,
        digest=digest,
        read_outcome=read_outcome,
        modified_ns=modified_ns,
    )


def _only_group(groups: list[ExactDuplicateGroup]) -> ExactDuplicateGroup:
    assert len(groups) == 1
    return groups[0]


def test_canonical_copy_prefers_newest_modification_timestamp() -> None:
    groups = derive_exact_duplicate_groups(
        [
            _row("a/older.txt", modified_ns=1_700_000_000_000_000_000),
            _row("a/newer.txt", modified_ns=1_800_000_000_000_000_000),
        ]
    )

    group = _only_group(groups)
    assert group.canonical_relative_path == "a/newer.txt"
    assert group.canonical_reason == CANONICAL_REASON_NEWEST_MODIFIED


def test_canonical_copy_tie_resolves_to_lexicographic_fallback() -> None:
    groups = derive_exact_duplicate_groups(
        [
            _row("b/bravo.txt", modified_ns=1_700_000_000_000_000_000),
            _row("b/alpha.txt", modified_ns=1_700_000_000_000_000_000),
        ]
    )

    group = _only_group(groups)
    assert group.canonical_relative_path == "b/alpha.txt"
    assert group.canonical_reason == CANONICAL_REASON_RELATIVE_PATH_FALLBACK


def test_canonical_copy_unavailable_timestamp_falls_back_to_smallest_path() -> None:
    groups = derive_exact_duplicate_groups(
        [
            _row("c/bravo.txt", modified_ns=None),
            _row("c/alpha.txt", modified_ns=None),
        ]
    )

    group = _only_group(groups)
    assert group.canonical_relative_path == "c/alpha.txt"
    assert group.canonical_reason == CANONICAL_REASON_RELATIVE_PATH_FALLBACK


def test_canonical_copy_tie_at_max_timestamp_falls_back_among_tied() -> None:
    groups = derive_exact_duplicate_groups(
        [
            _row("d/bravo.txt", modified_ns=1_800_000_000_000_000_000),
            _row("d/alpha.txt", modified_ns=1_800_000_000_000_000_000),
            _row("d/older.txt", modified_ns=1_700_000_000_000_000_000),
        ]
    )

    group = _only_group(groups)
    assert group.canonical_relative_path == "d/alpha.txt"
    assert group.canonical_reason == CANONICAL_REASON_RELATIVE_PATH_FALLBACK


def test_singleton_group_has_single_member_as_canonical_without_reason() -> None:
    groups = derive_exact_duplicate_groups([_row("e/unique.txt")])

    group = _only_group(groups)
    assert group.member_relative_paths == ("e/unique.txt",)
    assert group.canonical_relative_path == "e/unique.txt"
    assert group.canonical_reason is None


def test_singleton_group_with_unavailable_timestamp_has_no_reason() -> None:
    groups = derive_exact_duplicate_groups([_row("f/unique.txt", modified_ns=None)])

    group = _only_group(groups)
    assert group.canonical_relative_path == "f/unique.txt"
    assert group.canonical_reason is None


def test_members_are_ordered_lexicographically() -> None:
    groups = derive_exact_duplicate_groups(
        [
            _row("g/zeta.txt"),
            _row("g/alpha.txt"),
            _row("g/mid.txt"),
        ]
    )

    group = _only_group(groups)
    assert group.member_relative_paths == ("g/alpha.txt", "g/mid.txt", "g/zeta.txt")


def test_unproven_identities_never_form_a_group() -> None:
    groups = derive_exact_duplicate_groups(
        [
            _row("h/alpha.txt", read_outcome="changed-during-read"),
            _row("h/bravo.txt", read_outcome="changed-during-read"),
        ]
    )

    assert groups == []


def test_proven_identities_group_only_with_matching_keys() -> None:
    groups = derive_exact_duplicate_groups(
        [
            _row("i/alpha.txt", digest="a" * 64),
            _row("i/bravo.txt", digest="a" * 64),
            _row("i/other.txt", digest="b" * 64),
        ]
    )

    assert [group.key.digest for group in groups] == ["a" * 64, "b" * 64]
    alpha_group = groups[0]
    assert alpha_group.member_relative_paths == ("i/alpha.txt", "i/bravo.txt")


def test_group_records_carry_the_full_key() -> None:
    groups = derive_exact_duplicate_groups(
        [
            _row("j/alpha.txt", byte_size=42),
            _row("j/bravo.txt", byte_size=42),
        ]
    )

    group = _only_group(groups)
    assert group.key == GroupKey(ALGORITHM, ALGORITHM_VERSION, 42, DIGEST)
