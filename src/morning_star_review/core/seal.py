"""HMAC seals on payloads and the canonical JSON they are computed over (DESIGN 4.3).

``seal`` = hex HMAC-SHA256(key=RunContext.seal_key, msg=execution_id + "\\x00" +
type name + "\\x00" + canonical_json(model without its own seal)). A payload sealed by
another process, for another execution, as another type, or changed after sealing
does not verify.
"""

from __future__ import annotations

import hashlib
import hmac

from pydantic import BaseModel

from morning_star_review.core.contract import ReviewRequest, Sealed
from morning_star_review.core.jsonio import canonical_dumps, sha256_text
from morning_star_review.core.runctx import RunContext, run_context

__all__ = ["canonical_json", "compute_seal", "request_digest", "seal", "verify"]


def canonical_json(model: BaseModel) -> str:
    """JSON of ``model`` without its top-level ``seal``, with sorted keys and no spaces.

    Nested sealed payloads keep their seals, so an outer seal also covers them.
    """
    data = model.model_dump(mode="json", exclude={"seal"})
    return canonical_dumps(data)


def request_digest(req: ReviewRequest) -> str:
    """sha256 hex of ``canonical_json(req)`` (the request pin)."""
    return sha256_text(canonical_json(req))


def compute_seal(model: Sealed, key: bytes, execution_id: str) -> str:
    msg = f"{execution_id}\x00{type(model).__name__}\x00{canonical_json(model)}"
    return hmac.new(key, msg.encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()


def seal[S: Sealed](model: S, ctx: RunContext | None = None) -> S:
    """A copy of ``model`` with ``seal`` set for this run (``ctx`` defaults to the current)."""
    c = ctx if ctx is not None else run_context()
    return model.model_copy(update={"seal": compute_seal(model, c.seal_key, c.execution_id)})


def verify(model: Sealed, ctx: RunContext | None = None) -> bool:
    """True when ``model.seal`` was made by this run for exactly this content (constant time)."""
    if not model.seal:
        return False
    c = ctx if ctx is not None else run_context()
    expected = compute_seal(model, c.seal_key, c.execution_id)
    return hmac.compare_digest(model.seal.encode("ascii", "replace"), expected.encode("ascii"))
