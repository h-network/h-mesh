"""Watchdog: agent observability -- stalls, blocked deliveries, board hygiene.

Observe-only by design (see `service.Watchdog`'s docstring): this module
reports what it sees and never repairs an agent, ticket, or delivery itself.
"""

from .activity import ActivityTailer
from .presence import PresenceSampler
from .service import Watchdog, main, run_observers
from .verification import DeliveryVerifier

__all__ = [
    "ActivityTailer",
    "DeliveryVerifier",
    "PresenceSampler",
    "Watchdog",
    "main",
    "run_observers",
]
