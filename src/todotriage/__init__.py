"""Parse and lint TODO.md, keep the out-of-repo triage store, and answer queries."""

from __future__ import annotations


class TriageError(Exception):
    """A user-facing failure: bad input, validation, or a missing precondition."""
