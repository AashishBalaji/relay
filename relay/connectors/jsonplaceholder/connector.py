"""Thin wiring for the jsonplaceholder connector -- all the real logic
lives in the framework (relay.client) and the manifest. This file exists
only so a caller doesn't have to know the manifest's file path."""
from __future__ import annotations

from pathlib import Path

from relay.client.connector_client import ConnectorClient
from relay.manifest.loader import load_manifest

_MANIFEST_PATH = Path(__file__).parent / "manifest.yaml"


def get_client() -> ConnectorClient:
    manifest = load_manifest(_MANIFEST_PATH)
    return ConnectorClient(manifest)  # no auth needed -- public API
