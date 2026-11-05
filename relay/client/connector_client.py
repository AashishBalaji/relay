"""The HTTP client a connector manifest generates. A connector author never
writes retry logic, rate-limiting, pagination traversal, or auth injection
by hand -- all of that lives here, once, driven by the manifest; the
connector-specific file (relay/connectors/<name>/connector.py) only wires up
credentials and calls client.call(endpoint_name, ...).
"""
from __future__ import annotations

import random
import time
from typing import Any

import httpx

from relay.client.auth import apply_auth
from relay.client.mapping import extract_fields
from relay.client.paths import get_path, is_missing
from relay.client.rate_limit import RateLimiter
from relay.manifest.schema import ConnectorManifest


class UpstreamError(Exception):
    """Raised when an upstream call fails after exhausting retries."""

    def __init__(self, response: httpx.Response):
        self.response = response
        super().__init__(f"{response.status_code} from {response.request.url}")


class UpstreamConnectionError(Exception):
    """Raised when a transport-level failure (connection refused, DNS
    failure, timeout, TLS error, ...) persists after exhausting retries --
    distinct from UpstreamError, which carries a real HTTP response. A
    framework whose whole pitch is 'retry/backoff handled once, centrally'
    has to cover this path too, not just bad status codes."""

    def __init__(self, method: str, path: str, cause: Exception):
        self.cause = cause
        super().__init__(f"{method} {path} failed after retries: {cause!r}")


class MissingRequiredFieldsError(Exception):
    """Raised when a live response is missing a field the manifest marks
    required -- this is exactly the condition the drift detector watches
    for, but the live client raises it immediately rather than silently
    returning incomplete data to a caller who didn't ask to check."""

    def __init__(self, endpoint_name: str, missing: list[str]):
        self.endpoint_name = endpoint_name
        self.missing = missing
        super().__init__(f"{endpoint_name}: missing required fields {missing}")


