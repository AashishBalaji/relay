"""The 'Zero KTLO' piece: scans a connector's live (or recorded) responses
against its manifest and catches upstream breaking changes automatically,
instead of a human noticing a connector quietly broke.

Three outcomes per missing required field:
  - a confident rename is found (e.g. "user" -> "author", same leaf key
    still present under it) -> SAFE: a patched manifest can be generated
    and applied without a human in the loop.
  - a rename candidate is found but the match is weak -> UNSAFE: reported,
    not auto-applied, because a wrong guess here silently corrupts data
    downstream, which is worse than the drift it was meant to catch.
  - no plausible candidate at all (the field may have been removed
    outright, not renamed) -> UNSAFE, reported for a human to investigate.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any

from relay.client.connector_client import ConnectorClient, UpstreamConnectionError, UpstreamError
from relay.client.mapping import extract_fields
from relay.manifest.schema import ConnectorManifest, FieldMapping

_SAFE_SIMILARITY_THRESHOLD = 0.6

# difflib's ratio() on very short strings is noisy: "id" vs "uid" scores
# 0.8, well clear of the threshold above, purely from character overlap on
# a 2-3 character key -- not because it's actually a confident rename.
# Below this length a match is never auto-classified "safe", however high
# its similarity score, and falls back to "unsafe" for human review.
_MIN_KEY_LENGTH_FOR_SAFE = 4


@dataclass
class FieldDrift:
    endpoint: str
    field_name: str
    expected_path: str
    suggested_path: str | None
    confidence: str  # "safe" | "unsafe" | "unreachable"
    reason: str


@dataclass
class DriftReport:
    connector_name: str
    checked_endpoints: list[str] = field(default_factory=list)
    drifts: list[FieldDrift] = field(default_factory=list)

    @property
    def has_drift(self) -> bool:
        return bool(self.drifts)

    @property
    def safe_drifts(self) -> list[FieldDrift]:
        return [d for d in self.drifts if d.confidence == "safe"]

    @property
    def unsafe_drifts(self) -> list[FieldDrift]:
        return [d for d in self.drifts if d.confidence == "unsafe"]

    @property
    def unreachable_endpoints(self) -> list[FieldDrift]:
        # Kept distinct from unsafe_drifts: "the endpoint couldn't be
        # called at all" (auth failure, network error, 5xx after retries)
        # is a different situation from "a field was renamed", and a
        # consumer paging on drift results needs to be able to tell them
        # apart -- one means "the connector is down", the other means
        # "the schema changed".
        return [d for d in self.drifts if d.confidence == "unreachable"]

    def to_markdown(self) -> str:
        if not self.has_drift:
            n = len(self.checked_endpoints)
            return (
                f"# Drift report: {self.connector_name}\n\n"
                f"No drift detected across {n} endpoint(s). All clear.\n"
            )
        lines = [
            f"# Drift report: {self.connector_name}",
            "",
            f"Checked {len(self.checked_endpoints)} endpoint(s); "
            f"found {len(self.drifts)} issue(s) "
            f"({len(self.safe_drifts)} safe to auto-patch, {len(self.unsafe_drifts)} need review, "
            f"{len(self.unreachable_endpoints)} unreachable).",
            "",
        ]
        if self.unreachable_endpoints:
            lines.append("## Could not be checked (endpoint unreachable)")
            for d in self.unreachable_endpoints:
                lines.append(f"- `{d.endpoint}`: {d.reason}")
            lines.append("")
        if self.safe_drifts:
            lines.append("## Safe to auto-patch")
            for d in self.safe_drifts:
                lines.append(
                    f"- `{d.endpoint}.{d.field_name}`: "
                    f"`{d.expected_path}` -> `{d.suggested_path}` ({d.reason})"
                )
            lines.append("")
        if self.unsafe_drifts:
            lines.append("## Needs human review")
            for d in self.unsafe_drifts:
                suggestion = f" (best guess: `{d.suggested_path}`)" if d.suggested_path else ""
                lines.append(
                    f"- `{d.endpoint}.{d.field_name}`: "
                    f"`{d.expected_path}` is gone{suggestion} -- {d.reason}"
                )
            lines.append("")
        return "\n".join(lines)


def _suggest_rename(raw_item: dict, missing_path: str) -> tuple[str | None, float, bool]:
    """Walks missing_path down through raw_item until a segment doesn't
    resolve, then fuzzy-matches that segment against the sibling keys
    actually present there. Returns (suggested_full_path, similarity,
    long_enough_for_safe) or (None, 0.0, False) if nothing plausible is
    found. `long_enough_for_safe` reflects the ACTUAL matched segment (the
    part that changed), not some other part of the path -- difflib's
    ratio() on short strings is noisy (e.g. "id" vs "uid" scores 0.8 on
    character overlap alone), so a rename of a short key is never eligible
    to be called "safe" regardless of its similarity score, even if the
    rest of the path is long."""
    parts = missing_path.split(".")
    container: Any = raw_item
    resolved: list[str] = []

    for i, part in enumerate(parts):
        if isinstance(container, dict) and part in container:
            container = container[part]
            resolved.append(part)
            continue

        if not isinstance(container, dict):
            return None, 0.0, False

        candidates = difflib.get_close_matches(part, list(container.keys()), n=1, cutoff=0.4)
        if not candidates:
            return None, 0.0, False
        candidate_key = candidates[0]
        similarity = difflib.SequenceMatcher(None, part, candidate_key).ratio()
        long_enough = (
            len(part) >= _MIN_KEY_LENGTH_FOR_SAFE and len(candidate_key) >= _MIN_KEY_LENGTH_FOR_SAFE
        )

        # the rest of the path must resolve under the candidate for this to
        # count as a real suggestion, not just a coincidentally similar key
        cur = container[candidate_key]
        remaining = parts[i + 1 :]
        for rp in remaining:
            if isinstance(cur, dict) and rp in cur:
                cur = cur[rp]
            else:
                return None, 0.0, False

        return ".".join(resolved + [candidate_key] + remaining), similarity, long_enough

    # every part resolved -- shouldn't happen (get_path would've succeeded),
    # but handle it rather than assume.
    return None, 0.0, False


def scan_connector(
    client: ConnectorClient,
    endpoint_names: list[str] | None = None,
    path_params_by_endpoint: dict[str, dict] | None = None,
    sample_size: int = 3,
) -> DriftReport:
    """Scans each endpoint's first page of results for drift against the
    manifest. sample_size defaults to 3, not 1: a rename decision made off
    a single sampled item is too easily skewed by one quirky/null record
    (either masking a real drift or manufacturing a false "safe" match on
    a coincidental key collision) -- a few items is a cheap way to cut
    that risk without materially increasing the number of upstream calls
    (still just one page per endpoint)."""
    manifest = client.manifest
    endpoint_names = endpoint_names or [e.name for e in manifest.endpoints]
    path_params_by_endpoint = path_params_by_endpoint or {}
    report = DriftReport(connector_name=manifest.name, checked_endpoints=list(endpoint_names))

    for name in endpoint_names:
        endpoint = manifest.endpoint(name)
        try:
            raw_items = client.fetch_raw_items(
                name, path_params=path_params_by_endpoint.get(name, {}), max_pages=1
            )
        except (UpstreamError, UpstreamConnectionError) as exc:
            report.drifts.append(
                FieldDrift(
                    endpoint=name, field_name="<endpoint>", expected_path=endpoint.path,
                    suggested_path=None, confidence="unreachable",
                    reason=f"endpoint call failed: {exc}",
                )
            )
            continue

        seen_fields: set[str] = set()
        for raw_item in raw_items[:sample_size]:
            _, missing = extract_fields(raw_item, endpoint.fields)
            for field_name in missing:
                if field_name in seen_fields:
                    # Already reported from an earlier sampled item in this
                    # endpoint -- sampling more than one item is meant to
                    # reduce false positives/negatives, not multiply the
                    # same finding once per sample.
                    continue
                seen_fields.add(field_name)
                mapping: FieldMapping = endpoint.fields[field_name]
                suggested_path, similarity, long_enough = _suggest_rename(
                    raw_item, mapping.source_path
                )
                confident = (
                    suggested_path is not None
                    and similarity >= _SAFE_SIMILARITY_THRESHOLD
                    and long_enough
                )
                if confident:
                    report.drifts.append(
                        FieldDrift(
                            endpoint=name, field_name=field_name, expected_path=mapping.source_path,
                            suggested_path=suggested_path, confidence="safe",
                            reason=f"likely rename (similarity {similarity:.2f})",
                        )
                    )
                elif suggested_path is not None and not long_enough:
                    report.drifts.append(
                        FieldDrift(
                            endpoint=name, field_name=field_name, expected_path=mapping.source_path,
                            suggested_path=suggested_path, confidence="unsafe",
                            reason=(
                                f"rename guess too short to trust (similarity {similarity:.2f} "
                                "on a short key -- not a reliable signal)"
                            ),
                        )
                    )
                elif suggested_path is not None:
                    report.drifts.append(
                        FieldDrift(
                            endpoint=name, field_name=field_name, expected_path=mapping.source_path,
                            suggested_path=suggested_path, confidence="unsafe",
                            reason=f"low-confidence rename guess (similarity {similarity:.2f})",
                        )
                    )
                else:
                    report.drifts.append(
                        FieldDrift(
                            endpoint=name, field_name=field_name, expected_path=mapping.source_path,
                            suggested_path=None, confidence="unsafe",
                            reason="no similar key found nearby -- field may have been removed",
                        )
                    )

    return report


def apply_safe_patches(manifest: ConnectorManifest, report: DriftReport) -> ConnectorManifest:
    """Returns a NEW manifest with every 'safe' drift's source_path updated
    to its suggested replacement. Never mutates the input, and never
    touches an 'unsafe' drift -- those always require a human to look."""
    patched = manifest.model_copy(deep=True)
    by_endpoint = {e.name: e for e in patched.endpoints}
    for drift in report.safe_drifts:
        endpoint = by_endpoint[drift.endpoint]
        endpoint.fields[drift.field_name].source_path = drift.suggested_path
    return patched
