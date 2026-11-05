import signal
import subprocess
import sys
import time

from click.testing import CliRunner

from relay.cli.main import cli


def test_lint_clean_manifest_exits_zero(tmp_path):
    manifest_path = tmp_path / "m.yaml"
    manifest_path.write_text(
        "name: demo\nversion: '1.0.0'\nbase_url: https://api.test\n"
        "endpoints:\n  - name: list\n    path: /items\n    fields:\n"
        "      id:\n        source_path: id\n        required: true\n"
    )
    result = CliRunner().invoke(cli, ["lint", str(manifest_path)])
    assert result.exit_code == 0
    assert "OK" in result.output


def test_lint_error_finding_exits_nonzero(tmp_path):
    manifest_path = tmp_path / "m.yaml"
    manifest_path.write_text(
        "name: demo\nversion: '1.0.0'\nbase_url: https://api.test\n"
        "rate_limit_per_window: 0\n"
        "endpoints:\n  - name: list\n    path: /items\n    fields:\n"
        "      id:\n        source_path: id\n        required: true\n"
    )
    result = CliRunner().invoke(cli, ["lint", str(manifest_path)])
    assert result.exit_code != 0
    assert "ERROR" in result.output


def test_lint_warning_only_exits_zero(tmp_path):
    manifest_path = tmp_path / "m.yaml"
    manifest_path.write_text(
        "name: demo\nversion: '1.0.0'\nbase_url: https://api.test\n"
        "max_retries: 50\n"
        "endpoints:\n  - name: list\n    path: /items\n    fields:\n"
        "      id:\n        source_path: id\n        required: true\n"
    )
    result = CliRunner().invoke(cli, ["lint", str(manifest_path)])
    assert result.exit_code == 0
    assert "WARNING" in result.output


def test_base_url_override_warns_when_host_differs_from_manifest(tmp_path):
    """--base-url still carries the manifest's real credentials, so pointing
    it somewhere other than the manifest's own host (a copy-paste mistake,
    not necessarily `relay dev`'s mock server) must say so loudly."""
    manifest_path = tmp_path / "m.yaml"
    manifest_path.write_text(
        "name: demo\nversion: '1.0.0'\nbase_url: https://api.original.test\n"
        "max_retries: 0\n"
        "endpoints:\n  - name: get_one\n    path: /items\n    fields:\n"
        "      id:\n        source_path: id\n        required: true\n"
    )
    result = CliRunner().invoke(
        cli, ["test", str(manifest_path), "get_one", "--base-url", "http://127.0.0.1:1"]
    )
    assert "WARNING" in result.output
    assert "differs from manifest" in result.output


def test_dev_command_starts_and_stops_gracefully_on_sigterm(tmp_path):
    """Exercises the actual `relay dev` CLI subprocess end to end -- not
    just the underlying MockServer class -- including that a SIGTERM
    (not just Ctrl+C/KeyboardInterrupt) stops it cleanly."""
    manifest_path = tmp_path / "m.yaml"
    manifest_path.write_text(
        "name: demo\nversion: '1.0.0'\nbase_url: https://unused.test\n"
        "endpoints:\n  - name: list\n    path: /items\n    fields:\n"
        "      id:\n        source_path: id\n        required: true\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "relay.cli.main", "dev", str(manifest_path), "--port", "0"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    banner = ""
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            line = proc.stdout.readline()
            banner += line
            if "running at" in line:
                break
        assert "Mock server for 'demo' running at http://127.0.0.1:" in banner
    finally:
        proc.send_signal(signal.SIGTERM)
        remaining_output, _ = proc.communicate(timeout=5)
    assert proc.returncode == 0
    assert "Stopping mock server." in (banner + remaining_output)
