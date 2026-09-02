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
