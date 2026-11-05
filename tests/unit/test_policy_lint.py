from relay.dev.policy_lint import lint_manifest
from relay.manifest.schema import ApiKeyAuth, ConnectorManifest, EndpointSpec, FieldMapping


def _manifest(**overrides):
    endpoint = overrides.pop(
        "endpoint",
        EndpointSpec(name="list", path="/items", fields={"id": FieldMapping(source_path="id")}),
    )
    defaults = dict(name="demo", version="1.0.0", base_url="https://api.test", endpoints=[endpoint])
    defaults.update(overrides)
    return ConnectorManifest(**defaults)


def test_clean_manifest_has_no_findings():
    assert lint_manifest(_manifest()) == []


def test_zero_rate_limit_is_an_error():
    findings = lint_manifest(_manifest(rate_limit_per_window=0))
    assert any(f.severity == "error" and f.check == "rate_limit" for f in findings)


def test_very_high_rate_limit_is_a_warning_not_an_error():
    findings = lint_manifest(_manifest(rate_limit_per_window=5000))
    assert any(f.severity == "warning" and f.check == "rate_limit" for f in findings)
    assert not any(f.severity == "error" for f in findings)


def test_very_high_retries_is_a_warning():
    findings = lint_manifest(_manifest(max_retries=50))
    assert any(f.check == "retries" for f in findings)


def test_endpoint_with_no_required_fields_is_flagged():
    endpoint = EndpointSpec(
        name="list", path="/items", fields={"id": FieldMapping(source_path="id", required=False)}
    )
    findings = lint_manifest(_manifest(endpoint=endpoint))
    assert any(f.check == "required_fields" for f in findings)


def test_endpoint_with_a_required_field_is_not_flagged():
    endpoint = EndpointSpec(
        name="list", path="/items", fields={"id": FieldMapping(source_path="id", required=True)}
    )
    assert not any(f.check == "required_fields" for f in lint_manifest(_manifest(endpoint=endpoint)))


def test_endpoint_with_zero_fields_is_an_error_not_a_warning():
    """Worse than 'no required fields': there's no field mapping at all,
    so neither the client nor the drift detector has anything to check --
    this deserves a harder failure than the no-required-fields warning."""
    endpoint = EndpointSpec(name="list", path="/items", fields={})
    findings = lint_manifest(_manifest(endpoint=endpoint))
    matches = [f for f in findings if f.check == "required_fields"]
    assert len(matches) == 1
    assert matches[0].severity == "error"


def test_suspicious_value_in_auth_is_flagged_as_embedded_secret():
    auth = ApiKeyAuth(name="Authorization", location="header", value_prefix="Bearer eyFAKE")
    findings = lint_manifest(_manifest(auth=auth))
    assert any(f.severity == "error" and f.check == "embedded_secret" for f in findings)


def test_ordinary_auth_config_is_not_flagged():
    auth = ApiKeyAuth(name="X-Api-Key", location="header")
    assert lint_manifest(_manifest(auth=auth)) == []
