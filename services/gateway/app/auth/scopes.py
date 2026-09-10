"""
Authorization scopes (Stage 7) — the RBAC vocabulary.

Authentication answers "who are you?" (Stage 6 gave us the Principal + tenant).
**Authorization** answers "what may you do?" — and that's what scopes express.
A credential carries a set of scopes; each route requires one. Least privilege:
a read-only token can list/read jobs but can't submit or cancel.

OAuth2 convention is a space-delimited `scope` string in the token
("jobs:read jobs:write"); we parse that into a set.
"""

from __future__ import annotations

JOBS_READ = "jobs:read"     # list/get/events/document/view/stats
JOBS_WRITE = "jobs:write"   # submit/cancel

# API keys are full-access credentials in this build (a tenant's own root key);
# JWTs carry whatever scopes the token was issued with.
ALL: frozenset[str] = frozenset({JOBS_READ, JOBS_WRITE})


def parse_scopes(raw: str | list[str] | None) -> frozenset[str]:
    """Normalise a token's scope claim into a set.

    Accepts the OAuth2 space-delimited string, a list, or nothing (no scopes).
    """
    if not raw:
        return frozenset()
    if isinstance(raw, list):
        return frozenset(str(s) for s in raw)
    return frozenset(raw.split())
