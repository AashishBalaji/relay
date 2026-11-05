"""A local mock HTTP server for connector development.

The whole point of a manifest is that it fully describes an API's shape --
so this server derives its responses straight from the SAME manifest a
connector author is writing, with zero fixtures to hand-maintain. Point
`ConnectorClient` (via `relay test`/`relay drift-check --base-url`) at it
to exercise field mapping and pagination before the real upstream API is
even reachable, or to prove drift detection actually catches a break
(`--simulate-drift`) without needing a real API to go break something.

Kept on the Python standard library (`http.server`) on purpose -- this is
dev tooling, not something that ships in a production image, so it isn't
worth a new runtime dependency.
"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from relay.dev.generate import build_synthetic_item, build_synthetic_page, set_path
from relay.manifest.schema import ConnectorManifest, EndpointSpec

_DEFAULT_PAGE_SIZE = 5


def _path_regex(endpoint_path: str) -> re.Pattern:
    # "/repos/{owner}/{repo}/issues" -> a regex that captures each
    # {placeholder} as a named group matching one non-slash path segment.
    pattern = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", endpoint_path)
    return re.compile(f"^{pattern}$")


def _is_single_object_endpoint(endpoint: EndpointSpec) -> bool:
    # No explicit "returns one object vs. a list" flag exists on
    # EndpointSpec, so this combines two signals rather than trusting either
    # alone: a path with a placeholder ("/posts/{post_id}") is normally a
    # get-one endpoint -- but a placeholder alone isn't enough, since list
    # endpoints are routinely nested under a path param too
    # ("/repos/{owner}/{repo}/issues" is a list). An endpoint that declares
    # pagination is unambiguously a list endpoint regardless of its path.
    pagination = endpoint.pagination
    is_paginated = pagination is not None and pagination.style != "none"
    return "{" in endpoint.path and not is_paginated


def _build_list_body(
    endpoint: EndpointSpec, page_index: int, pages: int, rename: tuple[str, str] | None
) -> tuple[dict | list, bool]:
    """Returns (body, has_more). `has_more` tells the caller whether a
    cursor-style response should advertise a next page."""
    pagination = endpoint.pagination
    has_more = page_index + 1 < pages
    if pagination and pagination.items_path:
        items = build_synthetic_page(endpoint, page_index, _DEFAULT_PAGE_SIZE, rename=rename)
        body: dict = {}
        set_path(body, pagination.items_path, items)
        return body, has_more
    if pagination and pagination.style == "cursor":
        # Without an items_path, ConnectorClient's _extract_items treats a
        # dict response body as a SINGLE raw item, not a list -- so a
        # cursor-paginated endpoint with no items_path is "one item per
        # page" in the real client, and the next cursor has to live in that
        # same dict alongside the item's own fields. There's nowhere to put
        # a cursor in a bare list, which is what this used to (wrongly)
        # generate here.
        item = build_synthetic_item(endpoint.fields, page_index, rename=rename)
        return item, has_more
    items = build_synthetic_page(endpoint, page_index, _DEFAULT_PAGE_SIZE, rename=rename)
    return items, has_more


class _MockHandler(BaseHTTPRequestHandler):
    manifest: ConnectorManifest
    pages: int
    simulate_drift_on: str | None  # field_name to rename, or None

    def log_message(self, *args) -> None:  # pragma: no cover -- silence stdout noise
        pass

    def do_GET(self) -> None:  # noqa: N802 -- stdlib's naming convention
        manifest, pages, simulate_drift_on = self.manifest, self.pages, self.simulate_drift_on
        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        for endpoint in manifest.endpoints:
            if endpoint.method != "GET":
                # A path match alone isn't enough -- a POST-only endpoint
                # sharing a path with no GET sibling must not be served on
                # GET, or every write-only endpoint would silently look
                # readable through the mock server.
                continue
            match = _path_regex(endpoint.path).match(parsed.path)
            if not match:
                continue

            rename = None
            if simulate_drift_on and simulate_drift_on in endpoint.fields:
                # The replacement must be the new LAST SEGMENT of the
                # field's actual source_path (e.g. "login" -> "login_renamed"
                # for source_path "user.login"), not a name built from the
                # manifest-level field_name -- those are frequently
                # different, and getting this wrong would rename the wrong
                # thing entirely.
                original_path = endpoint.fields[simulate_drift_on].source_path
                original_last_segment = original_path.split(".")[-1]
                rename = (simulate_drift_on, f"{original_last_segment}_renamed")

            if _is_single_object_endpoint(endpoint):
                # Use a numeric path param (e.g. {post_id}) as the
                # synthetic index when there is exactly one and it looks
                # numeric, so GET /posts/5 doesn't look identical to
                # GET /posts/1 -- purely cosmetic, but it's what a
                # developer actually poking at this expects to see.
                index = 0
                params = match.groupdict()
                if len(params) == 1:
                    (only_value,) = params.values()
                    if only_value.isdigit():
                        index = int(only_value)
                item = build_synthetic_page(endpoint, index, 1, rename=rename)[0]
                self._send_json(200, item)
                return

            pagination = endpoint.pagination
            page_index = 0
            if pagination and pagination.style == "page":
                page_index = int(query.get(pagination.param_name, "1")) - 1
            elif pagination and pagination.style == "offset":
                page_index = int(query.get(pagination.param_name, "0")) // _DEFAULT_PAGE_SIZE
            elif pagination and pagination.style == "cursor":
                cursor = query.get(pagination.param_name)
                page_index = int(cursor) if cursor is not None else 0

            # A dict-shaped empty page whenever the response body has to be
            # a dict at all (items nested under items_path, or a cursor
            # endpoint with no items_path where the body IS a single item)
            # -- otherwise a bare empty list, matching _build_list_body's
            # own shape choice below.
            body_is_dict = bool(
                pagination and (pagination.items_path or pagination.style == "cursor")
            )
            if page_index >= pages:
                empty_body: dict | list = {} if body_is_dict else []
                if pagination and pagination.items_path:
                    set_path(empty_body, pagination.items_path, [])
                self._send_json(200, empty_body)
                return

            body, has_more = _build_list_body(endpoint, page_index, pages, rename)
            if pagination and pagination.style == "cursor" and pagination.next_token_path:
                # _build_list_body guarantees `body` is a dict whenever
                # style is "cursor" (either items under items_path, or the
                # single synthetic item itself), so the cursor always has
                # somewhere real to live -- no more silently-discarded write
                # into a throwaway dict.
                next_cursor = str(page_index + 1) if has_more else None
                set_path(body, pagination.next_token_path, next_cursor)
            self._send_json(200, body)
            return

        self._send_json(404, {"error": f"no endpoint matches {parsed.path!r}"})

    def _send_json(self, status: int, body) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class MockServer:
    """Wraps a background ThreadingHTTPServer serving synthetic data
    derived from `manifest`. Use as a context manager, or call close()
    directly -- either way the server thread is a daemon, so it never
    blocks process exit even if close() is skipped."""

    def __init__(
        self,
        manifest: ConnectorManifest,
        port: int = 0,
        pages: int = 2,
        simulate_drift_on: str | None = None,
    ):
        handler = type(
            "_BoundMockHandler",
            (_MockHandler,),
            {"manifest": manifest, "pages": pages, "simulate_drift_on": simulate_drift_on},
        )
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_port}"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def __enter__(self) -> MockServer:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
