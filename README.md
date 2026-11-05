# Relay

A manifest-driven connector framework for building integrations against third-party APIs — with AI-assisted connector generation and automated drift detection.

Instead of hand-writing a client per API (auth, retries, pagination, field mapping, error handling, all duplicated), you write a declarative manifest describing the API's shape, and Relay derives a working, production-ready client from it.

```yaml
name: jsonplaceholder
version: "1.0.0"
base_url: "https://jsonplaceholder.typicode.com"
auth:
  type: none
rate_limit_per_window: 20
rate_limit_window_seconds: 1.0
max_retries: 3
endpoints:
  - name: list_posts
    method: GET
    path: /posts
    pagination:
      style: page
      param_name: _page
      page_size_param: _limit
      page_size: 20
    fields:
      id:
        source_path: id
        required: true
      title:
        source_path: title
        required: true
```

```python
from relay.client.connector_client import ConnectorClient
from relay.manifest.loader import load_manifest

manifest = load_manifest("relay/connectors/jsonplaceholder/manifest.yaml")
client = ConnectorClient(manifest)
posts = client.call("list_posts")  # auth, retries, pagination, field mapping -- all derived from the manifest
```

## What's real vs. what's not

This is a portfolio project, built to be run and inspected, not just read. Here's exactly what's genuinely live versus what's demonstrated against fixtures, with the reason:

| Piece | Status | Why |
|---|---|---|
| Core framework (schema, client, auth, pagination, rate limiting, retries) | Fully real, unit-tested | Core logic |
| **JSONPlaceholder connector** | **Genuinely live** — real HTTP calls over the real network, including real multi-page traversal | `jsonplaceholder.typicode.com` is a free, stable, public API with no auth required |
| GitHub connector (OAuth2) | Fixture-tested only | Demonstrates the OAuth2 auth pattern and real-shaped response data (recorded JSON fixtures, including a page with 100+ issues and a "drift" fixture simulating a field rename), but is **not** called against the real GitHub API, because no real OAuth token was purchased for this project |
| AI manifest generator | Fully unit-tested with a fake/injectable LLM client | A real `AnthropicLLMClient` implementation exists and will call the real Anthropic API (model `claude-haiku-5-5`) if invoked with a real API key — but the generator's own tests never call the real Anthropic API; they inject a fake client |

Nothing in this project fakes a result silently. Every place it could not do a genuine live run (a real OAuth2 token, a purchased Anthropic API key) is disclosed above, not glossed over.

## What's in here

- **`relay/manifest/`** — the manifest schema (Pydantic v2) and loader. Validates a connector's YAML/JSON definition: auth config (API key, basic, OAuth2, none), endpoints, field mappings, pagination strategy, rate limits, retries.
- **`relay/client/`** — `ConnectorClient`, a generic HTTP client driven entirely by a loaded manifest. Handles auth injection, retry with exponential backoff, token-bucket rate limiting, and pagination traversal (page/offset or cursor style) — none of it hand-written per connector.
- **`relay/connectors/`** — example connectors: each is just a `manifest.yaml` plus a few lines of Python wiring credentials.
- **`relay/agents/manifest_generator.py`** — given an OpenAPI spec and an LLM client, drafts a starting manifest by asking the LLM to infer auth, key endpoints, and field mappings. You review and refine the draft; it isn't meant to be accepted blind.
- **`relay/agents/drift_detector.py`** — the "Zero KTLO" piece. Scans a connector's live responses against its manifest, and for every missing required field, checks whether a plausible renamed field exists nearby (fuzzy string match, similarity ≥ 0.6) or whether the field is genuinely gone. Classifies each issue as **safe to auto-patch** or **needs human review**, and can write the safe fixes back into the manifest.
- **`relay/cli/`** — `relay validate`, `relay test`, `relay drift-check`, `relay new-connector`.

## Try it

```bash
pip install -e .
relay validate relay/connectors/jsonplaceholder/manifest.yaml
relay test relay/connectors/jsonplaceholder/manifest.yaml list_posts
relay drift-check relay/connectors/jsonplaceholder/manifest.yaml
```

`drift-check` makes a real call to JSONPlaceholder, so it needs network access — it will report "No drift detected" against the live API today.

## Design notes worth knowing

- **Why the drift detector is conservative on purpose**: a field rename from `user` to `author` on a real API correctly comes back as *unsafe*, not auto-patched — the string similarity (~0.40) is genuinely too low to be a confident match. A tool that silently auto-patched this would be dangerous. The detector's job is to flag plausible renames for human review, not guess blindly.
- **Separation of concerns**: `fetch_raw_items()` returns raw, un-mapped API responses; `call()` wraps it and applies field extraction. This split exists specifically so the drift detector can inspect raw response shapes without `call()`'s mapping step discarding the very data it needs.
- **What isn't live-tested, and why**: the GitHub OAuth2 connector needs a real access token, which this project doesn't have (no token was purchased/available) — it's tested via recorded fixtures instead. The Anthropic LLM client is real and callable, but exercising it against the live Anthropic API wasn't run here (no committed API key).

## Running tests

```bash
pip install -e ".[dev]"
ruff check .
pytest tests/unit tests/contract -m "not live"
pytest tests/contract -m live   # hits the real JSONPlaceholder API; needs network
```

75 tests, all passing: unit tests for the manifest schema, field mapping, rate limiter, auth injection, HTTP client (mocked transport), CLI commands, manifest generator (fake LLM), drift detector, and property-based tests (Hypothesis) for the field-mapping round trip, the rate limiter's sliding-window invariant, and manifest version validation.

## Docker

```bash
docker build -t relay .
docker run --rm relay validate /path/to/manifest.yaml
```

The image was verified by actually building it and running `relay drift-check` against the live JSONPlaceholder API from inside the running container (non-root `appuser`, no dev tools, multi-stage build with no unnecessary build toolchain in the runtime image).
