"""The `relay` CLI: new-connector scaffolds a manifest, validate checks one,
test makes a real (or recorded) call against one endpoint. Kept thin on
purpose -- all the actual logic lives in relay.manifest / relay.client so it
stays unit-testable without going through the CLI at all.
"""
from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path
from urllib.parse import urlparse

import click
import yaml

from relay.agents.drift_detector import apply_safe_patches, scan_connector
from relay.client.connector_client import (
    ConnectorClient,
    MissingRequiredFieldsError,
    UpstreamConnectionError,
    UpstreamError,
)
from relay.dev.mock_server import MockServer
from relay.dev.policy_lint import lint_manifest
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


def _warn_if_base_url_host_differs(manifest, base_url: str) -> None:
    """A `--base-url` override still sends the manifest's real credentials
    -- the whole point for `relay dev` is 127.0.0.1, but a copy-paste
    mistake (pasting a real endpoint instead of the mock server's) would
    silently send real credentials to an arbitrary host with no sign
    anything unusual happened. One loud line is cheap insurance."""
    if urlparse(base_url).netloc != urlparse(manifest.base_url).netloc:
        click.echo(
            f"WARNING: --base-url ({base_url}) differs from manifest "
            f"{manifest.name!r}'s own base_url ({manifest.base_url}) -- "
            "its credentials will be sent to this URL instead.",
            err=True,
        )


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
@click.option(
    "--base-url",
    help="Call this base URL instead of the manifest's (e.g. a local `relay dev` mock server), "
    "without editing the manifest file",
)
def test_endpoint(
    manifest_path: str,
    endpoint_name: str,
    credential: tuple[str, ...],
    path_param: tuple[str, ...],
    base_url: str | None,
) -> None:
    """Make a real call against one endpoint of a connector and print the
    normalized result -- the fastest way to sanity-check a manifest against
    the real upstream API before wiring up anything else."""
    try:
        manifest = load_manifest(manifest_path)
    except ManifestError as exc:
        click.echo(f"INVALID MANIFEST: {exc}", err=True)
        raise SystemExit(1) from exc
    if base_url:
        _warn_if_base_url_host_differs(manifest, base_url)
        manifest = manifest.model_copy(update={"base_url": base_url})

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
@click.option(
    "--base-url",
    help="Scan this base URL instead of the manifest's (e.g. a local `relay dev` mock server), "
    "without editing the manifest file",
)
def drift_check(
    manifest_path: str,
    credential: tuple[str, ...],
    endpoints: tuple[str, ...],
    path_params_json: str | None,
    apply_patches: bool,
    base_url: str | None,
) -> None:
    """Scan a connector's endpoints for drift against its manifest, print a
    structured report, and optionally patch the manifest file in place for
    anything confidently identified as a safe rename."""
    try:
        manifest = load_manifest(manifest_path)
    except ManifestError as exc:
        click.echo(f"INVALID MANIFEST: {exc}", err=True)
        raise SystemExit(1) from exc
    if base_url:
        _warn_if_base_url_host_differs(manifest, base_url)
    scan_manifest = manifest.model_copy(update={"base_url": base_url}) if base_url else manifest

    endpoint_names = list(endpoints) or [e.name for e in manifest.endpoints if "{" not in e.path]
    path_params_by_endpoint = json.loads(path_params_json) if path_params_json else {}

    credentials = _resolve_credentials(manifest.name, credential)
    with ConnectorClient(scan_manifest, credentials=credentials) as client:
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


@cli.command("lint")
@click.argument("manifest_path")
def lint(manifest_path: str) -> None:
    """Check a manifest against platform policy (sane rate limits/retries,
    at least one required field per endpoint, no embedded-looking
    credentials) -- the kind of automated check a real App Shop would run
    before admitting a connector, run locally before anyone has to ask."""
    try:
        manifest = load_manifest(manifest_path)
    except ManifestError as exc:
        click.echo(f"INVALID MANIFEST: {exc}", err=True)
        raise SystemExit(1) from exc

    findings = lint_manifest(manifest)
    if not findings:
        click.echo(f"OK: {manifest.name} passes policy lint, no findings")
        return

    errors = [f for f in findings if f.severity == "error"]
    for f in findings:
        click.echo(f"[{f.severity.upper()}] {f.check}: {f.message}")
    if errors:
        raise SystemExit(1)


@cli.command("dev")
@click.argument("manifest_path")
@click.option("--port", default=0, help="Port to bind (default: pick a free one)")
@click.option("--pages", default=2, help="How many pages of synthetic data to serve before 'none'")
@click.option(
    "--simulate-drift", "simulate_drift_on",
    help="Rename this field's last path segment in generated responses, to prove "
    "`relay drift-check --base-url ...` actually catches it",
)
def dev(manifest_path: str, port: int, pages: int, simulate_drift_on: str | None) -> None:
    """Run a local mock server serving synthetic data shaped by this
    manifest's own field declarations -- no fixtures to write, no real
    upstream needed. Point `relay test`/`relay drift-check` at it with
    `--base-url` while building or changing a connector."""
    try:
        manifest = load_manifest(manifest_path)
    except ManifestError as exc:
        click.echo(f"INVALID MANIFEST: {exc}", err=True)
        raise SystemExit(1) from exc

    # A SIGTERM (process-manager stop, `kill <pid>`, a container stop)
    # bypasses KeyboardInterrupt entirely, which would skip the `with`
    # block's close() below -- harmless today only because the serving
    # thread is a daemon and the OS reclaims the socket, but a graceful
    # stop shouldn't depend on that. Registered BEFORE the server starts
    # and anything is printed, so there's no window where a signal sent
    # right after the startup banner appears could still hit Python's
    # default (kill) disposition.
    def _raise_keyboard_interrupt(signum, frame):
        raise KeyboardInterrupt

    previous_handler = signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    try:
        server_kwargs = {"port": port, "pages": pages, "simulate_drift_on": simulate_drift_on}
        with MockServer(manifest, **server_kwargs) as server:
            click.echo(f"Mock server for {manifest.name!r} running at {server.base_url}")
            click.echo(f"  relay test {manifest_path} <endpoint> --base-url {server.base_url}")
            click.echo(f"  relay drift-check {manifest_path} --base-url {server.base_url}")
            if simulate_drift_on:
                click.echo(f"  (simulating a rename of {simulate_drift_on!r} in every response)")
            click.echo("Press Ctrl+C to stop.")
            try:
                while True:
                    time.sleep(0.5)
            except KeyboardInterrupt:
                click.echo("\nStopping mock server.")
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


if __name__ == "__main__":
    cli()
