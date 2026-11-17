"""Review domain: anchoring, merging, verification policy, verdict and rendering.

Everything here is pure: no network, no clock, no model calls. ``finalize`` is the
entry point the ``finalize`` activity calls; ``merge.merge_drafts`` serves the
``merge`` activity of the ensemble pipeline.
"""

from __future__ import annotations

from morning_star_review.review.final import RunInfo, finalize

__all__ = ["RunInfo", "finalize"]
