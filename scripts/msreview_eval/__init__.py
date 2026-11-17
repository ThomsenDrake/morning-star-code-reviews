"""Evaluation helpers shared by ``scripts/make_test_prs.py`` and ``scripts/eval.py``.

Everything here is deterministic and offline: case definitions (answer keys),
building the file tree of each case from the ledger fixture, and scoring
``msreview review --json`` outputs against the answer keys.
"""
