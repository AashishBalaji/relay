import httpx

from relay.agents.drift_detector import apply_safe_patches, scan_connector
from relay.client.connector_client import ConnectorClient
from relay.manifest.schema import ConnectorManifest, EndpointSpec, FieldMapping


def make_manifest(endpoint):
    return ConnectorManifest(
        name="demo", version="1.0.0", base_url="https://api.test", endpoints=[endpoint]
    )


def _client(endpoint, payload):
    def handler(request):
        return httpx.Response(200, json=payload)

    manifest = make_manifest(endpoint)
    return ConnectorClient(
        manifest, http_client=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.test")
    )


def test_no_drift_when_nothing_missing():
    endpoint = EndpointSpec(name="list", path="/items", fields={"id": FieldMapping(source_path="id")})
    client = _client(endpoint, [{"id": 1}])
    report = scan_connector(client)
    assert not report.has_drift
    assert "No drift detected" in report.to_markdown()


def test_nested_field_rename_detected_as_safe():
    endpoint = EndpointSpec(
        name="list_issues", path="/items",
        fields={"author_login": FieldMapping(source_path="user.login", required=True)},
    )
    # "user" renamed to "user_info" upstream (a common, close rename --
    # unlike an unrelated word swap, this clears the similarity threshold),
    # same leaf key "login" still there underneath it.
    client = _client(endpoint, [{"id": 1, "user_info": {"login": "octocat"}}])
    report = scan_connector(client)
    assert report.has_drift
    drift = report.drifts[0]
    assert drift.confidence == "safe"
    assert drift.suggested_path == "user_info.login"


def test_flat_field_rename_detected():
    endpoint = EndpointSpec(
        name="list", path="/items",
        fields={"title": FieldMapping(source_path="title", required=True)},
    )
    client = _client(endpoint, [{"id": 1, "titel": "typo-ish rename"}])  # close but not identical
    report = scan_connector(client)
    assert report.has_drift
    assert report.drifts[0].suggested_path == "titel"


def test_field_removed_outright_is_unsafe_with_no_suggestion():
    endpoint = EndpointSpec(
        name="list", path="/items",
        fields={"legacy_flag": FieldMapping(source_path="legacy_flag", required=True)},
    )
    client = _client(endpoint, [{"id": 1, "name": "x"}])  # nothing remotely similar
    report = scan_connector(client)
    assert report.has_drift
    drift = report.drifts[0]
    assert drift.confidence == "unsafe"
    assert drift.suggested_path is None


def test_endpoint_call_failure_reported_as_unreachable_not_unsafe():
    """A connectivity failure (auth error, 5xx, network blip) is a
    different situation from a renamed field -- a consumer paging on
    results needs to tell "connector is down" from "schema changed",
    so this is its own classification, not folded into unsafe_drifts."""
    def handler(request):
        return httpx.Response(500)

    endpoint = EndpointSpec(name="list", path="/items", fields={}, pagination=None)
    manifest = ConnectorManifest(
        name="demo", version="1.0.0", base_url="https://api.test", max_retries=0, endpoints=[endpoint]
    )
    client = ConnectorClient(
        manifest, http_client=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.test")
    )
    report = scan_connector(client)
    assert report.has_drift
    assert len(report.unreachable_endpoints) == 1
    assert report.unsafe_drifts == []
    assert "endpoint call failed" in report.unreachable_endpoints[0].reason


def test_short_key_rename_never_classified_safe():
    """difflib similarity on short keys is noisy ("id" vs "uid" scores
    0.8) -- a rename guess on a key shorter than the safety floor must
    never be auto-patchable, however high its similarity score."""
    endpoint = EndpointSpec(
        name="list", path="/items",
        fields={"id": FieldMapping(source_path="id", required=True)},
    )
    client = _client(endpoint, [{"uid": 1, "name": "x"}])  # "id" -> "uid", short-key coincidence
    report = scan_connector(client)
    assert report.has_drift
    assert report.drifts[0].confidence == "unsafe"


def test_apply_safe_patches_updates_source_path_only_for_safe_drifts():
    endpoint = EndpointSpec(
        name="list_issues", path="/items",
        fields={
            "author_login": FieldMapping(source_path="user.login", required=True),
            "legacy_flag": FieldMapping(source_path="legacy_flag", required=True),
        },
    )
    client = _client(
        endpoint, [{"id": 1, "user_info": {"login": "octocat"}}]  # legacy_flag missing, no close match
    )
    manifest = client.manifest
    report = scan_connector(client)
    patched = apply_safe_patches(manifest, report)

    assert patched.endpoint("list_issues").fields["author_login"].source_path == "user_info.login"
    # the unsafe one must be untouched
    assert patched.endpoint("list_issues").fields["legacy_flag"].source_path == "legacy_flag"
    # original manifest must be untouched (apply_safe_patches never mutates its input)
    assert manifest.endpoint("list_issues").fields["author_login"].source_path == "user.login"


def test_markdown_report_lists_safe_and_unsafe_sections():
    endpoint = EndpointSpec(
        name="list_issues", path="/items",
        fields={"author_login": FieldMapping(source_path="user.login", required=True)},
    )
    client = _client(endpoint, [{"id": 1, "user_info": {"login": "octocat"}}])
    report = scan_connector(client)
    markdown = report.to_markdown()
    assert "Safe to auto-patch" in markdown
    assert "user_info.login" in markdown
