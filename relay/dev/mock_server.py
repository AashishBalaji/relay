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

from relay.dev.generate import build_synthetic_page, set_path
from relay.manifest.schema import ConnectorManifest, EndpointSpec

_DEFAULT_PAGE_SIZE = 5


def _path_regex(endpoint_path: str) -> re.Pattern:
    # "/repos/{owner}/{repo}/issues" -> a regex that captures each
    # {placeholder} as a named group matching one non-slash path segment.
    pattern = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", endpoint_path)
    return re.compile(f"^{pattern}$")


def _is_single_object_endpoint(endpoint: EndpointSpec) -> bool:
    # No explicit "returns one object vs. a list" flag exists on
    # EndpointSpec, so this uses the same convention real APIs follow: a
    # path with a placeholder ("/posts/{post_id}") is a get-one endpoint;
    # one without is a list endpoint.
    return "{" in endpoint.path


def _build_list_body(
    endpoint: EndpointSpec, page_index: int, pages: int, rename: tuple[str, str] | None
) -> tuple[dict | list, bool]:
    """Returns (body, has_more). `has_more` tells the caller whether a
    cursor-style response should advertise a next page."""
    items = build_synthetic_page(endpoint, page_index, _DEFAULT_PAGE_SIZE, rename=rename)
    has_more = page_index + 1 < pages
    pagination = endpoint.pagination
    if pagination and pagination.items_path:
        body: dict = {}
        set_path(body, pagination.items_path, items)
        return body, has_more
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

            if page_index >= pages:
                empty_body: dict | list = [] if not (pagination and pagination.items_path) else {}
                if pagination and pagination.items_path:
                    set_path(empty_body, pagination.items_path, [])
                self._send_json(200, empty_body)
                return

            body, has_more = _build_list_body(endpoint, page_index, pages, rename)
            if pagination and pagination.style == "cursor" and pagination.next_token_path:
                next_cursor = str(page_index + 1) if has_more else None
                target = body if isinstance(body, dict) else {}
                set_path(target, pagination.next_token_path, next_cursor)
                body = target if isinstance(body, dict) else body
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
