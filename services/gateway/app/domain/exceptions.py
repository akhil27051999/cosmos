"""
Domain-level exceptions.

These describe things that can go *wrong in the business rules*, in the
domain's own vocabulary — never in HTTP terms. The web layer (api/errors.py)
is the only place that decides "InvalidTransition => 409". Keeping the mapping
at the edge is what lets the domain stay framework-free.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class so the web layer can catch the whole family at once."""


class JobNotFound(DomainError):
    """Asked for a job id that doesn't exist."""


class InvalidTransition(DomainError):
    """Attempted a state change the job's state machine forbids."""


class VersionConflict(DomainError):
    """OCC failure: the job was modified by someone else since it was read.

    The caller sent an `expected_version` that no longer matches what's stored,
    so we refuse the write rather than silently clobber the other change.
    """


# ----- Stage 6: identity / multi-tenancy ---------------------------------
# These are still framework-free: the web layer (api/errors.py) maps them to
# 401 / 403 / 429. The domain only says *what* went wrong, never the HTTP code.


class Unauthenticated(DomainError):
    """No credential, or an invalid/revoked API key. -> HTTP 401."""


class Forbidden(DomainError):
    """Authenticated, but not allowed — e.g. the tenant is suspended, or the
    caller is reaching for another tenant's resource. -> HTTP 403."""


class QuotaExceeded(DomainError):
    """The tenant has used up its allowance for the current period. -> HTTP 429."""


class ServiceUnavailable(DomainError):
    """An upstream dependency we call synchronously is unreachable (e.g. the
    worker's gRPC control plane is down). -> HTTP 503."""
