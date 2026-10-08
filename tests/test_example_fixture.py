from __future__ import annotations

import stat
from pathlib import Path

from tests.example_fixture import (
    OUTPUT_ROOT,
    SOURCE_ROOT,
    copy_prepared_source,
    load_fixture_manifest,
)

OMITTED_SOURCE_PATHS = {
    "snapshot-2025-07-01/Home/Desktop/link-to-annual-tax-statement",
    "snapshot-2025-07-01/Home/Desktop/unreadable.txt",
}

NON_CANONICAL_DUPLICATE_PATHS = {
    "snapshot-2025-07-01/Home/Desktop/annual_tax_statement_2024 (copy).pdf",
    "snapshot-2025-07-01/Home/Desktop/recovery-checklist (copy).txt",
}


def relative_entries(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*")}


def test_prepared_source_reconstructs_non_git_metadata(tmp_path: Path) -> None:
    prepared = copy_prepared_source(tmp_path / "selected-backup-root")
    manifest = load_fixture_manifest()

    for relative_path, expected_modified_time_ns in manifest[
        "source_modified_time_ns"
    ].items():
        assert (
            prepared / relative_path
        ).stat().st_mtime_ns == expected_modified_time_ns

    for relative_path in manifest["unreadable_source_paths"]:
        mode = (prepared / relative_path).stat().st_mode
        assert mode & (stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH) == 0

    link = prepared / "snapshot-2025-07-01/Home/Desktop/link-to-annual-tax-statement"
    assert link.is_symlink()
    assert link.readlink() == Path("annual_tax_statement_2024.pdf")


def test_example_output_is_the_expected_source_subset() -> None:
    source_entries = relative_entries(SOURCE_ROOT)
    target_entries = relative_entries(OUTPUT_ROOT)

    assert (
        source_entries - target_entries
        == OMITTED_SOURCE_PATHS
        | NON_CANONICAL_DUPLICATE_PATHS
        | {
            "snapshot-2023-11-01",
            "snapshot-2023-11-01/Home",
            "snapshot-2023-11-01/Home/Projects",
            "snapshot-2023-11-01/Home/Projects/ai-assistant",
            "snapshot-2023-11-01/Home/Projects/ai-assistant/docs",
            "snapshot-2023-11-01/Home/Projects/ai-assistant/docs/recovery-checklist.txt",
        }
    )
    assert target_entries - source_entries == set()
    for relative_path in target_entries:
        source_path = SOURCE_ROOT / relative_path
        target_path = OUTPUT_ROOT / relative_path
        if target_path.is_file():
            assert target_path.read_bytes() == source_path.read_bytes()
