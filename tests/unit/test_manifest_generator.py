from relay.agents.manifest_generator import (
    _response_fields,
    build_prompt,
    generate_manifest_draft,
)

_SAMPLE_SPEC = {
    "info": {"title": "Widgets API"},
    "servers": [{"url": "https://api.widgets.test"}],
    "paths": {
        "/widgets": {
            "get": {
                "summary": "List widgets",
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "array",
                                    "items": {"$ref": "#/components/schemas/Widget"},
                                }
                            }
                        }
                    }
                },
            }
        }
    },
    "components": {
        "schemas": {
            "Widget": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
            }
        }
    },
}


class FakeLLMClient:
    def __init__(self, response: str):
        self.response = response
        self.last_prompt: str | None = None

    def complete(self, prompt: str) -> str:
        self.last_prompt = prompt
        return self.response


VALID_MANIFEST_YAML = """\
Here's the manifest:

```yaml
name: widgets
version: "1.0.0"
base_url: "https://api.widgets.test"
auth:
  type: none
endpoints:
  - name: list_widgets
    method: GET
    path: /widgets
    fields:
      id:
        source_path: id
        required: true
        type_hint: integer
      name:
        source_path: name
        required: true
        type_hint: string
```
"""


def test_response_fields_resolves_array_and_ref():
    operation = _SAMPLE_SPEC["paths"]["/widgets"]["get"]
    fields = _response_fields(_SAMPLE_SPEC, operation)
    assert set(fields) == {"id", "name"}


def test_build_prompt_includes_endpoint_and_fields():
    prompt = build_prompt(_SAMPLE_SPEC, "widgets")
    assert "GET /widgets" in prompt
    assert "id" in prompt and "name" in prompt
    assert "api.widgets.test" in prompt


def test_generate_manifest_draft_succeeds_on_valid_llm_output():
    llm = FakeLLMClient(VALID_MANIFEST_YAML)
    result = generate_manifest_draft(_SAMPLE_SPEC, "widgets", llm)
    assert result.ok
    assert result.manifest.name == "widgets"
    assert result.manifest.endpoint("list_widgets").path == "/widgets"
    assert llm.last_prompt is not None  # the prompt was actually built and sent


def test_generate_manifest_draft_reports_problems_on_invalid_schema():
    bad_yaml = """\
```yaml
name: widgets
version: not-a-version
base_url: "https://api.widgets.test"
endpoints: []
```
"""
    llm = FakeLLMClient(bad_yaml)
    result = generate_manifest_draft(_SAMPLE_SPEC, "widgets", llm)
    assert not result.ok
    assert any("version" in p for p in result.problems)


def test_generate_manifest_draft_reports_problems_on_non_yaml_output():
    llm = FakeLLMClient("I'm sorry, I can't help with that.")
    result = generate_manifest_draft(_SAMPLE_SPEC, "widgets", llm)
    assert not result.ok
    assert result.problems  # some problem was reported, not a silent crash


def test_generate_manifest_draft_handles_output_with_no_code_fence():
    """Some LLM responses won't bother with a fence if asked just right --
    the extractor should fall back to treating the whole output as YAML."""
    bare_yaml = """\
name: widgets
version: "1.0.0"
base_url: "https://api.widgets.test"
auth:
  type: none
endpoints:
  - name: list_widgets
    path: /widgets
    fields:
      id:
        source_path: id
"""
    llm = FakeLLMClient(bare_yaml)
    result = generate_manifest_draft(_SAMPLE_SPEC, "widgets", llm)
    assert result.ok
