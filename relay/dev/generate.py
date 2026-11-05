"""Generates synthetic response items straight from a manifest's own field
declarations -- no fixtures to hand-maintain, no real upstream needed. This
is what makes the local mock server (relay/dev/mock_server.py) possible: it
serves data shaped exactly like what the manifest claims the real API
returns, which is also exactly what the client and drift detector expect.
"""
from __future__ import annotations

from typing import Any

from relay.manifest.schema import EndpointSpec, FieldMapping


def set_path(container: dict, path: str, value: Any) -> None:
    """The write-side counterpart of relay.client.paths.get_path: creates
    whatever nested dicts 'a.b.c' needs along the way."""
    parts = path.split(".")
    current = container
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


def _synthetic_value(field_name: str, mapping: FieldMapping, index: int) -> Any:
    hint = mapping.type_hint
    if hint == "integer":
        return index
    if hint == "number":
        return round(index + 0.5, 2)
    if hint == "boolean":
        return index % 2 == 0
    if hint == "array":
        return [f"{field_name}_item_{i}" for i in range(1, 3)]
    if hint == "object":
        return {"nested": f"{field_name}_value_{index}"}
    # "string" or unspecified -- the common case, and a safe default since
    # it's always valid JSON and never confused with a "field is missing"
    # sentinel the way None could be.
    return f"{field_name}_{index}"


def build_synthetic_item(
    fields: dict[str, FieldMapping], index: int, rename: tuple[str, str] | None = None
) -> dict:
    """Builds one raw (un-normalized) item matching an endpoint's declared
    fields, with synthetic-but-typed values at each field's source_path.
    `rename`, if given, is (field_name, new_last_segment) -- it renames the
    LAST segment of that one field's source_path in the generated output,
    simulating exactly the kind of upstream drift the drift detector is
    built to catch, without needing a real API to actually break."""
    item: dict = {}
    for field_name, mapping in fields.items():
        path = mapping.source_path
        if rename is not None and rename[0] == field_name:
            parts = path.split(".")
            parts[-1] = rename[1]
            path = ".".join(parts)
        set_path(item, path, _synthetic_value(field_name, mapping, index))
    return item


def build_synthetic_page(
    endpoint: EndpointSpec, page_index: int, page_size: int, rename: tuple[str, str] | None = None
) -> list[dict]:
    """Builds one page's worth of synthetic items for an endpoint, with
    globally-increasing indices so items across pages are distinguishable
    (item 0..page_size-1 on page 0, page_size..2*page_size-1 on page 1, …)."""
    start = page_index * page_size
    return [
        build_synthetic_item(endpoint.fields, start + i, rename=rename) for i in range(page_size)
    ]
