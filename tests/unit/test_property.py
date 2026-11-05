"""Property-based tests (Hypothesis) sweeping the input space rather than
a handful of hand-picked examples -- the same validation philosophy used
elsewhere in this portfolio (Ledger's EOQ/safety-stock tests): examples
prove the formula matches a known case, properties prove it holds in
general.
"""
import string

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from relay.client.mapping import extract_fields
from relay.client.rate_limit import RateLimiter
from relay.manifest.schema import ConnectorManifest, EndpointSpec, FieldMapping

_key = st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=6)
_leaf_value = st.one_of(st.integers(), st.text(max_size=10), st.booleans(), st.none())


@st.composite
def _nested_path_and_value(draw, max_depth=3):
    depth = draw(st.integers(min_value=0, max_value=max_depth))
    path_parts = [draw(_key) for _ in range(depth + 1)]
    value = draw(_leaf_value)
    node = value
    for part in reversed(path_parts):
        node = {part: node}
    return node, ".".join(path_parts), value


@given(_nested_path_and_value())
def test_extract_fields_finds_every_resolvable_nested_path(data):
    """For ANY nested dict built from a known path, extracting that exact
    path must round-trip the original value and never report it missing --
    regardless of depth, key names, or whether the value itself is falsy
    (0, "", False, None all have to come through correctly, not be
    confused with 'this path doesn't resolve')."""
    item, path, expected_value = data
    fields = {"target": FieldMapping(source_path=path, required=True)}
    normalized, missing = extract_fields(item, fields)
    assert missing == []
    assert normalized["target"] == expected_value


@given(
    max_calls=st.integers(min_value=1, max_value=5),
    n_calls=st.integers(min_value=1, max_value=25),
)
def test_rate_limiter_never_exceeds_max_calls_in_any_window(max_calls, n_calls):
    """No matter how many calls are requested back-to-back, no sliding
    window of length window_seconds should ever contain more than
    max_calls recorded acquisitions -- that's the entire point of a rate
    limiter, so it's worth checking as a general property rather than one
    or two fixed scenarios."""
    now = [0.0]

    def clock():
        return now[0]

    def sleep(duration):
        now[0] += duration

    window = 1.0
    limiter = RateLimiter(max_calls=max_calls, window_seconds=window, clock=clock, sleep=sleep)
    timestamps = []
    for _ in range(n_calls):
        limiter.acquire()
        timestamps.append(now[0])
        now[0] += 0.01  # a touch of simulated work between calls

    # A tiny epsilon guards against float rounding at the exact window
    # boundary (e.g. 2.01 - 1.0 == 1.0099999999999998 in binary floating
    # point) putting a boundary timestamp a hair on the wrong side of the
    # comparison -- a test artifact, not something the limiter itself
    # needs to worry about, since real monotonic clocks essentially never
    # land on an exact boundary either.
    epsilon = 1e-9
    for t in timestamps:
        in_window = sum(1 for x in timestamps if t - window + epsilon < x <= t)
        assert in_window <= max_calls


def _make_manifest(version: str) -> ConnectorManifest:
    return ConnectorManifest(
        name="x", version=version, base_url="https://example.com",
        endpoints=[EndpointSpec(name="a", path="/a", fields={"id": FieldMapping(source_path="id")})],
    )


@given(
    major=st.integers(min_value=0, max_value=999),
    minor=st.integers(min_value=0, max_value=999),
    patch=st.integers(min_value=0, max_value=999),
)
def test_any_semver_shaped_version_is_accepted(major, minor, patch):
    version = f"{major}.{minor}.{patch}"
    assert _make_manifest(version).version == version


@given(
    bad=st.text(min_size=1, max_size=10).filter(
        lambda s: s.count(".") != 2 or not all(p.isdigit() for p in s.split("."))
    )
)
def test_any_non_semver_version_is_rejected(bad):
    with pytest.raises(ValidationError):
        _make_manifest(bad)
