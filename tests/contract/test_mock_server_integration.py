"""Proves relay/dev isn't just unit-testable in isolation -- it actually
works end to end with the real ConnectorClient and the real drift
detector, the same way a connector author would use it: point `relay
test`/`relay drift-check --base-url` at a running `relay dev` server.
No mocking of the client or detector here; the only thing standing in
for a real upstream is the mock server itself, over a real socket.
"""
from relay.agents.drift_detector import scan_connector
from relay.client.connector_client import ConnectorClient
from relay.dev.mock_server import MockServer
from relay.manifest.schema import ConnectorManifest, EndpointSpec, FieldMapping, PaginationSpec


def _paginated_manifest():
    pagination = PaginationSpec(style="page", param_name="_page", page_size_param="_limit")
    endpoint = EndpointSpec(
        name="list_posts", path="/posts", pagination=pagination,
        fields={
            "id": FieldMapping(source_path="id", required=True, type_hint="integer"),
            "author": FieldMapping(source_path="user.login", required=True, type_hint="string"),
        },
    )
    return ConnectorManifest(
        name="demo", version="1.0.0", base_url="https://unused.example", endpoints=[endpoint]
    )


def test_real_client_against_local_mock_server_traverses_pages():
    manifest = _paginated_manifest()
    with MockServer(manifest, pages=3) as server:
        live_manifest = manifest.model_copy(update={"base_url": server.base_url})
        with ConnectorClient(live_manifest) as client:
            result = client.call("list_posts")
    # 3 pages of 5 synthetic items each, over a real socket, through the
    # real retry/pagination/field-mapping client code.
    assert len(result) == 15
    assert [item["id"] for item in result] == list(range(15))


def test_drift_check_against_mock_server_clean_run_has_no_findings():
    manifest = _paginated_manifest()
    with MockServer(manifest, pages=1) as server:
        live_manifest = manifest.model_copy(update={"base_url": server.base_url})
        with ConnectorClient(live_manifest) as client:
            report = scan_connector(client)
    assert not report.has_drift


def test_simulate_drift_flag_is_actually_caught_by_the_real_drift_detector():
    """The whole point of --simulate-drift: prove locally, with zero real
    upstream involved, that drift-check would have caught a real break."""
    manifest = _paginated_manifest()
    with MockServer(manifest, pages=1, simulate_drift_on="author") as server:
        live_manifest = manifest.model_copy(update={"base_url": server.base_url})
        with ConnectorClient(live_manifest) as client:
            report = scan_connector(client)
    assert report.has_drift
    drifted = [d for d in report.drifts if d.field_name == "author"]
    assert len(drifted) == 1
    assert drifted[0].suggested_path == "user.login_renamed"
