"""Entry point for ``python -m morning_star_review`` (delegates to the CLI)."""

from morning_star_review.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
