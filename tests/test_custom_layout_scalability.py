from __future__ import annotations

from typing import Any, cast

from scripts.benchmark_custom_layout import run_case


def test_small_layout_edits_do_not_write_the_complete_projection() -> None:
    for size in (10_000, 100_000):
        result = run_case(size, "one-entry", 1)
        sample = cast(list[dict[str, Any]], result["samples"])[0]
        assert sample["total_entries"] == size
        assert sample["affected_entries"] == 1
        assert sample["changed_entries"] == 1
        assert sample["sqlite_rows_read"] < 100
        assert sample["sqlite_rows_written"] < 100
        # The progress handler counts executed SQLite opcodes, so a hidden
        # complete-table SELECT is visible even if it writes nothing.
        assert sample["sqlite_vm_steps_lower_bound"] < 500_000
        assert sample["peak_memory_bytes"] > 0


def test_moderate_subtree_and_root_wide_layout_edits() -> None:
    for size in (10_000, 100_000):
        for scenario in ("subtree", "root-wide"):
            result = run_case(size, scenario, 1)
            sample = cast(list[dict[str, Any]], result["samples"])[0]
            assert sample["total_entries"] == size
            assert sample["affected_entries"] >= sample["changed_entries"] > 0
            assert sample["sqlite_rows_written"] >= 2 * sample["changed_entries"]
