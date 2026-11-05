from relay.client.mapping import extract_fields
from relay.manifest.schema import FieldMapping


def test_extract_simple_fields():
    item = {"id": 1, "title": "hello"}
    fields = {"id": FieldMapping(source_path="id"), "title": FieldMapping(source_path="title")}
    normalized, missing = extract_fields(item, fields)
    assert normalized == {"id": 1, "title": "hello"}
    assert missing == []


def test_extract_nested_path():
    item = {"user": {"profile": {"name": "Ada"}}}
    fields = {"name": FieldMapping(source_path="user.profile.name")}
    normalized, missing = extract_fields(item, fields)
    assert normalized == {"name": "Ada"}
    assert missing == []


def test_missing_required_field_reported():
    item = {"id": 1}
    fields = {"id": FieldMapping(source_path="id"), "title": FieldMapping(source_path="title", required=True)}
    normalized, missing = extract_fields(item, fields)
    assert normalized == {"id": 1}
    assert missing == ["title"]


def test_missing_optional_field_silently_dropped():
    item = {"id": 1}
    fields = {"id": FieldMapping(source_path="id"), "nickname": FieldMapping(source_path="nickname", required=False)}
    normalized, missing = extract_fields(item, fields)
    assert normalized == {"id": 1}
    assert missing == []


def test_none_value_is_not_treated_as_missing():
    """A field that genuinely IS null upstream should come through as None,
    not be confused with a path that doesn't resolve at all -- those are
    different conditions (the first is normal data, the second is drift)."""
    item = {"id": 1, "deleted_at": None}
    fields = {"deleted_at": FieldMapping(source_path="deleted_at", required=True)}
    normalized, missing = extract_fields(item, fields)
    assert normalized == {"deleted_at": None}
    assert missing == []


def test_path_through_non_dict_is_missing():
    item = {"user": "not-a-dict"}
    fields = {"name": FieldMapping(source_path="user.name")}
    normalized, missing = extract_fields(item, fields)
    assert missing == ["name"]
