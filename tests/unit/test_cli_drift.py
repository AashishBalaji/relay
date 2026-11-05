import json

import httpx
from click.testing import CliRunner

import relay.cli.main as cli_main
from relay.cli.main import cli
from relay.client.connector_client import ConnectorClient


def _patch_client(monkeypatch, handler):
    """Swaps ConnectorClient's real httpx.Client for a mock-transport one,
    so the drift-check CLI command can be tested without real network --
    live network behavior is already proven by tests/contract."""

    real_init = ConnectorClient.__init__

    def fake_init(self, manifest, credentials=None, http_client=None, **kwargs):
        http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=manifest.base_url)
        real_init(self, manifest, credentials=credentials, http_client=http_client, **kwargs)

    monkeypatch.setattr(cli_main.ConnectorClient, "__init__", fake_init)


def test_drift_check_clean_exits_zero(tmp_path, monkeypatch):
    def handler(request):
        return httpx.Response(200, json=[{"id": 1, "name": "a"}])

    _patch_client(monkeypatch, handler)
    manifest_path = tmp_path / "m.yaml"
    manifest_path.write_text(
        "name: demo\nversion: '1.0.0'\nbase_url: https://api.test\n"
        "endpoints:\n  - name: list\n    path: /items\n    fields:\n"
        "      id:\n        source_path: id\n      name:\n        source_path: name\n"
    )
    result = CliRunner().invoke(cli, ["drift-check", str(manifest_path)])
    assert result.exit_code == 0
    assert "No drift detected" in result.output


def test_drift_check_unsafe_drift_exits_nonzero(tmp_path, monkeypatch):
    def handler(request):
        return httpx.Response(200, json=[{"id": 1}])  # "name" missing, nothing similar nearby

    _patch_client(monkeypatch, handler)
    manifest_path = tmp_path / "m.yaml"
    manifest_path.write_text(
        "name: demo\nversion: '1.0.0'\nbase_url: https://api.test\n"
        "endpoints:\n  - name: list\n    path: /items\n    fields:\n"
        "      id:\n        source_path: id\n      name:\n        source_path: name\n"
    )
    result = CliRunner().invoke(cli, ["drift-check", str(manifest_path)])
    assert result.exit_code != 0
    assert "Needs human review" in result.output


def test_drift_check_apply_safe_patches_writes_file(tmp_path, monkeypatch):
    def handler(request):
        return httpx.Response(200, json=[{"id": 1, "full_name": "a"}])  # "name" -> "full_name"

    _patch_client(monkeypatch, handler)
    manifest_path = tmp_path / "m.yaml"
    manifest_path.write_text(
        "name: demo\nversion: '1.0.0'\nbase_url: https://api.test\n"
        "endpoints:\n  - name: list\n    path: /items\n    fields:\n"
        "      id:\n        source_path: id\n      name:\n        source_path: name\n"
    )
    result = CliRunner().invoke(cli, ["drift-check", str(manifest_path), "--apply-safe-patches"])
    assert "Applied 1 safe patch" in result.output
    updated = manifest_path.read_text()
    assert "full_name" in updated


def test_drift_check_scans_only_endpoints_with_no_path_params_by_default(tmp_path, monkeypatch):
    seen_paths = []

    def handler(request):
        seen_paths.append(request.url.path)
        return httpx.Response(200, json=[])

    _patch_client(monkeypatch, handler)
    manifest_path = tmp_path / "m.yaml"
    manifest_path.write_text(
        "name: demo\nversion: '1.0.0'\nbase_url: https://api.test\n"
        "endpoints:\n"
        "  - name: list\n    path: /items\n    fields: {}\n"
        "  - name: get_one\n    path: /items/{id}\n    fields: {}\n"
    )
    result = CliRunner().invoke(cli, ["drift-check", str(manifest_path)])
    assert result.exit_code == 0
    assert seen_paths == ["/items"]  # get_one was skipped -- it needs a path param we didn't give


def test_drift_check_path_params_json_targets_a_specific_endpoint(tmp_path, monkeypatch):
    seen_paths = []

    def handler(request):
        seen_paths.append(request.url.path)
        return httpx.Response(200, json=[])

    _patch_client(monkeypatch, handler)
    manifest_path = tmp_path / "m.yaml"
    manifest_path.write_text(
        "name: demo\nversion: '1.0.0'\nbase_url: https://api.test\n"
        "endpoints:\n  - name: get_one\n    path: /items/{item_id}\n    fields: {}\n"
    )
    params = json.dumps({"get_one": {"item_id": 42}})
    result = CliRunner().invoke(
        cli, ["drift-check", str(manifest_path), "-e", "get_one", "--path-params-json", params]
    )
    assert result.exit_code == 0
    assert seen_paths == ["/items/42"]
