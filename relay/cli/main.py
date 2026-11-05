"""The `relay` CLI: new-connector scaffolds a manifest, validate checks one,
test makes a real (or recorded) call against one endpoint. Kept thin on
purpose -- all the actual logic lives in relay.manifest / relay.client so it
stays unit-testable without going through the CLI at all.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import click
import yaml

from relay.agents.drift_detector import apply_safe_patches, scan_connector
from relay.client.connector_client import (
    ConnectorClient,
    MissingRequiredFieldsError,
    UpstreamConnectionError,
    UpstreamError,
)
from relay.manifest.loader import ManifestError, load_manifest

_TEMPLATE = """\
name: {name}
version: "0.1.0"
base_url: "https://api.example.com"
auth:
  type: none
rate_limit_per_window: 10
rate_limit_window_seconds: 1.0
max_retries: 3
endpoints:
  - name: list_things
    method: GET
    path: /things
    fields:
      id:
        source_path: id
        required: true
      name:
        source_path: name
        required: true
"""


def _resolve_credentials(manifest_name: str, explicit: tuple[str, ...]) -> dict:
    credentials = dict(kv.split("=", 1) for kv in explicit)
    # Fall back to env vars named RELAY_<CONNECTOR>_<KEY> so a secret never
    # has to be typed on the command line.
    for key in ("api_key", "access_token", "username", "password"):
        env_name = f"RELAY_{manifest_name.upper()}_{key.upper()}"
        if key not in credentials and env_name in os.environ:
            credentials[key] = os.environ[env_name]
    return credentials


@click.group()
def cli():
    """Relay: a manifest-driven connector framework."""


@cli.command("new-connector")
@click.argument("name")
@click.option(
    "--dir", "out_dir", default="relay/connectors", help="Where to scaffold the connector"
)
def new_connector(name: str, out_dir: str) -> None:
    """Scaffold a new connector manifest at <out_dir>/<name>/manifest.yaml."""
    target_dir = Path(out_dir) / name
    target_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = target_dir / "manifest.yaml"
    if manifest_path.exists():
        raise click.ClickException(f"{manifest_path} already exists")
    manifest_path.write_text(_TEMPLATE.format(name=name))
    click.echo(f"Scaffolded {manifest_path}")


@cli.command("validate")
@click.argument("manifest_path")
def validate(manifest_path: str):
    """Validate a connector manifest."""
    try:
        manifest = load_manifest(manifest_path)
    except ManifestError as exc:
        click.echo(f"INVALID: {manifest_path}", err=True)
        for problem in exc.problems:
            click.echo(f"  - {problem}", err=True)
        raise SystemExit(1) from exc
    click.echo(f"OK: {manifest.name} v{manifest.version} ({len(manifest.endpoints)} endpoint(s))")


@cli.command("test")
@click.argument("manifest_path")
@click.argument("endpoint_name")
@click.option("--credential", "-c", multiple=True, help="key=value, e.g. -c api_key=abc123")
@click.option("--path-param", "-p", multiple=True, help="key=value for path interpolation")
def test_endpoint(
    manifest_path: str,
    endpoint_name: str,
    credential: tuple[str, ...],
    path_param: tuple[str, ...],
) -> None:
    """Make a real call against one endpoint of a connector and print the
    normalized result -- the fastest way to sanity-check a manifest against
    the real upstream API before wiring up anything else."""
    try:
        manifest = load_manifest(manifest_path)
    except ManifestError as exc:
        click.echo(f"INVALID MANIFEST: {exc}", err=True)
        raise SystemExit(1) from exc

    credentials = _resolve_credentials(manifest.name, credential)
    path_params = dict(kv.split("=", 1) for kv in path_param)

    with ConnectorClient(manifest, credentials=credentials) as client:
        try:
            result = client.call(endpoint_name, path_params=path_params)
        except (UpstreamError, UpstreamConnectionError, MissingRequiredFieldsError) as exc:
            click.echo(f"FAILED: {exc}", err=True)
            raise SystemExit(1) from exc
    click.echo(json.dumps(result, indent=2, default=str))


@cli.command("drift-check")
@click.argument("manifest_path")
@click.option("--credential", "-c", multiple=True, help="key=value, e.g. -c access_token=abc123")
@click.option(
    "--endpoint", "-e", "endpoints", multiple=True,
    help="Scan only this endpoint (repeatable). Default: every endpoint with no {placeholders}.",
)
@click.option(
    "--path-params-json",
    help='JSON mapping endpoint name -> path params, e.g. \'{"get_post": {"post_id": 1}}\'',
)
@click.option(
    "--apply-safe-patches", "apply_patches", is_flag=True,
    help="Write safe fixes back into the manifest file",
)
def drift_check(
    manifest_path: str,
    credential: tuple[str, ...],
    endpoints: tuple[str, ...],
    path_params_json: str | None,
    apply_patches: bool,
) -> None:
    """Scan a connector's endpoints for drift against its manifest, print a
    structured report, and optionally patch the manifest file in place for
    anything confidently identified as a safe rename."""
    try:
        manifest = load_manifest(manifest_path)
    except ManifestError as exc:
        click.echo(f"INVALID MANIFEST: {exc}", err=True)
        raise SystemExit(1) from exc

    endpoint_names = list(endpoints) or [e.name for e in manifest.endpoints if "{" not in e.path]
    path_params_by_endpoint = json.loads(path_params_json) if path_params_json else {}

    credentials = _resolve_credentials(manifest.name, credential)
    with ConnectorClient(manifest, credentials=credentials) as client:
        report = scan_connector(
            client, endpoint_names=endpoint_names, path_params_by_endpoint=path_params_by_endpoint
        )
    click.echo(report.to_markdown())

    if report.safe_drifts and apply_patches:
        patched = apply_safe_patches(manifest, report)
        Path(manifest_path).write_text(
            yaml.safe_dump(patched.model_dump(mode="json"), sort_keys=False)
        )
        click.echo(f"Applied {len(report.safe_drifts)} safe patch(es) to {manifest_path}")

    # CI should fail loudly both on drift that needs a human AND on an
    # endpoint that couldn't be reached at all -- the two are reported
    # separately (see DriftReport) so a consumer can tell "schema changed"
    # from "upstream was down", but both still need attention.
    if report.unsafe_drifts or report.unreachable_endpoints:
        raise SystemExit(1)


if __name__ == "__main__":
    cli()
