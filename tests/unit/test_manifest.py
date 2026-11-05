import pytest
from pydantic import ValidationError

from relay.manifest.loader import ManifestError, load_manifest
from relay.manifest.schema import ConnectorManifest, EndpointSpec, FieldMapping, PaginationSpec


def _minimal_endpoint(name="list_things"):
    return EndpointSpec(name=name, path="/things", fields={"id": FieldMapping(source_path="id")})


def test_valid_manifest_constructs():
    m = ConnectorManifest(
        name="demo", version="1.0.0", base_url="https://example.com", endpoints=[_minimal_endpoint()]
    )
    assert m.endpoint("list_things").path == "/things"


def test_version_must_be_semver():
    with pytest.raises(ValidationError):
        ConnectorManifest(
            name="demo", version="not-a-version", base_url="https://example.com",
            endpoints=[_minimal_endpoint()],
        )


def test_duplicate_endpoint_names_rejected():
    with pytest.raises(ValidationError):
        ConnectorManifest(
            name="demo", version="1.0.0", base_url="https://example.com",
            endpoints=[_minimal_endpoint("a"), _minimal_endpoint("a")],
        )


def test_empty_endpoints_rejected():
    with pytest.raises(ValidationError):
        ConnectorManifest(name="demo", version="1.0.0", base_url="https://example.com", endpoints=[])


def test_endpoint_path_must_start_with_slash():
    with pytest.raises(ValidationError):
        EndpointSpec(name="x", path="things")  # missing leading slash


def test_cursor_pagination_requires_next_token_path():
    with pytest.raises(ValidationError):
        PaginationSpec(style="cursor")


def test_page_pagination_requires_param_name():
    with pytest.raises(ValidationError):
        PaginationSpec(style="page")


def test_endpoint_lookup_missing_raises_keyerror():
    m = ConnectorManifest(
        name="demo", version="1.0.0", base_url="https://example.com", endpoints=[_minimal_endpoint()]
    )
    with pytest.raises(KeyError):
        m.endpoint("does_not_exist")


def test_load_manifest_from_yaml(tmp_path):
    manifest_yaml = """
name: demo
version: "1.0.0"
base_url: "https://example.com"
auth:
  type: api_key
  name: Authorization
  value_prefix: "Bearer "
endpoints:
  - name: list_things
    path: /things
    fields:
      id:
        source_path: id
"""
    p = tmp_path / "demo.yaml"
    p.write_text(manifest_yaml)
    m = load_manifest(p)
    assert m.name == "demo"
    assert m.auth.type == "api_key"


def test_load_manifest_missing_file_raises_manifest_error(tmp_path):
    with pytest.raises(ManifestError):
        load_manifest(tmp_path / "nope.yaml")


def test_load_manifest_invalid_yaml_reports_flat_problems(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("name: demo\nversion: 1.0.0\nbase_url: https://example.com\nendpoints: []\n")
    with pytest.raises(ManifestError) as exc_info:
        load_manifest(p)
    assert any("endpoints" in problem for problem in exc_info.value.problems)
