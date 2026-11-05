"""Extracts manifest-declared fields out of a raw response item.

This is the layer both the client (to hand callers a normalized dict instead
of upstream's raw shape) and the drift detector (to notice when a required
field's path stops resolving) read -- one implementation, two consumers,
so drift detection can never silently fall out of sync with what the client
actually does.
"""
from __future__ import annotations

from relay.client.paths import get_path, is_missing
from relay.manifest.schema import FieldMapping


def extract_fields(raw_item: dict, fields: dict[str, FieldMapping]) -> tuple[dict, list[str]]:
    """Returns (normalized_dict, missing_required_field_names).

    A missing optional field is silently dropped from the result. A missing
    required field is reported in missing_required_field_names rather than
    raising -- the caller decides whether that's fatal (the live client
    does; the drift detector doesn't, since one missing field across many
    connectors shouldn't stop the whole drift scan)."""
    result: dict = {}
    missing: list[str] = []
    for name, mapping in fields.items():
        value = get_path(raw_item, mapping.source_path)
        if is_missing(value):
            if mapping.required:
                missing.append(name)
            continue
        result[name] = value
    return result, missing
