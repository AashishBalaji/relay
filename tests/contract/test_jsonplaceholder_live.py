"""A real, live contract test against the actual jsonplaceholder.typicode.com
API -- no mocking. This is deliberately a connector-level test, separate
from tests/unit, because it depends on a real network and a third party's
uptime; tests/unit never does.
"""
import pytest

from relay.connectors.jsonplaceholder.connector import get_client

pytestmark = pytest.mark.live


def test_list_posts_returns_well_shaped_items():
    client = get_client()
    posts = client.call("list_posts")
    assert len(posts) > 0
    first = posts[0]
    assert isinstance(first["id"], int)
    assert isinstance(first["title"], str)
    assert isinstance(first["body"], str)


def test_pagination_actually_traverses_multiple_pages():
    client = get_client()
    # page_size=10 in the manifest; asking for more than one page's worth
    # proves pagination traversal works against the real API, not just a
    # single request.
    posts = client.call("list_posts", max_pages=3)
    assert len(posts) > 10
    ids = [p["id"] for p in posts]
    assert len(set(ids)) == len(ids)  # no duplicate items across pages


def test_path_param_interpolation_against_real_endpoint():
    client = get_client()
    comments = client.call("list_comments_for_post", path_params={"post_id": 1})
    assert len(comments) > 0
    assert all(c["post_id"] == 1 for c in comments)


def test_single_object_endpoint():
    client = get_client()
    result = client.call("get_post", path_params={"post_id": 1})
    assert len(result) == 1
    assert result[0]["id"] == 1
