from __future__ import annotations

import errno
import fcntl
from pathlib import Path

import pytest

from filesystem_organizer import linux_filesystem


def test_nonblocking_lock_refuses_a_second_holder(tmp_path: Path) -> None:
    lock = tmp_path / "locks" / "run.lock"
    with (
        linux_filesystem.nonblocking_lock(lock),
        pytest.raises(linux_filesystem.LinuxFilesystemError, match="already holds"),
        linux_filesystem.nonblocking_lock(lock),
    ):
        pass


def test_destination_lock_path_is_stable_and_parent_scoped(tmp_path: Path) -> None:
    destination = tmp_path / "materialized" / "result"
    lock = linux_filesystem.destination_lock_path(destination)

    assert lock.parent == destination.parent
    assert lock.name.startswith(".filesystem-organizer-lock-")
    assert lock == linux_filesystem.destination_lock_path(destination)


def test_clone_unavailable_removes_partial_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(b"payload")

    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EOPNOTSUPP, "unsupported")

    monkeypatch.setattr(fcntl, "ioctl", unavailable)

    assert (
        linux_filesystem.try_native_clone(source, destination)
        is linux_filesystem.CloneResult.UNAVAILABLE
    )
    assert not destination.exists()


def test_sync_filesystem_uses_injected_linux_system(tmp_path: Path) -> None:
    calls: list[int] = []

    class FakeSystem:
        def syncfs(self, descriptor: int) -> None:
            calls.append(descriptor)

        def rename_no_replace(self, source: bytes, destination: bytes) -> None:
            raise AssertionError("not used")

    linux_filesystem.sync_filesystem(tmp_path, system=FakeSystem())

    assert calls


def test_hard_link_probe_leaves_no_artifacts(tmp_path: Path) -> None:
    linux_filesystem.probe_hard_link_support(tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_destination_probe_exercises_no_replace_and_cleans_up(tmp_path: Path) -> None:
    capabilities = linux_filesystem.probe_destination_capabilities(tmp_path)

    assert capabilities.writable
    assert capabilities.supports_atomic_no_replace
    assert list(tmp_path.iterdir()) == []


def test_publish_refuses_existing_destination_with_injected_system(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    destination = tmp_path / "destination"
    staging.mkdir()

    class ExistingDestination:
        def syncfs(self, descriptor: int) -> None:
            raise AssertionError("not used")

        def rename_no_replace(self, source: bytes, destination: bytes) -> None:
            raise FileExistsError(errno.EEXIST, "exists")

    with pytest.raises(linux_filesystem.LinuxFilesystemError, match="already exists"):
        linux_filesystem.publish_directory_no_replace(
            staging, destination, system=ExistingDestination()
        )
