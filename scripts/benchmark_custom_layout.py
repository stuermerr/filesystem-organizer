"""Benchmark incremental Custom Layout edits against synthetic Baseline Projections."""

from __future__ import annotations

import argparse
import json
import os
import resource
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Self

from filesystem_organizer.consolidation_plan.custom_layout import (
    apply_layout,
    finalize_layout_projection,
    initialize_layout_baseline,
    rebuild_active_projection,
)
from filesystem_organizer.run_workspace import PLAN_SCHEMA_VERSION, SCHEMA_VERSION


def _seed(run_path: Path, entry_count: int) -> tuple[str, Path]:
    run_path.mkdir(parents=True)
    database = run_path / "analysis.sqlite3"
    plan_id = f"benchmark-{entry_count}"
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE analysis_runs (run_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL);
        CREATE TABLE consolidation_plans (
          plan_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, snapshot_id TEXT NOT NULL,
          schema_version INTEGER NOT NULL, status TEXT NOT NULL,
          intended_destination TEXT NOT NULL, created_at TEXT NOT NULL, finalized_at TEXT);
        CREATE TABLE skipped_entry_findings (
          run_id TEXT NOT NULL, relative_path TEXT NOT NULL, reason TEXT NOT NULL);
        CREATE TABLE inventory_entries (
          run_id TEXT NOT NULL, relative_path TEXT NOT NULL, entry_kind TEXT NOT NULL,
          observed_byte_size INTEGER, modified_ns INTEGER, read_outcome TEXT NOT NULL);
        CREATE TABLE content_identities (
          run_id TEXT NOT NULL, relative_path TEXT NOT NULL, algorithm TEXT NOT NULL,
          algorithm_version INTEGER NOT NULL, byte_size INTEGER NOT NULL,
          digest TEXT, read_outcome TEXT NOT NULL);
        CREATE TABLE plan_output_entries (
          plan_id TEXT NOT NULL, entry_index INTEGER NOT NULL, entry_kind TEXT NOT NULL,
          source_relative_path TEXT, output_relative_path TEXT NOT NULL,
          expected_byte_size INTEGER, algorithm TEXT, algorithm_version INTEGER,
          digest TEXT, modified_ns INTEGER, evidence_kind TEXT NOT NULL,
          reason TEXT NOT NULL);
        """
    )
    connection.execute("INSERT INTO analysis_runs VALUES(?,?)", ("run", SCHEMA_VERSION))
    connection.execute(
        "INSERT INTO consolidation_plans VALUES(?,?,?,?,?,?,?,NULL)",
        (
            plan_id,
            "run",
            "snapshot",
            PLAN_SCHEMA_VERSION,
            "draft",
            "benchmark-output",
            "now",
        ),
    )
    batch: list[tuple[object, ...]] = []
    for index in range(entry_count):
        path = f"root/group-{index // 1000:06d}/file-{index:09d}.bin"
        digest = f"{index:064x}"
        batch.append(
            (
                plan_id,
                index,
                "file",
                path,
                path,
                1,
                "BLAKE3-256",
                1,
                digest,
                0,
                "content-identity",
                "benchmark",
            )
        )
        if len(batch) == 10_000:
            connection.executemany(
                "INSERT INTO plan_output_entries VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", batch
            )
            batch.clear()
    connection.executemany(
        "INSERT INTO plan_output_entries VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", batch
    )
    initialize_layout_baseline(connection, plan_id)
    connection.commit()
    connection.close()
    return plan_id, database


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[
        min(len(ordered) - 1, max(0, int(len(ordered) * fraction + 0.999999) - 1))
    ]


def _layout_for(database: Path, plan_id: str, scenario: str, output: Path) -> None:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    fingerprint = str(
        connection.execute(
            "SELECT fingerprint FROM plan_baseline_metadata WHERE plan_id=?", (plan_id,)
        ).fetchone()[0]
    )
    layout: dict[str, Any] = {
        "layout_schema_version": 2,
        "plan_id": plan_id,
        "base_revision": 0,
        "baseline_fingerprint": fingerprint,
        "unmatched": "preserve",
        "rules": [],
        "entry_exceptions": [],
        "directories": [],
        "skipped_actions": [],
    }
    if scenario == "one-entry":
        entry_id = str(
            connection.execute(
                "SELECT entry_id FROM plan_baseline_entries WHERE plan_id=? ORDER BY baseline_path LIMIT 1",
                (plan_id,),
            ).fetchone()[0]
        )
        layout["entry_exceptions"] = [
            {"entry_id": entry_id, "action": {"place_at": "edited/one.bin"}}
        ]
    elif scenario == "subtree":
        layout["rules"] = [
            {
                "selector": {"subtree": "root/group-000000"},
                "action": {"place_under": "edited"},
            }
        ]
    elif scenario == "root-wide":
        layout["rules"] = [
            {"selector": {"subtree": "root"}, "action": {"place_under": "edited"}}
        ]
    else:
        raise ValueError(f"Unknown scenario: {scenario}")
    connection.close()
    output.write_text(json.dumps(layout), encoding="utf-8")


def _database_and_wal_bytes(database: Path) -> int:
    wal = database.with_name(database.name + "-wal")
    return database.stat().st_size + (wal.stat().st_size if wal.exists() else 0)


def _resident_memory_bytes() -> int:
    try:
        with Path("/proc/self/statm").open(encoding="ascii") as status:
            pages = int(status.readline().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        maximum = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(maximum * (1 if sys.platform == "darwin" else 1024))


class _PeakMemory:
    def __init__(self) -> None:
        self.peak_bytes = 0
        self._stop = threading.Event()
        self._watcher = threading.Thread(target=self._sample, daemon=True)

    def _sample(self) -> None:
        while not self._stop.wait(0.02):
            self.peak_bytes = max(self.peak_bytes, _resident_memory_bytes())

    def __enter__(self) -> Self:
        self.peak_bytes = _resident_memory_bytes()
        self._watcher.start()
        return self

    def __exit__(self, *_exception: object) -> None:
        self._stop.set()
        self._watcher.join()
        self.peak_bytes = max(self.peak_bytes, _resident_memory_bytes())


def run_case(entry_count: int, scenario: str, repetitions: int) -> dict[str, object]:
    samples: list[dict[str, float | int]] = []
    for _ in range(repetitions):
        with (
            tempfile.TemporaryDirectory(prefix="fso-layout-benchmark-") as temporary,
            _PeakMemory() as memory,
        ):
            root = Path(temporary)
            plan_id, database = _seed(root / "run", entry_count)
            layout = root / "layout.json"
            _layout_for(database, plan_id, scenario, layout)
            before = _database_and_wal_bytes(database)
            started = time.perf_counter()
            metrics: dict[str, int] = {}
            result = apply_layout(
                root / "run", plan_id, layout, performance_metrics=metrics
            )
            apply_seconds = time.perf_counter() - started
            database_growth = _database_and_wal_bytes(database) - before
            started = time.perf_counter()
            rebuild_active_projection(root / "run", plan_id)
            reconstruction_seconds = time.perf_counter() - started
            connection = sqlite3.connect(database, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            started = time.perf_counter()
            finalize_layout_projection(connection, plan_id, "benchmark")
            finalization_seconds = time.perf_counter() - started
            connection.rollback()
            connection.close()
            sample = {
                "apply_seconds": apply_seconds,
                "affected_baseline_rows_read": int(result["affected_entry_count"]),
                "sqlite_rows_read": metrics["sqlite_rows_read"],
                "sqlite_rows_written": metrics["sqlite_rows_written"],
                "sqlite_vm_steps_lower_bound": metrics["sqlite_vm_steps_lower_bound"],
                "database_growth_bytes": database_growth,
                "total_entries": int(result["total_entry_count"]),
                "affected_entries": int(result["affected_entry_count"]),
                "changed_entries": int(result["changed_entry_count"]),
                "reconstruction_seconds": reconstruction_seconds,
                "finalization_seconds": finalization_seconds,
            }
        sample["peak_memory_bytes"] = memory.peak_bytes
        samples.append(sample)
    numeric = samples[0].keys()
    median = {
        name: statistics.median(float(sample[name]) for sample in samples)
        for name in numeric
    }
    p95 = {
        name: _percentile([float(sample[name]) for sample in samples], 0.95)
        for name in numeric
    }
    return {
        "entry_count": entry_count,
        "scenario": scenario,
        "repetitions": repetitions,
        "median": median,
        "p95": p95,
        "samples": samples,
    }


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sizes", type=int, nargs="+", default=[10_000, 100_000, 1_000_000]
    )
    parser.add_argument(
        "--scenarios",
        nargs="+",
        choices=("one-entry", "subtree", "root-wide"),
        default=["one-entry", "subtree", "root-wide"],
    )
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--output", type=Path)
    options = parser.parse_args(arguments)
    if options.repetitions < 1 or any(size < 1 for size in options.sizes):
        parser.error("sizes and repetitions must be positive")
    result = {
        "schema_version": 1,
        "cases": [
            run_case(size, scenario, options.repetitions)
            for size in options.sizes
            for scenario in options.scenarios
        ],
    }
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if options.output is not None:
        options.output.parent.mkdir(parents=True, exist_ok=True)
        options.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
