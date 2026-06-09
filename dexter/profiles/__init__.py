"""User-profile persistence: saved commutes for returning riders.

A small, **LLM-free, domain-free** storage layer that sits beside the agent.
Dependencies point downward only — the agent imports this; this imports nothing
from the agent or the MBTA library. It stores primitive rows keyed by an opaque
``user_id`` (an anonymous per-browser token; no accounts, no PII). The agent maps
between its ``ResolvedTarget`` and the flat :class:`SavedCommute` here.
"""

from .models import SavedCommute
from .store import CommuteStore

__all__ = ["CommuteStore", "SavedCommute"]
