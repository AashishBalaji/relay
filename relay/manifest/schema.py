"""The connector manifest: the one declarative spec a connector author writes.

Everything downstream (the HTTP client, contract tests, drift detection) is
derived from this schema rather than hand-written per connector. That's the
whole point of the framework: a connector author describes *what* an API
looks like, not *how* to call it reliably.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# --- Auth -------------------------------------------------------------

class ApiKeyAuth(BaseModel):
    type: Literal["api_key"] = "api_key"
    location: Literal["header", "query"] = "header"
    name: str  # header name or query param name, e.g. "Authorization" or "api_key"
    value_prefix: str = ""  # e.g. "Bearer " or "token "


class BasicAuth(BaseModel):
    type: Literal["basic"] = "basic"


class OAuth2Auth(BaseModel):
    type: Literal["oauth2"] = "oauth2"
    token_url: str
    grant_type: Literal["client_credentials", "authorization_code"] = "client_credentials"
    scopes: list[str] = Field(default_factory=list)


class NoAuth(BaseModel):
    type: Literal["none"] = "none"


AuthConfig = ApiKeyAuth | BasicAuth | OAuth2Auth | NoAuth


# --- Pagination ---------------------------------------------------------

class PaginationSpec(BaseModel):
    style: Literal["page", "cursor", "offset", "none"] = "none"
    param_name: str | None = None  # the query param that carries page/cursor/offset
    page_size_param: str | None = None
    page_size: int | None = None
    # JSON path (dot notation, e.g. "meta.next_cursor") to the next cursor/page
    # value in the response body, used by cursor-style pagination.
    next_token_path: str | None = None
    # JSON path to the list of items in a paginated response, e.g. "items" or "data.results"
    items_path: str | None = None

    @model_validator(mode="after")
    def _cursor_needs_token_path(self) -> PaginationSpec:
        if self.style == "cursor" and not self.next_token_path:
            raise ValueError("cursor-style pagination requires next_token_path")
        if self.style in ("page", "offset") and not self.param_name:
            raise ValueError(f"{self.style}-style pagination requires param_name")
        return self


# --- Endpoints ------------------------------------------------------------

class FieldMapping(BaseModel):
    """Maps a manifest-level (normalized) field name to where it actually
    lives in the raw JSON response, via dot-notation path. This is the
    layer that both the client (to normalize responses) and the drift
    detector (to notice when a path stops resolving) both read."""
    source_path: str
    required: bool = True
    type_hint: Literal["string", "integer", "number", "boolean", "array", "object"] | None = None


class EndpointSpec(BaseModel):
    name: str  # logical name, e.g. "list_users"
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    path: str  # may contain {placeholders}, e.g. "/users/{user_id}"
    pagination: PaginationSpec | None = None
    # field_name -> FieldMapping, describing the shape of a single item in
    # the response this endpoint returns (or of the response itself, for
    # non-list endpoints).
    fields: dict[str, FieldMapping] = Field(default_factory=dict)

    @field_validator("path")
    @classmethod
    def _path_must_start_with_slash(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError(f"endpoint path must start with '/': {v!r}")
        return v


# --- Top-level manifest ---------------------------------------------------

class ConnectorManifest(BaseModel):
    name: str
    version: str
    base_url: str
    auth: AuthConfig = Field(default_factory=NoAuth)
    endpoints: list[EndpointSpec]
    # Rate limit, expressed as "N requests per window_seconds" -- the client
    # framework enforces this via a token-bucket limiter regardless of what
    # the upstream API itself returns, so a connector author gets safe
    # defaults even against an API with no documented limit.
    rate_limit_per_window: int = 10
    rate_limit_window_seconds: float = 1.0
    max_retries: int = 3

    @field_validator("version")
    @classmethod
    def _version_must_be_semverish(cls, v: str) -> str:
        parts = v.split(".")
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            raise ValueError(f"version must be 'MAJOR.MINOR.PATCH', got {v!r}")
        return v

    @field_validator("endpoints")
    @classmethod
    def _endpoint_names_unique(cls, v: list[EndpointSpec]) -> list[EndpointSpec]:
        names = [e.name for e in v]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate endpoint names: {sorted(dupes)}")
        if not v:
            raise ValueError("a connector manifest needs at least one endpoint")
        return v

    def endpoint(self, name: str) -> EndpointSpec:
        for e in self.endpoints:
            if e.name == name:
                return e
        raise KeyError(f"no endpoint named {name!r} in connector {self.name!r}")
