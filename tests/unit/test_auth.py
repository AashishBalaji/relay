import pytest

from relay.client.auth import MissingCredentialError, apply_auth
from relay.manifest.schema import ApiKeyAuth, BasicAuth, NoAuth, OAuth2Auth


def test_no_auth_does_nothing():
    headers, params = {}, {}
    result = apply_auth(NoAuth(), {}, headers, params)
    assert result is None
    assert headers == {} and params == {}


def test_api_key_header():
    auth = ApiKeyAuth(name="Authorization", location="header", value_prefix="Bearer ")
    headers, params = {}, {}
    apply_auth(auth, {"api_key": "secret123"}, headers, params)
    assert headers == {"Authorization": "Bearer secret123"}
    assert params == {}


def test_api_key_query():
    auth = ApiKeyAuth(name="api_key", location="query")
    headers, params = {}, {}
    apply_auth(auth, {"api_key": "secret123"}, headers, params)
    assert params == {"api_key": "secret123"}
    assert headers == {}


def test_api_key_missing_credential_raises():
    auth = ApiKeyAuth(name="Authorization")
    with pytest.raises(MissingCredentialError):
        apply_auth(auth, {}, {}, {})


def test_basic_auth_returns_tuple():
    result = apply_auth(BasicAuth(), {"username": "u", "password": "p"}, {}, {})
    assert result == ("u", "p")


def test_basic_auth_missing_credential_raises():
    with pytest.raises(MissingCredentialError):
        apply_auth(BasicAuth(), {"username": "u"}, {}, {})


def test_oauth2_sets_bearer_header():
    headers, params = {}, {}
    apply_auth(OAuth2Auth(token_url="https://example.com/token"), {"access_token": "tok"}, headers, params)
    assert headers == {"Authorization": "Bearer tok"}


def test_oauth2_missing_credential_raises():
    with pytest.raises(MissingCredentialError):
        apply_auth(OAuth2Auth(token_url="https://example.com/token"), {}, {}, {})
