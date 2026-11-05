"""Dot-notation JSON path resolution, shared by the client (to extract
normalized fields and pagination tokens) and the drift detector (to notice
when a path that used to resolve no longer does)."""
from __future__ import annotations

from typing import Any

_MISSING = object()


def get_path(obj: Any, path: str) -> Any:
    """Resolve 'a.b.c' against nested dicts. Returns the sentinel _MISSING
    (not None -- None is a legitimate value) if any segment is absent or the
    current value isn't a dict."""
    current = obj
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return _MISSING
    return current


def is_missing(value: Any) -> bool:
    return value is _MISSING
