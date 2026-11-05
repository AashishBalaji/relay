from click.testing import CliRunner

from relay.cli.main import cli


def test_new_connector_scaffolds_manifest(tmp_path):
    runner = CliRunner()
    result = runner.invoke(cli, ["new-connector", "demo", "--dir", str(tmp_path)])
    assert result.exit_code == 0
    manifest_path = tmp_path / "demo" / "manifest.yaml"
    assert manifest_path.exists()
    assert "name: demo" in manifest_path.read_text()


def test_new_connector_refuses_to_overwrite(tmp_path):
    runner = CliRunner()
    runner.invoke(cli, ["new-connector", "demo", "--dir", str(tmp_path)])
    result = runner.invoke(cli, ["new-connector", "demo", "--dir", str(tmp_path)])
    assert result.exit_code != 0
    assert "already exists" in result.output


def test_validate_valid_manifest(tmp_path):
    runner = CliRunner()
    runner.invoke(cli, ["new-connector", "demo", "--dir", str(tmp_path)])
    manifest_path = tmp_path / "demo" / "manifest.yaml"
    result = runner.invoke(cli, ["validate", str(manifest_path)])
    assert result.exit_code == 0
    assert "OK: demo" in result.output


def test_validate_invalid_manifest_reports_problems(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: demo\nversion: not-semver\nbase_url: https://example.com\nendpoints: []\n")
    runner = CliRunner()
    result = runner.invoke(cli, ["validate", str(bad)])
    assert result.exit_code != 0
    assert "INVALID" in result.output


def test_validate_missing_file(tmp_path):
    runner = CliRunner()
    result = runner.invoke(cli, ["validate", str(tmp_path / "nope.yaml")])
    assert result.exit_code != 0
