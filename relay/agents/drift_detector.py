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

from relay.client.connector_client import ConnectorClient, UpstreamError
from relay.client.mapping import extract_fields
from relay.manifest.schema import ConnectorManifest, FieldMapping

_SAFE_SIMILARITY_THRESHOLD = 0.6


@dataclass
class FieldDrift:
    endpoint: str
    field_name: str
    expected_path: str
    suggested_path: str | None
    confidence: str  # "safe" | "unsafe"
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
            f"found {len(self.drifts)} drifted field(s) "
            f"({len(self.safe_drifts)} safe to auto-patch, {len(self.unsafe_drifts)} need review).",
            "",
        ]
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


def _suggest_rename(raw_item: dict, missing_path: str) -> tuple[str | None, float]:
    """Walks missing_path down through raw_item until a segment doesn't
    resolve, then fuzzy-matches that segment against the sibling keys
    actually present there. Returns (suggested_full_path, similarity) or
    (None, 0.0) if nothing plausible is found."""
    parts = missing_path.split(".")
    container: Any = raw_item
    resolved: list[str] = []

    for i, part in enumerate(parts):
        if isinstance(container, dict) and part in container:
            container = container[part]
            resolved.append(part)
            continue

        if not isinstance(container, dict):
            return None, 0.0

        candidates = difflib.get_close_matches(part, list(container.keys()), n=1, cutoff=0.4)
        if not candidates:
            return None, 0.0
        candidate_key = candidates[0]
        similarity = difflib.SequenceMatcher(None, part, candidate_key).ratio()

        # the rest of the path must resolve under the candidate for this to
        # count as a real suggestion, not just a coincidentally similar key
        cur = container[candidate_key]
        remaining = parts[i + 1 :]
        for rp in remaining:
            if isinstance(cur, dict) and rp in cur:
                cur = cur[rp]
            else:
                return None, 0.0

        return ".".join(resolved + [candidate_key] + remaining), similarity

    # every part resolved -- shouldn't happen (get_path would've succeeded),
    # but handle it rather than assume.
    return None, 0.0


def scan_connector(
    client: ConnectorClient,
    endpoint_names: list[str] | None = None,
    path_params_by_endpoint: dict[str, dict] | None = None,
    sample_size: int = 1,
) -> DriftReport:
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
        except UpstreamError as exc:
            report.drifts.append(
                FieldDrift(
                    endpoint=name, field_name="<endpoint>", expected_path=endpoint.path,
                    suggested_path=None, confidence="unsafe", reason=f"endpoint call failed: {exc}",
                )
            )
            continue

        for raw_item in raw_items[:sample_size]:
            _, missing = extract_fields(raw_item, endpoint.fields)
            for field_name in missing:
                mapping: FieldMapping = endpoint.fields[field_name]
                suggested_path, similarity = _suggest_rename(raw_item, mapping.source_path)
                if suggested_path is not None and similarity >= _SAFE_SIMILARITY_THRESHOLD:
                    report.drifts.append(
                        FieldDrift(
                            endpoint=name, field_name=field_name, expected_path=mapping.source_path,
                            suggested_path=suggested_path, confidence="safe",
                            reason=f"likely rename (similarity {similarity:.2f})",
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
