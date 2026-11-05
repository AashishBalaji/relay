"""Turns a manifest's declarative auth config into actual request
mutations. Credentials themselves are never stored on the manifest -- they
come in at call time from whatever secret store the caller uses -- so a
manifest can be committed to a repo (as every example connector here is)
without carrying a secret."""
from __future__ import annotations

from relay.manifest.schema import ApiKeyAuth, AuthConfig, BasicAuth, NoAuth, OAuth2Auth


class MissingCredentialError(Exception):
    pass


def apply_auth(
    auth: AuthConfig,
    credentials: dict,
    headers: dict,
    params: dict,
) -> tuple[str, str] | None:
    """Mutates headers/params in place for header/query-based auth; returns
    a (username, password) tuple for httpx's basic-auth kwarg when relevant,
    else None."""
    if isinstance(auth, NoAuth):
        return None

    if isinstance(auth, ApiKeyAuth):
        if "api_key" not in credentials:
            raise MissingCredentialError("api_key auth requires credentials['api_key']")
        value = auth.value_prefix + credentials["api_key"]
        if auth.location == "header":
            headers[auth.name] = value
        else:
            params[auth.name] = value
        return None

    if isinstance(auth, BasicAuth):
        if "username" not in credentials or "password" not in credentials:
            raise MissingCredentialError(
                "basic auth requires credentials['username'] and ['password']"
            )
        return (credentials["username"], credentials["password"])

    if isinstance(auth, OAuth2Auth):
        if "access_token" not in credentials:
            raise MissingCredentialError(
                "oauth2 auth requires credentials['access_token'] "
                "(Relay doesn't run the OAuth2 flow itself; it expects a token already obtained)"
            )
        headers["Authorization"] = f"Bearer {credentials['access_token']}"
        return None

    raise TypeError(f"unknown auth type: {type(auth)!r}")  # pragma: no cover
