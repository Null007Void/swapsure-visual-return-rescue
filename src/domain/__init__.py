"""Pure domain rules for SwapSure.

Nothing here performs I/O beyond reading the frozen specification files, keeps
mutable state, or reaches the network.  Every function is deterministic in its
declared inputs (including an explicit clock value where time matters).
"""

from domain.eligibility import (
    ALL_STATUSES,
    ELIGIBLE_VISUAL_RESCUE,
    REFUND_REQUESTED,
    EligibilityDecision,
    EligibilityError,
    PolicyVersionMismatch,
    decide,
)
from domain.ranking import RankedCandidate, rank_candidates, ranking_key

__all__ = [
    "ALL_STATUSES",
    "ELIGIBLE_VISUAL_RESCUE",
    "REFUND_REQUESTED",
    "EligibilityDecision",
    "EligibilityError",
    "PolicyVersionMismatch",
    "decide",
    "RankedCandidate",
    "rank_candidates",
    "ranking_key",
]
