"""Entry point for ``python -m morning_star_review.agent <phase> <job_dir>``.

Exit codes (DESIGN 6.8): 0 = ``result.json`` written (any state); 3 = invalid job
(retrying will not help); anything else = crash (the launcher may retry; ``meter.json``
keeps the counts and the deadline).
"""

from __future__ import annotations

import asyncio
import os
import sys

from morning_star_review.core.redact import redact

EXIT_OK = 0
EXIT_CRASH = 1
EXIT_USAGE = 2
EXIT_INVALID_JOB = 3
PHASES = ("explore", "verify", "format")
# Settings that would send model requests, and the key, to another endpoint (DESIGN 6.8).
# The launcher never forwards them; the entry point drops them too, in case a caller does.
ENDPOINT_OVERRIDES = (
    "SERVER_URL",
    "MISTRAL_API_URL",
    "MISTRAL_SERVER_URL",
    "MISTRAL_PROVIDER_API_BASE",
    "AGENTS_SDK_COMPLETION_GATEWAY_API_BASE",
)


ORPHAN_POLL_S = 2.0


def _exit_when_orphaned(poll_s: float = ORPHAN_POLL_S) -> None:
    """Exit once the launching process is gone (the agent runs in its own session).

    A driver that dies without cleanup (SIGHUP of a closed terminal, kill -9, OOM)
    would otherwise leave the agent calling models until its own deadline, for nobody.
    The parent pid changes when the process is re-parented (to init or a subreaper).
    """
    import threading
    import time

    parent = os.getppid()
    if parent <= 1:
        return  # already orphaned at start: nothing to compare with

    def watch() -> None:
        while True:
            time.sleep(poll_s)
            if os.getppid() != parent:
                print("msreview-agent: the launching process exited; stopping", file=sys.stderr)
                sys.stderr.flush()
                os._exit(EXIT_CRASH)

    threading.Thread(target=watch, name="msreview-orphan-watch", daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] not in PHASES:
        print(
            f"usage: python -m morning_star_review.agent {{{'|'.join(PHASES)}}} <job_dir>",
            file=sys.stderr,
        )
        return EXIT_USAGE
    phase, job_dir = args
    for name in ENDPOINT_OVERRIDES:
        os.environ.pop(name, None)
    _exit_when_orphaned()
    # Imported here so a usage error does not pay for loading the Agents SDK.
    from morning_star_review.agent import jobio, runner

    try:
        asyncio.run(runner.run_job(phase, job_dir))  # type: ignore[arg-type]
    except jobio.InvalidJob as exc:
        print(f"msreview-agent: invalid job: {exc}", file=sys.stderr)
        return EXIT_INVALID_JOB
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # every other failure is a retryable crash
        key = os.environ.get("MISTRAL_API_KEY", "")
        text = redact(f"{type(exc).__name__}: {exc}", [key] if key else None)
        print(f"msreview-agent: crash: {text[:2000]}", file=sys.stderr)
        return EXIT_CRASH
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