_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ConnectorClient:
    def __init__(
        self,
        manifest: ConnectorManifest,
        credentials: dict | None = None,
        http_client: httpx.Client | None = None,
        rate_limiter: RateLimiter | None = None,
        sleep=time.sleep,
        jitter=random.random,
    ):
        self.manifest = manifest
        self.credentials = credentials or {}
        # Only a client we create ourselves is ours to close -- an injected
        # http_client is owned by whoever passed it in (e.g. a caller
        # pooling connections across several ConnectorClients).
        self._owns_http_client = http_client is None
        self.http = http_client or httpx.Client(base_url=manifest.base_url, timeout=10.0)
        self.rate_limiter = rate_limiter or RateLimiter(
            manifest.rate_limit_per_window, manifest.rate_limit_window_seconds
        )
        self._sleep = sleep
        self._jitter = jitter

    def close(self) -> None:
        """Releases the underlying connection pool -- only if this client
        created it. A long-running process that instantiates many
        ConnectorClients (one per connector, say) needs this to avoid
        leaking sockets; a one-shot CLI invocation doesn't strictly need
        it, but calling it is always safe."""
        if self._owns_http_client:
            self.http.close()

    def __enter__(self) -> ConnectorClient:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def call(
        self,
        endpoint_name: str,
        path_params: dict | None = None,
        query_params: dict | None = None,
        json_body: dict | None = None,
        raise_on_missing_required: bool = True,
        max_pages: int = 50,
    ) -> list[dict]:
        """Returns a list of normalized dicts (one per item). A non-paginated,
        single-object endpoint still returns a one-element list, so callers
        don't need two different code paths."""
        endpoint = self.manifest.endpoint(endpoint_name)
        raw_items = self.fetch_raw_items(
            endpoint_name, path_params=path_params, query_params=query_params,
            json_body=json_body, max_pages=max_pages,
        )
        all_normalized: list[dict] = []
        for raw_item in raw_items:
            normalized, missing = extract_fields(raw_item, endpoint.fields)
            if missing and raise_on_missing_required:
                raise MissingRequiredFieldsError(endpoint_name, missing)
            all_normalized.append(normalized)
        return all_normalized

    def fetch_raw_items(
        self,
        endpoint_name: str,
        path_params: dict | None = None,
        query_params: dict | None = None,
        json_body: dict | None = None,
        max_pages: int = 50,
    ) -> list[dict]:
        """Does auth injection, retries, rate-limiting and pagination
        traversal, but returns the RAW (un-mapped) items straight off the
        wire. `call()` is this plus field extraction; the drift detector
        uses this directly because it needs the raw shape to suggest where
        a renamed field actually went, which field extraction already threw
        away by the time `call()` returns."""
        endpoint = self.manifest.endpoint(endpoint_name)
        path = endpoint.path.format(**(path_params or {}))

        all_raw: list[dict] = []
        page_token: Any = None
        pages_fetched = 0

        while True:
            headers: dict = {}
            params = dict(query_params or {})
            basic_auth = apply_auth(self.manifest.auth, self.credentials, headers, params)

            pagination = endpoint.pagination
            if pagination and pagination.style == "page":
                # Send the param explicitly even on page 1 -- relying on the
                # upstream API to default an omitted page param to 1 is an
                # assumption, not a guarantee, and this framework's whole
                # point is not leaving that kind of thing implicit.
                params[pagination.param_name] = page_token or 1
            elif pagination and pagination.style != "none" and page_token is not None:
                params[pagination.param_name] = page_token
            if pagination and pagination.page_size_param and pagination.page_size:
                params[pagination.page_size_param] = pagination.page_size

            response = self._request_with_retry(
                endpoint.method, path,
                params=params, headers=headers, json=json_body, auth=basic_auth,
            )
            body = response.json()

            raw_items = self._extract_items(body, pagination)
            all_raw.extend(raw_items)

            pages_fetched += 1
            if not pagination or pagination.style == "none" or pages_fetched >= max_pages:
                break

            if pagination.style == "cursor":
                next_token = get_path(body, pagination.next_token_path)
                if is_missing(next_token) or next_token is None:
                    break
                if next_token == page_token:
                    # A buggy (but real-world) upstream can echo the same
                    # cursor back forever -- stop here instead of burning
                    # max_pages calls against it before giving up.
                    break
                page_token = next_token
            elif pagination.style == "page":
                if not raw_items:
                    break
                page_token = (page_token or 1) + 1
            elif pagination.style == "offset":
                if not raw_items:
                    break
                page_token = (page_token or 0) + len(raw_items)

        return all_raw

    @staticmethod
    def _extract_items(body: Any, pagination) -> list[dict]:
        if pagination and pagination.items_path:
            items = get_path(body, pagination.items_path)
            return items if not is_missing(items) and isinstance(items, list) else []
        if isinstance(body, list):
            return body
        return [body]

    def _backoff_seconds(self, attempt: int) -> float:
        # Full jitter (per AWS's backoff guidance): a random value in
        # [0, cap] rather than a fixed cap. Several connectors/workers
        # retrying a shared upstream after an outage with identical,
        # un-jittered backoff would all retry in lockstep -- a
        # thundering herd against the very thing that just recovered.
        cap = min(2**attempt, 30)
        return self._jitter() * cap

    def _request_with_retry(self, method: str, path: str, **kwargs) -> httpx.Response:
        attempt = 0
        while True:
            self.rate_limiter.acquire()
            try:
                response = self.http.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                # A connection failure, timeout, or DNS blip is just as
                # retryable as a 503 -- a framework whose selling point is
                # "retry/backoff handled once, centrally" has to cover
                # transport failures too, not only bad status codes.
                if attempt < self.manifest.max_retries:
                    attempt += 1
                    self._sleep(self._backoff_seconds(attempt))
                    continue
                raise UpstreamConnectionError(method, path, exc) from exc
            if response.status_code < 400:
                return response
            if response.status_code in _RETRYABLE_STATUS and attempt < self.manifest.max_retries:
                attempt += 1
                self._sleep(self._backoff_seconds(attempt))
                continue
            raise UpstreamError(response)
