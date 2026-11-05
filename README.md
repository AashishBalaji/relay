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
- **`relay/cli/`** — `relay validate`, `relay test`, `relay drift-check`, `relay new-connector`, `relay lint`, `relay dev`.
- **`relay/dev/`** — local developer-experience tooling: `generate.py` derives synthetic field values straight from a manifest's own field declarations (no hand-maintained fixtures); `mock_server.py` is a stdlib-only HTTP mock server that serves that synthetic data, honoring whatever pagination style and endpoint shape the manifest describes, with an optional `--simulate-drift` flag to rename a field in every response; `policy_lint.py` is a set of policy checks standing in for what an App Shop-style marketplace would enforce before admitting a connector (sane rate limits, sane retries, at least one required field per endpoint, no embedded secrets in auth config).

## Try it

```bash
pip install -e .
relay validate relay/connectors/jsonplaceholder/manifest.yaml
relay test relay/connectors/jsonplaceholder/manifest.yaml list_posts
relay drift-check relay/connectors/jsonplaceholder/manifest.yaml
```

`drift-check` makes a real call to JSONPlaceholder, so it needs network access — it will report "No drift detected" against the live API today.

## Local dev loop (`relay dev` / `relay lint`)

A connector author shouldn't need a real, reachable upstream API just to exercise field mapping, pagination, or drift detection while writing a manifest. `relay dev` spins up a local mock HTTP server whose responses are generated directly from the manifest's own field declarations — point `relay test`/`relay drift-check` at it with `--base-url`:

```bash
relay dev relay/connectors/jsonplaceholder/manifest.yaml
# Mock server for 'jsonplaceholder' running at http://127.0.0.1:PORT
#   relay test relay/connectors/jsonplaceholder/manifest.yaml <endpoint> --base-url http://127.0.0.1:PORT
#   relay drift-check relay/connectors/jsonplaceholder/manifest.yaml --base-url http://127.0.0.1:PORT
# Press Ctrl+C to stop.
```

Add `--simulate-drift <field_name>` to prove, with zero real upstream involved, that `drift-check` would actually catch a real schema break — it renames that field's last path segment in every generated response, so `drift-check --base-url ...` against the running mock server reports drift exactly as it would against a real API that renamed the field.

`relay lint` runs policy checks a marketplace would reasonably want a connector to pass before listing it — rate limit and retry sanity, at least one required field per endpoint (otherwise drift detection has nothing to notice), and a pattern-based check for secrets accidentally hardcoded into auth config instead of pulled from the environment:

```bash
relay lint relay/connectors/jsonplaceholder/manifest.yaml
# OK: jsonplaceholder passes policy lint, no findings
```

## Design notes worth knowing

- **Why the drift detector is conservative on purpose**: a field rename from `user` to `author` on a real API correctly comes back as *unsafe*, not auto-patched — the string similarity (~0.40) is genuinely too low to be a confident match. A tool that silently auto-patched this would be dangerous. The detector's job is to flag plausible renames for human review, not guess blindly. Short keys get an extra guard: a rename is never auto-classified "safe" below a minimum key length, because string-similarity on short strings is noisy (`"id"` vs `"uid"` scores 0.8 on character overlap alone, despite being an unreliable signal).
- **Endpoint-unreachable vs. field-renamed are reported separately**: if an endpoint can't be called at all (auth failure, network error, 5xx after retries), that's a different situation from a schema change, and `DriftReport` keeps them apart (`unreachable_endpoints` vs. `unsafe_drifts`) so a consumer can tell "the connector is down" from "the schema changed" — both still fail CI loudly, but for different reasons.
- **Separation of concerns**: `fetch_raw_items()` returns raw, un-mapped API responses; `call()` wraps it and applies field extraction. This split exists specifically so the drift detector can inspect raw response shapes without `call()`'s mapping step discarding the very data it needs.
- **Resource cleanup and retries**: `ConnectorClient` supports `close()` / use as a context manager, closing only the HTTP client it created itself (never one injected by a caller who owns it). Retries cover transport-level failures (`ConnectError`, timeouts) in addition to retryable HTTP status codes, with full-jitter backoff so multiple connectors retrying a shared upstream after an outage don't retry in lockstep.
- **What isn't live-tested, and why**: the GitHub OAuth2 connector needs a real access token, which this project doesn't have (no token was purchased/available) — it's tested via recorded fixtures instead. The Anthropic LLM client is real and callable, but exercising it against the live Anthropic API wasn't run here (no committed API key).

## Round-table review

Relay went through the same three-lens independent review as this portfolio's earlier Ledger project: a hiring manager (for the specific JD this targets), a senior platform engineer (code review), and an SRE/reliability engineer (production-readiness). Round 1 scores: 8/10, 7/10, 5/10. The SRE review surfaced real issues, not nitpicks — a resource leak (`ConnectorClient` never closed its HTTP client), retries that didn't cover transport-level failures, no jitter on backoff, and most importantly a drift-detector safety gap (short-key rename guesses could pass the similarity threshold on character-overlap coincidence alone, and an unreachable endpoint was indistinguishable from a real schema change). All of these were fixed for real — see "Design notes" above — and verified with the full test suite (105 tests) plus a live run against JSONPlaceholder.

## Running tests

```bash
pip install -e ".[dev]"
ruff check .
pytest tests/unit tests/contract -m "not live"
pytest tests/contract -m live   # hits the real JSONPlaceholder API; needs network
```

105 tests, all passing: unit tests for the manifest schema, field mapping, rate limiter, auth injection, HTTP client (mocked transport, including transport-error retries and resource cleanup), CLI commands, manifest generator (fake LLM), drift detector (including the short-key safety guard and unreachable-vs-drifted classification), property-based tests (Hypothesis) for the field-mapping round trip, the rate limiter's sliding-window invariant, and manifest version validation, and the `relay dev`/`relay lint` extension (synthetic data generation, the mock server across all pagination styles plus single-object endpoints, `--simulate-drift`, policy lint checks, and an end-to-end test combining the mock server with the real `ConnectorClient` and `drift_detector`).

## Docker

```bash
docker build -t relay .
docker run --rm relay validate /path/to/manifest.yaml
```

The image was verified by actually building it and running `relay drift-check` against the live JSONPlaceholder API from inside the running container (non-root `appuser`, no dev tools, multi-stage build with no unnecessary build toolchain in the runtime image).
