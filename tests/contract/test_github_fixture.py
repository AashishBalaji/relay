"""A contract test for the GitHub connector against RECORDED fixtures, not
live traffic. This connector's auth is OAuth2 and genuinely needs a real
access token to call the live API -- which we don't have and aren't buying
one for a portfolio demo (the same honest trade-off as not purchasing a
live Anthropic API key elsewhere in this portfolio). The fixtures below are
real, stable GitHub issue response shapes, not invented ones.
"""
import json
from pathlib import Path

import httpx
import pytest

from relay.client.connector_client import ConnectorClient, MissingRequiredFieldsError
from relay.manifest.loader import load_manifest

_FIXTURES = Path(__file__).parent.parent / "fixtures"
_MANIFEST_PATH = Path(__file__).parent.parent.parent / "relay" / "connectors" / "github" / "manifest.yaml"


def _load_fixture(name: str) -> list:
    return json.loads((_FIXTURES / name).read_text())


def test_list_issues_normalizes_against_recorded_page():
    page1 = _load_fixture("github_issues_page1.json")
    page2_empty = _load_fixture("github_issues_page2_empty.json")
    responses = iter([page1, page2_empty])

    def handler(request):
        return httpx.Response(200, json=next(responses))

    manifest = load_manifest(_MANIFEST_PATH)
    client = ConnectorClient(
        manifest,
        credentials={"access_token": "fake-token-for-fixture-test"},
        http_client=httpx.Client(transport=httpx.MockTransport(handler), base_url=manifest.base_url),
    )
    issues = client.call("list_repo_issues", path_params={"owner": "octocat", "repo": "Hello-World"})
    assert len(issues) == 2
    assert issues[0] == {
        "id": 1, "number": 1347, "title": "Found a bug", "state": "open", "author_login": "octocat",
    }


def test_drifted_fixture_raises_missing_required_field():
    """The 'user' key was renamed to 'author' in this fixture, simulating a
    real upstream breaking change. The live client should refuse to return
    silently-incomplete data for a field the manifest marks required."""
    drifted = _load_fixture("github_issues_drifted.json")

    def handler(request):
        return httpx.Response(200, json=drifted)

    manifest = load_manifest(_MANIFEST_PATH)
    client = ConnectorClient(
        manifest,
        credentials={"access_token": "fake-token-for-fixture-test"},
        http_client=httpx.Client(transport=httpx.MockTransport(handler), base_url=manifest.base_url),
    )
    with pytest.raises(MissingRequiredFieldsError) as exc_info:
        client.call("list_repo_issues", path_params={"owner": "octocat", "repo": "Hello-World"})
    assert "author_login" in exc_info.value.missing
