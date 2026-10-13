"""Generator for ccc agent definitions."""

__all__ = ["AgentGenError"]


class AgentGenError(Exception):
    """A build-stopping error with a message meant for the user."""
