import httpx

from relay.dev.mock_server import MockServer
from relay.manifest.schema import ConnectorManifest, EndpointSpec, FieldMapping, PaginationSpec


def _manifest(endpoint):
    return ConnectorManifest(
        name="demo", version="1.0.0", base_url="https://unused.example", endpoints=[endpoint]
    )


def test_single_object_endpoint_returns_one_item_keyed_by_path_param():
    endpoint = EndpointSpec(
        name="get_post", path="/posts/{post_id}",
        fields={"id": FieldMapping(source_path="id", type_hint="integer")},
    )
    with MockServer(_manifest(endpoint)) as server:
        r = httpx.get(f"{server.base_url}/posts/7")
        assert r.status_code == 200
        assert r.json() == {"id": 7}


def test_page_pagination_serves_configured_pages_then_empty():
    pagination = PaginationSpec(style="page", param_name="page")
    endpoint = EndpointSpec(
        name="list", path="/items", pagination=pagination,
        fields={"id": FieldMapping(source_path="id", type_hint="integer")},
    )
    with MockServer(_manifest(endpoint), pages=2) as server:
        page1 = httpx.get(f"{server.base_url}/items", params={"page": 1}).json()
        page2 = httpx.get(f"{server.base_url}/items", params={"page": 2}).json()
        page3 = httpx.get(f"{server.base_url}/items", params={"page": 3}).json()
        assert len(page1) == 5
        assert len(page2) == 5
        assert page1[0]["id"] != page2[0]["id"]
        assert page3 == []


def test_cursor_pagination_advertises_next_cursor_then_none():
    pagination = PaginationSpec(
        style="cursor", param_name="cursor", next_token_path="meta.next", items_path="items"
    )
    endpoint = EndpointSpec(
        name="list", path="/items", pagination=pagination,
        fields={"id": FieldMapping(source_path="id", type_hint="integer")},
    )
    with MockServer(_manifest(endpoint), pages=2) as server:
        first = httpx.get(f"{server.base_url}/items").json()
        assert first["meta"]["next"] == "1"
        second = httpx.get(f"{server.base_url}/items", params={"cursor": "1"}).json()
        assert second["meta"]["next"] is None


def test_simulate_drift_renames_field_in_generated_response():
    endpoint = EndpointSpec(
        name="list", path="/items",
        fields={"author": FieldMapping(source_path="user.login", required=True)},
    )
    with MockServer(_manifest(endpoint), simulate_drift_on="author") as server:
        body = httpx.get(f"{server.base_url}/items").json()
        item = body[0]
        assert "login" not in item.get("user", {})
        assert "login_renamed" in item["user"]


def test_unknown_path_returns_404():
    endpoint = EndpointSpec(name="list", path="/items", fields={})
    with MockServer(_manifest(endpoint)) as server:
        r = httpx.get(f"{server.base_url}/nonexistent")
        assert r.status_code == 404


def test_list_endpoint_with_path_param_and_pagination_is_not_treated_as_single_object():
    """A path placeholder alone doesn't mean "get one" -- a list endpoint
    nested under a path param (e.g. /repos/{owner}/{repo}/issues) must
    still page normally, not collapse to one object."""
    pagination = PaginationSpec(style="page", param_name="page")
    endpoint = EndpointSpec(
        name="list_issues", path="/repos/{owner}/issues", pagination=pagination,
        fields={"id": FieldMapping(source_path="id", type_hint="integer")},
    )
    with MockServer(_manifest(endpoint), pages=2) as server:
        body = httpx.get(f"{server.base_url}/repos/acme/issues", params={"page": 1}).json()
        assert isinstance(body, list)
        assert len(body) == 5


def test_get_request_to_post_only_endpoint_path_returns_404_not_200():
    endpoint = EndpointSpec(
        name="create_item", method="POST", path="/items",
        fields={"id": FieldMapping(source_path="id")},
    )
    with MockServer(_manifest(endpoint)) as server:
        r = httpx.get(f"{server.base_url}/items")
        assert r.status_code == 404


def test_cursor_pagination_without_items_path_advertises_next_cursor():
    """Without items_path, ConnectorClient's _extract_items treats a dict
    body as a SINGLE item -- so the next cursor has to live in that same
    dict, not get silently dropped the way it used to."""
    pagination = PaginationSpec(style="cursor", param_name="cursor", next_token_path="next")
    endpoint = EndpointSpec(
        name="list", path="/items", pagination=pagination,
        fields={"id": FieldMapping(source_path="id", type_hint="integer")},
    )
    with MockServer(_manifest(endpoint), pages=2) as server:
        first = httpx.get(f"{server.base_url}/items").json()
        assert first["next"] == "1"
        assert first["id"] == 0
        second = httpx.get(f"{server.base_url}/items", params={"cursor": "1"}).json()
        assert second["next"] is None
