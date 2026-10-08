from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from blake3 import blake3

from .exact_duplicate_groups import (
    READ_OUTCOME_CHANGED_DURING_READ,
    READ_OUTCOME_CHANGED_SINCE_SCAN,
    READ_OUTCOME_SUCCESSFUL,
    READ_OUTCOME_UNREADABLE,
    IdentityRow,
)

CONTENT_IDENTITY_ALGORITHM = "BLAKE3-256"
CONTENT_IDENTITY_ALGORITHM_VERSION = 1


@dataclass(frozen=True)
class ContentIdentity:
    relative_path: str
    byte_size: int
    digest: str | None
    read_outcome: str


@dataclass(frozen=True)
class FileObservation:
    """Filesystem identity observed before a regular file is streamed."""

    byte_size: int
    modified_ns: int | None
    device: int | None
    inode: int | None

    @classmethod
    def captured(cls, metadata: os.stat_result) -> FileObservation:
        return cls(
            byte_size=metadata.st_size,
            modified_ns=metadata.st_mtime_ns,
            device=metadata.st_dev,
            inode=metadata.st_ino,
        )

    @classmethod
    def persisted(cls, byte_size: int, modified_ns: int | None) -> FileObservation:
        """Reconstruct the subset retained for later snapshot revalidation."""
        return cls(byte_size, modified_ns, None, None)

    def matches(self, metadata: os.stat_result) -> bool:
        return (
            metadata.st_size == self.byte_size
            and metadata.st_mtime_ns == self.modified_ns
            and (self.device is None or metadata.st_dev == self.device)
            and (self.inode is None or metadata.st_ino == self.inode)
        )


def stable_read(
    path: Path,
    before: os.stat_result,
    on_chunk: Callable[[bytes], None] | None = None,
) -> tuple[int, bool]:
    """Stream a file's bytes, proving filesystem stability throughout the read.

    Returns ``(total_bytes_read, stable)``. ``stable`` is False when the
    file's device/inode/size/modification-time identity changed between the
    pre-open observation, the point the descriptor was opened, and the point
    the read finished, or when the streamed byte count disagrees with the
    pre-open size. Raises OSError if the file cannot be opened or read.
    """
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as source:
        opened = os.fstat(source.fileno())
        total = 0
        while chunk := source.read(1024 * 1024):
            total += len(chunk)
            if on_chunk is not None:
                on_chunk(chunk)
        after = os.fstat(source.fileno())

    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    stable = total == before.st_size and all(
        getattr(before, field) == getattr(opened, field) == getattr(after, field)
        for field in stable_fields
    )
    return total, stable


def read_regular_file(path: Path, before: os.stat_result) -> str:
    try:
        _, stable = stable_read(path, before)
    except OSError:
        return READ_OUTCOME_UNREADABLE
    return READ_OUTCOME_SUCCESSFUL if stable else READ_OUTCOME_CHANGED_DURING_READ


def hash_regular_file(
    path: Path,
    relative_path: str,
    observation: FileObservation,
) -> ContentIdentity:
    """Stream a full BLAKE3-256 digest, proving stability throughout the read.

    No sampled or partial hash may establish Exact Duplicate identity: the
    entire file is streamed. A file whose device, inode, size, or modification
    time already disagrees with the supplied inventory observation is reported
    as "changed-since-scan" without being opened; a file that changes between
    the start and end of the read itself is reported as "changed-during-read".
    Neither outcome contributes a proven digest. Device and inode are optional
    for callers performing later revalidation without the original stat record.
    """
    try:
        before = path.lstat()
    except OSError:
        return ContentIdentity(
            relative_path, observation.byte_size, None, READ_OUTCOME_UNREADABLE
        )
    if not observation.matches(before):
        return ContentIdentity(
            relative_path,
            observation.byte_size,
            None,
            READ_OUTCOME_CHANGED_SINCE_SCAN,
        )

    hasher = blake3()

    def _update(chunk: bytes) -> None:
        hasher.update(chunk)

    try:
        total, stable = stable_read(path, before, on_chunk=_update)
    except OSError:
        return ContentIdentity(
            relative_path, observation.byte_size, None, READ_OUTCOME_UNREADABLE
        )
    if not stable:
        return ContentIdentity(
            relative_path, total, None, READ_OUTCOME_CHANGED_DURING_READ
        )
    return ContentIdentity(
        relative_path, total, hasher.hexdigest(), READ_OUTCOME_SUCCESSFUL
    )


def digest_file(path: Path) -> tuple[int, str]:
    """Hash a file's on-disk bytes independently of any in-memory write buffer.

    Raises OSError if the path cannot be opened or read; the caller decides
    how an unreadable file is treated.
    """
    before = path.lstat()
    hasher = blake3()

    def _update(chunk: bytes) -> None:
        hasher.update(chunk)

    total, _ = stable_read(path, before, on_chunk=_update)
    return total, hasher.hexdigest()


def select_identity_rows(
    connection: sqlite3.Connection, run_id: str
) -> list[IdentityRow]:
    """Load every content identity for a run joined with its modified timestamp.

    Returns one ``IdentityRow`` per content_identities row, paired with the
    inventory entry's modification timestamp so Exact Duplicate group
    derivation can rank members. Rows are ordered by relative path.
    """
    rows = connection.execute(
        "SELECT content_identities.relative_path, content_identities.algorithm, "
        "content_identities.algorithm_version, content_identities.byte_size, "
        "content_identities.digest, content_identities.read_outcome, "
        "inventory_entries.modified_ns "
        "FROM content_identities JOIN inventory_entries "
        "ON inventory_entries.run_id = content_identities.run_id "
        "AND inventory_entries.relative_path = content_identities.relative_path "
        "WHERE content_identities.run_id = ? "
        "ORDER BY content_identities.relative_path",
        (run_id,),
    ).fetchall()
    return [
        IdentityRow(
            relative_path=str(row["relative_path"]),
            algorithm=str(row["algorithm"]),
            algorithm_version=int(row["algorithm_version"]),
            byte_size=int(row["byte_size"]),
            digest=str(row["digest"]),
            read_outcome=str(row["read_outcome"]),
            modified_ns=row["modified_ns"],
        )
        for row in rows
    ]
