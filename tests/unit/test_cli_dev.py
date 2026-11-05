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
