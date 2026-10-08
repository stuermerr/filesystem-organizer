from __future__ import annotations

import sys
from typing import Any, Protocol, Self

from tqdm import tqdm


class ProgressReporter(Protocol):
    """Report sequential process phases without coupling work to a terminal UI."""

    def start(
        self, description: str, total: int | None = None, unit: str = "operations"
    ) -> None: ...

    def advance(self, amount: int = 1) -> None: ...

    def complete(self) -> None: ...


class NullProgressReporter:
    """Default reporter for library callers that do not need terminal output."""

    def start(
        self, description: str, total: int | None = None, unit: str = "operations"
    ) -> None:
        pass

    def advance(self, amount: int = 1) -> None:
        pass

    def complete(self) -> None:
        pass


class TqdmProgressReporter:
    """Render sequential CLI phases on stderr while leaving stdout machine-readable."""

    def __init__(self) -> None:
        self._bar: tqdm[Any] | None = None
        self._description: str | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def start(
        self, description: str, total: int | None = None, unit: str = "operations"
    ) -> None:
        self.complete()
        self._description = description
        if total is None:
            print(f"{description}...", file=sys.stderr, flush=True)
            return
        self._bar = tqdm(total=total, desc=description, unit=unit)

    def advance(self, amount: int = 1) -> None:
        if self._bar is not None:
            self._bar.update(amount)

    def complete(self) -> None:
        had_bar = self._bar is not None
        if self._bar is not None:
            self._bar.close()
            self._bar = None
        if self._description is not None and not had_bar:
            print(f"{self._description} done", file=sys.stderr, flush=True)
            self._description = None
        self._description = None

    def close(self) -> None:
        if self._bar is not None:
            self._bar.close()
            self._bar = None
        self._description = None
