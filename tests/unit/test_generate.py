from relay.client.mapping import extract_fields
from relay.client.paths import get_path
from relay.dev.generate import build_synthetic_item, build_synthetic_page, set_path
from relay.manifest.schema import EndpointSpec, FieldMapping


def test_set_path_creates_nested_structure():
    container: dict = {}
    set_path(container, "a.b.c", 42)
    assert container == {"a": {"b": {"c": 42}}}


def test_set_path_overwrites_existing_leaf():
    container = {"a": {"b": 1}}
    set_path(container, "a.b", 2)
    assert container == {"a": {"b": 2}}


def test_synthetic_item_resolves_cleanly_through_extract_fields():
    """The whole point of generating synthetic data is that it's usable by
    the SAME extract_fields the real client calls -- if this round-trips
    with nothing missing, the mock server's output is shaped correctly."""
    fields = {
        "id": FieldMapping(source_path="id", required=True, type_hint="integer"),
        "author": FieldMapping(source_path="user.login", required=True, type_hint="string"),
        "active": FieldMapping(source_path="flags.active", required=False, type_hint="boolean"),
    }
    item = build_synthetic_item(fields, index=3)
    normalized, missing = extract_fields(item, fields)
    assert missing == []
    assert normalized["id"] == 3
    assert normalized["author"] == "author_3"
    assert isinstance(normalized["active"], bool)


def test_synthetic_item_respects_rename():
    from relay.client.paths import is_missing

    fields = {"id": FieldMapping(source_path="user.id", required=True)}
    item = build_synthetic_item(fields, index=1, rename=("id", "uid"))
    # the renamed path resolves, the original one does not
    assert is_missing(get_path(item, "user.id"))
    assert not is_missing(get_path(item, "user.uid"))


def test_build_synthetic_page_indices_increase_across_pages():
    endpoint = EndpointSpec(
        name="list", path="/items", fields={"id": FieldMapping(source_path="id", type_hint="integer")}
    )
    page0 = build_synthetic_page(endpoint, page_index=0, page_size=3)
    page1 = build_synthetic_page(endpoint, page_index=1, page_size=3)
    assert [item["id"] for item in page0] == [0, 1, 2]
    assert [item["id"] for item in page1] == [3, 4, 5]
