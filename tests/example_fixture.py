from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import TypedDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "example_source"
OUTPUT_ROOT = PROJECT_ROOT / "example_output"
MANIFEST_PATH = PROJECT_ROOT / "example_fixture_manifest.json"


class FixtureManifest(TypedDict):
    schema_version: int
    source_modified_time_ns: dict[str, int]
    unreadable_source_paths: list[str]


def load_fixture_manifest() -> FixtureManifest:
    manifest: FixtureManifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if manifest["schema_version"] != 1:
        raise ValueError(
            f"Unsupported development fixture schema: {manifest['schema_version']}"
        )
    return manifest


def copy_prepared_source(destination: Path) -> Path:
    """Copy the immutable source fixture and reconstruct non-Git metadata."""
    shutil.copytree(SOURCE_ROOT, destination, symlinks=True)
    manifest = load_fixture_manifest()
    for relative_path, modified_time_ns in manifest["source_modified_time_ns"].items():
        os.utime(
            destination / relative_path,
            ns=(modified_time_ns, modified_time_ns),
            follow_symlinks=False,
        )
    for relative_path in manifest["unreadable_source_paths"]:
        (destination / relative_path).chmod(0)
    return destination
