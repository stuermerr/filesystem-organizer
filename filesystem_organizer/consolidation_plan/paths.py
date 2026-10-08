from __future__ import annotations


def is_within(path: str, root: str) -> bool:
    """Whether a relative path is the root or one of its descendants."""
    return root == "." or path == root or path.startswith(f"{root}/")
