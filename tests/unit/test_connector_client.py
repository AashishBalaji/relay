import base64

import httpx
import pytest

from relay.client.connector_client import (
    ConnectorClient,
    MissingRequiredFieldsError,
    UpstreamConnectionError,
    UpstreamError,
)
from relay.manifest.schema import (
    ApiKeyAuth,
    BasicAuth,
    ConnectorManifest,
    EndpointSpec,
    FieldMapping,
    PaginationSpec,
)


def make_manifest(endpoints, auth=None, max_retries=3, rate_limit=1000):
    return ConnectorManifest(
        name="t",
        version="1.0.0",
        base_url="https://api.test",
        auth=auth or {"type": "none"},
        endpoints=endpoints,
        max_retries=max_retries,
        rate_limit_per_window=rate_limit,
        rate_limit_window_seconds=1.0,
    )


def client_with(handler, manifest, sleep=lambda s: None):
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url="https://api.test")
    return ConnectorClient(manifest, http_client=http, sleep=sleep)


def test_simple_call_normalizes_fields():
    def handler(request):
        return httpx.Response(200, json=[{"id": 1, "name": "a"}, {"id": 2, "name": "b"}])

    endpoint = EndpointSpec(
        name="list", path="/items",
        fields={"id": FieldMapping(source_path="id"), "name": FieldMapping(source_path="name")},
    )
    client = client_with(handler, make_manifest([endpoint]))
    assert client.call("list") == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]


def test_single_object_response_wrapped_in_list():
    def handler(request):
        return httpx.Response(200, json={"id": 1, "name": "a"})

    endpoint = EndpointSpec(
        name="get_one", path="/items/1",
        fields={"id": FieldMapping(source_path="id")},
    )
    client = client_with(handler, make_manifest([endpoint]))
    assert client.call("get_one") == [{"id": 1}]


def test_path_params_interpolated():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        return httpx.Response(200, json=[])

    endpoint = EndpointSpec(name="get_issue", path="/repos/{owner}/{repo}/issues", fields={})
    client = client_with(handler, make_manifest([endpoint]))
    client.call("get_issue", path_params={"owner": "acme", "repo": "widgets"})
    assert seen["path"] == "/repos/acme/widgets/issues"


def test_missing_required_field_raises_by_default():
    def handler(request):
        return httpx.Response(200, json=[{"id": 1}])  # "name" absent

    endpoint = EndpointSpec(
        name="list", path="/items",
        fields={"id": FieldMapping(source_path="id"), "name": FieldMapping(source_path="name", required=True)},
    )
    client = client_with(handler, make_manifest([endpoint]))
    with pytest.raises(MissingRequiredFieldsError):
        client.call("list")


def test_missing_required_field_can_be_suppressed():
    """The drift detector calls with raise_on_missing_required=False so a
    scan across many connectors doesn't abort on the first drifted field --
    it needs to collect every drift, not stop at the first one."""
    def handler(request):
        return httpx.Response(200, json=[{"id": 1}])

    endpoint = EndpointSpec(
        name="list", path="/items",
        fields={"id": FieldMapping(source_path="id"), "name": FieldMapping(source_path="name", required=True)},
    )
    client = client_with(handler, make_manifest([endpoint]))
    result = client.call("list", raise_on_missing_required=False)
    assert result == [{"id": 1}]


def test_retries_on_500_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(500)
        return httpx.Response(200, json=[{"id": 1}])

    endpoint = EndpointSpec(name="list", path="/items", fields={"id": FieldMapping(source_path="id")})
    sleeps = []
    client = client_with(handler, make_manifest([endpoint], max_retries=5), sleep=sleeps.append)
    assert client.call("list") == [{"id": 1}]
    assert calls["n"] == 3
    assert len(sleeps) == 2  # two failed attempts before success


def test_retries_exhausted_raises_upstream_error():
    def handler(request):
        return httpx.Response(500)

    endpoint = EndpointSpec(name="list", path="/items", fields={})
    client = client_with(handler, make_manifest([endpoint], max_retries=2))
    with pytest.raises(UpstreamError):
        client.call("list")


def test_non_retryable_4xx_fails_immediately():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(404)

    endpoint = EndpointSpec(name="list", path="/items", fields={})
    client = client_with(handler, make_manifest([endpoint], max_retries=5))
    with pytest.raises(UpstreamError):
        client.call("list")
    assert calls["n"] == 1  # no retries burned on a non-retryable error


