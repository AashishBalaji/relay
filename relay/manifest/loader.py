"""Loads a connector manifest from YAML/JSON on disk and validates it,
turning pydantic's validation errors into a flat list of human-readable
problems rather than a raw ValidationError traceback -- the CLI's
`relay validate` command and the drift-report generator both want that
flat list, not a pydantic exception object.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml
from pydantic import ValidationError

from relay.manifest.schema import ConnectorManifest


class ManifestError(Exception):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def _read_raw(path: Path) -> dict:
    text = path.read_text()
    if path.suffix in (".yaml", ".yml"):
        return yaml.safe_load(text)
    if path.suffix == ".json":
        return json.loads(text)
    raise ManifestError([f"unsupported manifest extension: {path.suffix!r} (use .yaml or .json)"])


def load_manifest(path: str | Path) -> ConnectorManifest:
    path = Path(path)
    if not path.exists():
        raise ManifestError([f"manifest file not found: {path}"])
    raw = _read_raw(path)
    try:
        return ConnectorManifest.model_validate(raw)
    except ValidationError as exc:
        problems = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise ManifestError(problems) from exc
