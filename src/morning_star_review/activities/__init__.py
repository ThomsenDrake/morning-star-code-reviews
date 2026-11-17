"""Workflow activities and the ``ACTIVITIES`` registry (DESIGN 5.2, ADDENDUM D).

Each activity is a module-level ``async def`` taking one contract model, registered
with an explicit ``msreview_*`` name, and its first statement is ``guard(...)``. The
functions are reached through their modules (``activities.explore.explore``) so the
submodule names stay modules.
"""

from __future__ import annotations

from typing import Any

from morning_star_review.activities import (
    explore,
    fetch_pr,
    finalize,
    merge,
    prepare,
    publish,
    report_failure,
    structure,
    verify,
)

__all__ = ["ACTIVITIES", "ACTIVITY_NAMES"]

ACTIVITIES: tuple[Any, ...] = (
    fetch_pr.fetch_pr,
    prepare.prepare,
    explore.explore,
    structure.structure,
    merge.merge,
    verify.verify,
    finalize.finalize,
    publish.publish,
    report_failure.report_failure,
)

ACTIVITY_NAMES: tuple[str, ...] = tuple(a.__name__ for a in ACTIVITIES)
