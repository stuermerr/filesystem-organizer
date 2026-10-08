"""Small Linux syscall seam used by materialization orchestration.

The module deliberately exposes typed outcomes instead of leaking errno policy
through workflow code.  Its ``LinuxSystem`` dependency is replaceable in tests.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

_FICLONE = 0x4004_9409
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1


class LinuxFilesystemError(Exception):
    """A Linux filesystem operation could not establish its stated guarantee."""


class CloneResult(StrEnum):
    CLONED = "cloned"
    UNAVAILABLE = "unavailable"


class LinuxSystem(Protocol):
    def syncfs(self, descriptor: int) -> None: ...

    def rename_no_replace(self, source: bytes, destination: bytes) -> None: ...


class _CtypesLinuxSystem:
    def __init__(self) -> None:
        self._libc = ctypes.CDLL(None, use_errno=True)

    def syncfs(self, descriptor: int) -> None:
        if self._libc.syncfs(descriptor) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))

    def rename_no_replace(self, source: bytes, destination: bytes) -> None:
        if self._libc.renameat2(
            _AT_FDCWD, source, _AT_FDCWD, destination, _RENAME_NOREPLACE
        ) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))


@dataclass(frozen=True)
class DestinationCapabilities:
    writable: bool
    supports_atomic_no_replace: bool


def _require_linux() -> None:
    platform = os.uname().sysname
    if platform != "Linux":
        raise LinuxFilesystemError(f"Linux filesystem support is unavailable: {platform}")


def destination_lock_path(destination: Path) -> Path:
    """Return the persistent sidecar lock path for one canonical destination."""
    canonical = str(destination.expanduser().resolve(strict=False))
    token = hashlib.sha256(canonical.encode()).hexdigest()[:32]
    return destination.parent / f".filesystem-organizer-lock-{token}"


@contextmanager
def nonblocking_lock(path: Path) -> Iterator[int]:
    """Hold an advisory lock whose lifetime is bound to an open descriptor."""
    _require_linux()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise LinuxFilesystemError(
                f"operation already holds Linux filesystem lock: {path}"
            ) from error
        yield descriptor
    finally:
        os.close(descriptor)


def try_native_clone(source: Path, destination: Path) -> CloneResult:
    """Clone on the destination filesystem, with a narrow fallback allowlist."""
    _require_linux()
    source_fd = os.open(source, os.O_RDONLY | os.O_CLOEXEC)
    try:
        destination_fd = os.open(
            destination,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC,
            0o600,
        )
        try:
            fcntl.ioctl(destination_fd, _FICLONE, source_fd)
        except OSError as error:
            if error.errno in {errno.EOPNOTSUPP, errno.ENOTTY, errno.EXDEV}:
                destination.unlink(missing_ok=True)
                return CloneResult.UNAVAILABLE
            raise LinuxFilesystemError("native clone failed") from error
        finally:
            os.close(destination_fd)
    finally:
        os.close(source_fd)
    return CloneResult.CLONED


def probe_hard_link_support(root: Path) -> None:
    """Prove that in-place protection can create and remove a hard link."""
    _require_linux()
    probe = root / f".filesystem-organizer-hard-link-probe-{uuid.uuid4()}"
    linked = probe.with_name(f"{probe.name}.link")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC, 0o600
        )
        os.close(descriptor)
        descriptor = None
        os.link(probe, linked)
    except OSError as error:
        raise LinuxFilesystemError(
            "hard-link support and write permission are required"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        linked.unlink(missing_ok=True)
        probe.unlink(missing_ok=True)


def probe_destination_capabilities(parent: Path) -> DestinationCapabilities:
    """Exercise writable and no-replace publication semantics with cleanup."""
    _require_linux()
    token = uuid.uuid4().hex
    staging = parent / f".filesystem-organizer-probe-{token}.staging"
    destination = parent / f".filesystem-organizer-probe-{token}.destination"
    try:
        staging.mkdir()
        _CtypesLinuxSystem().rename_no_replace(
            os.fsencode(staging), os.fsencode(destination)
        )
        return DestinationCapabilities(writable=True, supports_atomic_no_replace=True)
    except OSError as error:
        raise LinuxFilesystemError(
            f"destination lacks required write or atomic publication capability: {parent}"
        ) from error
    finally:
        destination.rmdir() if destination.is_dir() else None
        staging.rmdir() if staging.is_dir() else None


def sync_filesystem(path: Path, *, system: LinuxSystem | None = None) -> None:
    """Synchronize the filesystem containing ``path`` once per phase."""
    _require_linux()
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        try:
            (system or _CtypesLinuxSystem()).syncfs(descriptor)
        except OSError as error:
            raise LinuxFilesystemError(f"could not synchronize filesystem for {path}") from error
    finally:
        os.close(descriptor)


def publish_directory_no_replace(
    staging: Path, destination: Path, *, system: LinuxSystem | None = None
) -> None:
    """Atomically publish a completed directory without replacing any path."""
    _require_linux()
    try:
        (system or _CtypesLinuxSystem()).rename_no_replace(
            os.fsencode(staging), os.fsencode(destination)
        )
    except FileExistsError as error:
        raise LinuxFilesystemError(f"destination already exists: {destination}") from error
    except OSError as error:
        raise LinuxFilesystemError("atomic no-replace publication failed") from error
