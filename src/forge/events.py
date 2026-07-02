"""The engine's event vocabulary. Every SessionManager flow is a generator of
TurnEvents; the web SSE stream and the Slack renderer both consume them, so
kind/data field names are a cross-surface contract (see docs + slackbot)."""
from dataclasses import dataclass


@dataclass(frozen=True)
class TurnEvent:
    kind: str
    data: dict
