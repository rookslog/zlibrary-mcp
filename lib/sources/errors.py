"""Provider-attributed errors for the multi-source search path.

Every failure in this path names WHICH provider failed and WHY, because the
caller's next move differs by reason: a DNS failure means the domain is gone or
blocked and the mirror list needs updating, whereas a connect timeout means the
host resolves but its packets are being dropped and a different mirror may
work. Collapsing both into a bare "search failed" (or, worse, into an empty
result list) is what made an unreachable provider indistinguishable from a
query that legitimately matched nothing.

Reason codes are stable strings so callers can branch on them without parsing
prose. They are surfaced to the MCP caller inside the error envelope.
"""

from typing import Any, Dict, List, Optional

# Human-readable gloss per reason code. Keep these phrased as a predicate that
# reads naturally after "<provider> (<host>) ...".
REASON_TEXT = {
    "dns_failure": "could not be resolved (DNS returned no address)",
    "dns_timeout": "did not resolve before the DNS probe deadline",
    "connect_timeout": "resolved but did not accept a connection before the deadline",
    "connect_refused": "resolved but actively refused the connection",
    "connect_error": "resolved but the connection failed",
    "tls_error": "accepted the connection but the TLS handshake failed",
    "read_timeout": "accepted the connection but sent no response before the deadline",
    "search_timeout": "did not finish the search before the deadline",
    "http_error": "returned an HTTP error",
    "quota_exhausted": "has no downloads left on this account today",
    "protocol_error": "returned a malformed response",
    "integrity_mismatch": "returned bytes that did not match the requested digest",
    "configuration_error": "cannot run with the supplied configuration",
    "unknown": "failed for an unclassified reason",
}

# Reasons that mean "this host is not talking to us at all", as opposed to a
# host that answered with something we did not like. Only these justify trying
# a different mirror or provider without further thought.
UNREACHABLE_REASONS = frozenset(
    {
        "dns_failure",
        "dns_timeout",
        "connect_timeout",
        "connect_refused",
        "connect_error",
        "tls_error",
    }
)


class SourceError(Exception):
    """Base class for a failure attributed to one source provider.

    Attributes:
        provider: Provider name ('annas' or 'libgen')
        host: Hostname that failed, when known
        reason: Stable reason code, a key of REASON_TEXT
        detail: Free-text extra context (exception type, HTTP status, ...)
    """

    reason = "unknown"

    def __init__(
        self,
        provider: str,
        host: str = "",
        detail: str = "",
        reason: Optional[str] = None,
    ):
        self.provider = provider
        self.host = host
        self.detail = detail
        if reason:
            self.reason = reason
        super().__init__(self._build_message())

    def _build_message(self) -> str:
        where = f" ({self.host})" if self.host else ""
        gloss = REASON_TEXT.get(self.reason, self.reason)
        tail = f" [{self.detail}]" if self.detail else ""
        return f"{self.provider}{where} {gloss}{tail}"

    @property
    def unreachable(self) -> bool:
        """Whether this failure means the host never answered."""
        return self.reason in UNREACHABLE_REASONS

    def to_dict(self) -> Dict[str, Any]:
        """Machine-readable form for the MCP error envelope."""
        return {
            "provider": self.provider,
            "host": self.host,
            "reason": self.reason,
            "detail": self.detail,
            "message": str(self),
        }


class ProviderUnreachableError(SourceError):
    """The provider's host never completed a connection.

    Raised by the pre-flight probe and by adapters that classify a transport
    failure. Distinguishes DNS failure from connect timeout via `reason`.
    """

    reason = "connect_error"


class ProviderTimeoutError(SourceError):
    """The provider connected but the operation exceeded its wall-clock budget.

    Also raised when a blocking third-party call is abandoned: see
    `lib/sources/net.run_bounded`, which cannot interrupt the underlying
    socket and instead abandons a daemon thread.
    """

    reason = "search_timeout"


class ProviderResponseError(SourceError):
    """The provider answered, but with an error or an unparseable body."""

    reason = "http_error"


class ProviderConfigurationError(SourceError, ValueError):
    """The caller selected a provider without its required configuration.

    This remains a ValueError for adapter callers while carrying a stable
    source reason through the bridge envelope. It is permanent until the
    caller changes configuration and therefore must not count as dependency
    health evidence.
    """

    reason = "configuration_error"


class AllSourcesFailedError(Exception):
    """Every candidate provider (or mirror) failed.

    Carries the individual failures so the caller can see that, say, Anna's
    failed DNS while LibGen timed out connecting — two different problems that
    a single flattened message would hide.
    """

    def __init__(self, operation: str, failures: List[SourceError]):
        self.operation = operation
        self.failures = failures
        summary = "; ".join(str(f) for f in failures) or "no providers attempted"
        super().__init__(f"{operation} failed on every source: {summary}")

    def to_dict(self) -> Dict[str, Any]:
        """Machine-readable form for the MCP error envelope."""
        return {
            "operation": self.operation,
            "failures": [f.to_dict() for f in self.failures],
            "message": str(self),
        }
