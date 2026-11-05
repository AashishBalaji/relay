"""Thin wiring for the GitHub connector. Unlike jsonplaceholder, this one
needs a real OAuth2 access token at call time -- Relay's framework never
runs the OAuth2 dance itself (see relay.client.auth), it just expects a
token the caller already obtained."""
from __future__ import annotations

from pathlib import Path

from relay.client.connector_client import ConnectorClient
from relay.manifest.loader import load_manifest

_MANIFEST_PATH = Path(__file__).parent / "manifest.yaml"


def get_client(access_token: str) -> ConnectorClient:
    manifest = load_manifest(_MANIFEST_PATH)
    return ConnectorClient(manifest, credentials={"access_token": access_token})
