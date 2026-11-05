"""Policy checks a platform team would want enforced before a connector is
trusted -- in an App Shop, before it's listed; here, before `relay dev`
even bothers simulating it. Each check is independent and returns zero or
more LintFinding objects; nothing here touches the manifest."""
from __future__ import annotations

from dataclasses import dataclass

from relay.manifest.schema import ConnectorManifest

# Above this, a connector is almost certainly going to get itself
# rate-limited or banned by a real upstream; below 1 it can't make any
# requests at all, which is really a config bug, not a policy violation,
# but it belongs in the same bucket of "this manifest isn't ready".
_MAX_SANE_RATE_LIMIT = 1000
_MAX_SANE_RETRIES = 10

# A crude but useful net: catches the common "someone pasted a real key
# into the manifest while testing" mistake before it's committed anywhere.
# Not a substitute for a real secret scanner, and says so in its finding.
_SUSPICIOUS_VALUE_PREFIXES = ("sk-", "ghp_", "AKIA", "Bearer ey")


@dataclass
class LintFinding:
    severity: str  # "error" | "warning"
    check: str
    message: str


def _check_rate_limit(manifest: ConnectorManifest) -> list[LintFinding]:
    findings = []
    if manifest.rate_limit_per_window < 1:
        findings.append(
            LintFinding("error", "rate_limit", "rate_limit_per_window must be at least 1")
        )
    elif manifest.rate_limit_per_window > _MAX_SANE_RATE_LIMIT:
        findings.append(
            LintFinding(
                "warning", "rate_limit",
                f"rate_limit_per_window ({manifest.rate_limit_per_window}) is unusually high "
                f"(> {_MAX_SANE_RATE_LIMIT}) -- double check this is really what the upstream "
                "API allows, not a copy-paste default",
            )
        )
    return findings


def _check_retries(manifest: ConnectorManifest) -> list[LintFinding]:
    if manifest.max_retries > _MAX_SANE_RETRIES:
        return [
            LintFinding(
                "warning", "retries",
                f"max_retries ({manifest.max_retries}) is unusually high -- combined with "
                "exponential backoff this can mean minutes of retrying a single call before "
                "giving up",
            )
        ]
    return []


def _check_required_fields(manifest: ConnectorManifest) -> list[LintFinding]:
    findings = []
    for endpoint in manifest.endpoints:
        if endpoint.fields and not any(f.required for f in endpoint.fields.values()):
            findings.append(
                LintFinding(
                    "warning", "required_fields",
                    f"endpoint {endpoint.name!r} declares fields but marks none of them "
                    "required -- drift detection has nothing to notice a break with, since "
                    "nothing is ever 'missing'",
                )
            )
    return findings


def _check_no_embedded_secrets(manifest: ConnectorManifest) -> list[LintFinding]:
    findings = []
    candidates = [("base_url", manifest.base_url)]
    auth = manifest.auth
    if hasattr(auth, "name"):
        candidates.append(("auth.name", auth.name))
    if hasattr(auth, "value_prefix"):
        candidates.append(("auth.value_prefix", auth.value_prefix))
    if hasattr(auth, "token_url"):
        candidates.append(("auth.token_url", auth.token_url))
    for field_label, value in candidates:
        if any(value.startswith(p) for p in _SUSPICIOUS_VALUE_PREFIXES):
            findings.append(
                LintFinding(
                    "error", "embedded_secret",
                    f"{field_label} looks like it might contain a real credential, not a "
                    "config value -- manifests are committed to source control, so credentials "
                    "belong in the credentials/env vars relay test and relay drift-check "
                    "already support, never in the manifest itself. (This is a pattern check, "
                    "not a real secret scanner -- it can both miss real secrets and flag "
                    "harmless values; use a dedicated secret scanner for anything that matters.)",
                )
            )
    return findings


_CHECKS = [_check_rate_limit, _check_retries, _check_required_fields, _check_no_embedded_secrets]


def lint_manifest(manifest: ConnectorManifest) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for check in _CHECKS:
        findings.extend(check(manifest))
    return findings
