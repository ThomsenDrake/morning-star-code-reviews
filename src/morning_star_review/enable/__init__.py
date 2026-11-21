"""``msreview enable | status | update | disable``: set up the review on repositories.

Talks to GitHub through the user's GitHub CLI login (``gh``). Secret values are passed
to ``gh`` on standard input, never on a command line, and are kept in memory only.
"""

from __future__ import annotations

import argparse

__all__ = ["run_command"]


def run_command(args: argparse.Namespace) -> int:
    """Run ``args.command`` (enable, status, update or disable); the exit code."""
    from morning_star_review.enable.commands import run

    return run(args)