def test_cursor_pagination_traverses_pages():
    def handler(request):
        cursor = request.url.params.get("cursor")
        if cursor is None:
            return httpx.Response(200, json={"items": [{"id": 1}], "meta": {"next_cursor": "abc"}})
        return httpx.Response(200, json={"items": [{"id": 2}], "meta": {"next_cursor": None}})

    pagination = PaginationSpec(
        style="cursor", param_name="cursor", next_token_path="meta.next_cursor", items_path="items"
    )
    endpoint = EndpointSpec(
        name="list", path="/items", pagination=pagination, fields={"id": FieldMapping(source_path="id")}
    )
    client = client_with(handler, make_manifest([endpoint]))
    assert client.call("list") == [{"id": 1}, {"id": 2}]


def test_page_pagination_stops_on_empty_page():
    def handler(request):
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(200, json=[{"id": page}] if page <= 2 else [])

    pagination = PaginationSpec(style="page", param_name="page")
    endpoint = EndpointSpec(
        name="list", path="/items", pagination=pagination, fields={"id": FieldMapping(source_path="id")}
    )
    client = client_with(handler, make_manifest([endpoint]))
    assert client.call("list") == [{"id": 1}, {"id": 2}]


def test_api_key_header_auth_reaches_upstream_request():
    seen = {}

    def handler(request):
        seen["auth_header"] = request.headers.get("Authorization")
        return httpx.Response(200, json=[])

    endpoint = EndpointSpec(name="list", path="/items", fields={})
    manifest = make_manifest(
        [endpoint], auth=ApiKeyAuth(name="Authorization", location="header", value_prefix="Bearer ")
    )
    client = ConnectorClient(
        manifest,
        credentials={"api_key": "secret123"},
        http_client=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.test"),
    )
    client.call("list")
    assert seen["auth_header"] == "Bearer secret123"


def test_basic_auth_reaches_upstream_request():
    seen = {}

    def handler(request):
        seen["auth_header"] = request.headers.get("Authorization")
        return httpx.Response(200, json=[])

    endpoint = EndpointSpec(name="list", path="/items", fields={})
    manifest = make_manifest([endpoint], auth=BasicAuth())
    client = ConnectorClient(
        manifest,
        credentials={"username": "u", "password": "p"},
        http_client=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.test"),
    )
    client.call("list")
    expected = "Basic " + base64.b64encode(b"u:p").decode()
    assert seen["auth_header"] == expected


def test_close_closes_self_created_http_client():
    endpoint = EndpointSpec(name="list", path="/items", fields={})
    client = ConnectorClient(make_manifest([endpoint]))  # no http_client injected -- owns its own
    assert not client.http.is_closed
    client.close()
    assert client.http.is_closed


def test_close_does_not_close_an_injected_http_client():
    endpoint = EndpointSpec(name="list", path="/items", fields={})
    injected = httpx.Client(base_url="https://api.test")
    client = client_with(lambda request: httpx.Response(200, json=[]), make_manifest([endpoint]))
    client.http = injected  # swap in an injected client to prove ownership, not identity
    client._owns_http_client = False
    client.close()
    assert not injected.is_closed
    injected.close()


def test_context_manager_closes_on_exit():
    endpoint = EndpointSpec(name="list", path="/items", fields={})
    manifest = make_manifest([endpoint])
    with ConnectorClient(manifest) as client:
        http = client.http
        assert not http.is_closed
    assert http.is_closed


def test_transport_error_is_retried_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json=[{"id": 1}])

    endpoint = EndpointSpec(name="list", path="/items", fields={"id": FieldMapping(source_path="id")})
    sleeps = []
    client = client_with(handler, make_manifest([endpoint], max_retries=5), sleep=sleeps.append)
    assert client.call("list") == [{"id": 1}]
    assert calls["n"] == 3
    assert len(sleeps) == 2


def test_transport_error_exhausted_raises_upstream_connection_error():
    def handler(request):
        raise httpx.ConnectError("boom")

    endpoint = EndpointSpec(name="list", path="/items", fields={})
    client = client_with(handler, make_manifest([endpoint], max_retries=2))
    with pytest.raises(UpstreamConnectionError):
        client.call("list")


def test_rate_limiter_is_consulted_per_request():
    acquire_calls = {"n": 0}

    class CountingLimiter:
        def acquire(self):
            acquire_calls["n"] += 1

    def handler(request):
        return httpx.Response(200, json=[{"id": 1}])

    pagination = PaginationSpec(style="page", param_name="page")
    endpoint = EndpointSpec(
        name="list", path="/items", pagination=pagination, fields={"id": FieldMapping(source_path="id")}
    )
    manifest = make_manifest([endpoint])
    client = ConnectorClient(
        manifest,
        http_client=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.test"),
        rate_limiter=CountingLimiter(),
    )
    client.call("list", max_pages=1)
    assert acquire_calls["n"] == 1
