"""An AI-assisted connector-manifest generator: given a third-party API's
OpenAPI spec, drafts a Relay manifest instead of a human hand-writing one
endpoint at a time.

Deliberately NOT fully automatic: the output is always a *draft* a human
reviews and edits before it's used for real, same as the drift detector
below only auto-patches changes it's highly confident are safe. The core
logic here never touches the network itself -- it's built against an
LLMClient Protocol, so it's fully unit-testable with a fake that returns
canned text, the same pattern as relay's drift-report generation and as
the triage bot in a prior portfolio project (Ledger).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

import yaml
from pydantic import ValidationError

from relay.manifest.schema import ConnectorManifest


class LLMClient(Protocol):
    def complete(self, prompt: str) -> str: ...


class AnthropicLLMClient:
    """The real implementation. Never exercised by unit tests -- only by a
    human running `relay generate-manifest` for real, with a real API key."""

    def __init__(self, api_key: str | None = None, model: str = "claude-haiku-5-5"):
        import anthropic  # imported lazily so unit tests never need the package configured

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def complete(self, prompt: str) -> str:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in response.content if block.type == "text")


@dataclass
class GeneratedManifest:
    manifest: ConnectorManifest | None
    raw_output: str
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.manifest is not None


def _resolve_ref(spec: dict, ref: str) -> dict:
    """Resolves one '#/components/schemas/Foo'-style local ref. Doesn't
    recurse into nested refs -- good enough for the common case of a flat
    response-object schema, and failing loudly on anything more exotic is
    safer than silently drafting a wrong manifest."""
    if not ref.startswith("#/"):
        return {}
    node: Any = spec
    for part in ref[2:].split("/"):
        node = node.get(part, {}) if isinstance(node, dict) else {}
    return node if isinstance(node, dict) else {}


def _response_fields(spec: dict, operation: dict) -> list[str]:
    """Best-effort extraction of field names from a 200 response's JSON
    schema -- handles a direct object schema, an array of objects, and one
    level of $ref, which covers most real-world OpenAPI specs without
    needing a full schema resolver."""
    responses = operation.get("responses", {})
    ok_response = responses.get("200") or responses.get("201") or {}
    schema = ok_response.get("content", {}).get("application/json", {}).get("schema", {})
    if "$ref" in schema:
        schema = _resolve_ref(spec, schema["$ref"])
    if schema.get("type") == "array":
        item_schema = schema.get("items", {})
        if "$ref" in item_schema:
            item_schema = _resolve_ref(spec, item_schema["$ref"])
        schema = item_schema
    return list(schema.get("properties", {}).keys())


def build_prompt(openapi_spec: dict, connector_name: str) -> str:
    """Condenses an OpenAPI spec into a compact description of each
    operation -- method, path, and known response fields -- rather than
    pasting the whole spec into the prompt, which would blow the context
    window on any real-world API and bury the signal in boilerplate."""
    base_url = (
        openapi_spec.get("servers", [{}])[0].get("url", "")
        if openapi_spec.get("servers")
        else ""
    )
    lines = [f"API title: {openapi_spec.get('info', {}).get('title', connector_name)}"]
    for path, methods in openapi_spec.get("paths", {}).items():
        for method, operation in methods.items():
            if method.upper() not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                continue
            fields = _response_fields(openapi_spec, operation)
            summary = operation.get("summary", "")
            lines.append(f"- {method.upper()} {path} -- {summary} -- response fields: {fields}")

    endpoints_description = "\n".join(lines)
    return f"""\
You are drafting a Relay connector manifest (YAML) for the "{connector_name}" API.
Base URL: {base_url}

Known operations:
{endpoints_description}

Produce ONLY a YAML document matching this schema, inside a ```yaml code fence:

name: {connector_name}
version: "1.0.0"
base_url: "<base url>"
auth:
  type: none | api_key | basic | oauth2
  # api_key fields: location (header|query), name, value_prefix
  # oauth2 fields: token_url, grant_type, scopes
endpoints:
  - name: <logical_name>
    method: GET|POST|PUT|PATCH|DELETE
    path: /resource/{{path_param}}
    fields:
      <field_name>:
        source_path: <dot.path.in.response>
        required: true|false
        type_hint: string|integer|number|boolean|array|object

Pick concise, snake_case endpoint and field names. Only include fields you
can see evidence for in the operations above.
"""


_CODE_FENCE_RE = re.compile(r"```(?:yaml|yml|json)?\s*\n(.*?)```", re.DOTALL)


def _extract_yaml_block(raw_output: str) -> str:
    match = _CODE_FENCE_RE.search(raw_output)
    return match.group(1) if match else raw_output


def generate_manifest_draft(
    openapi_spec: dict, connector_name: str, llm: LLMClient
) -> GeneratedManifest:
    prompt = build_prompt(openapi_spec, connector_name)
    raw_output = llm.complete(prompt)
    yaml_text = _extract_yaml_block(raw_output)

    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        return GeneratedManifest(
            manifest=None, raw_output=raw_output, problems=[f"not valid YAML: {exc}"]
        )

    if not isinstance(parsed, dict):
        return GeneratedManifest(
            manifest=None,
            raw_output=raw_output,
            problems=["LLM output did not parse to a YAML mapping"],
        )

    try:
        manifest = ConnectorManifest.model_validate(parsed)
    except ValidationError as exc:
        problems = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        return GeneratedManifest(manifest=None, raw_output=raw_output, problems=problems)

    return GeneratedManifest(manifest=manifest, raw_output=raw_output, problems=[])
