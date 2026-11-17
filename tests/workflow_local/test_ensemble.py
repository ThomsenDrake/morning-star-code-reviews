"""Ensemble + verify pipeline runs (ADDENDUM D), in-process with the stub agent."""

from __future__ import annotations

import json

import pytest
from harness import Harness
from wfkit import AUTHZ, ROUNDING, SQLI, SWAP, notes_for

from morning_star_review.core.contract import FinalFinding

pytestmark = pytest.mark.timeout(120)

GLM, LARGE, DEEPSEEK = "zai-glm-5-3", "mistral-large-4", "deepseek-v4-1-flash-1017"
OPENED = ["app/db.py", "app/handlers.py", "app/service.py"]


def _explorers(h: Harness) -> None:
    h.spec(f"explore:{GLM}", notes=notes_for(h.repo, SQLI, AUTHZ), opened_paths=OPENED)
    h.spec(f"explore:{LARGE}", notes=notes_for(h.repo, SQLI, AUTHZ, SWAP), opened_paths=OPENED)
    h.spec(f"explore:{DEEPSEEK}", notes=notes_for(h.repo, SQLI, ROUNDING), opened_paths=OPENED)


def _by_title(findings: list[FinalFinding], title: str) -> FinalFinding:
    return next(f for f in findings if f.title == title)


async def test_three_explorers_merge_and_verify(harness: Harness) -> None:
    h = harness
    _explorers(h)
    h.spec(
        "verify",
        verdicts_by_title={
            "SQL injection": "confirmed",
            "swapped": "confirmed",
            "Inverted admin": "uncertain",
            "Rounding": "refuted",
        },
    )
    out = await h.run(h.request(preset="best"))
    assert out.status == "posted", out.reason

    # Three explorer jobs (one directory each), no formatter, one verifier.
    explores = h.calls("explore")
    assert sorted(c["model"] for c in explores) == sorted([GLM, LARGE, DEEPSEEK])
    assert {c["job_dir"] for c in explores} == {f"explore-{m}" for m in (GLM, LARGE, DEEPSEEK)}
    (ver,) = h.calls("verify")
    assert ver["model"] == GLM and ver["job_dir"] == "verify"
    assert len(ver["candidates"]) == 4
    assert h.calls("format") == []

    sqli = _by_title(out.findings, SQLI[2])
    assert sqli.verify_status == "confirmed" and sqli.state == "open"
    assert sorted(sqli.votes) == sorted([GLM, LARGE, DEEPSEEK])
    swap = _by_title(out.findings, SWAP[2])
    assert swap.verify_status == "confirmed" and swap.votes == [LARGE]
    # Uncertain with 2 votes and high severity: inline, tagged unconfirmed.
    authz = _by_title(out.findings, AUTHZ[2])
    assert authz.verify_status == "uncertain" and authz.state == "open"
    assert sorted(authz.votes) == sorted([GLM, LARGE])
    # Refuted: dropped, counted, and kept for the report JSON.
    assert ROUNDING[2] not in {f.title for f in out.findings}
    assert [r.title for r in out.refuted] == [ROUNDING[2]]

    bodies = {c["path"]: c["body"] for c in h.fake.review_comments}
    assert set(bodies) == {"app/db.py", "app/handlers.py", "app/service.py"}
    assert "(unconfirmed)" in bodies["app/handlers.py"]
    assert "(unconfirmed)" not in bodies["app/db.py"]
    assert out.verdict == "findings"
    assert {f"explorer:{m}" for m in (GLM, LARGE, DEEPSEEK)} | {f"verifier:{GLM}"} <= set(out.usage)
    findings_json = json.loads((h.run_dir / "report" / "findings.json").read_text())
    assert [r["title"] for r in findings_json["refuted"]] == [ROUNDING[2]]
    # The verifier's prompt lists each candidate on a header line inside an untrusted block.
    prompt = (h.run_dir / "verify" / "prompt.txt").read_text()
    assert sum(1 for line in prompt.splitlines() if line.startswith("CANDIDATE ")) == 4


async def test_single_vote_uncertain_stays_in_the_summary(harness: Harness) -> None:
    h = harness
    _explorers(h)
    # The verifier never addresses the swapped-arguments candidate: it counts as uncertain.
    h.spec(
        "verify",
        verdicts_by_title={
            "SQL injection": "confirmed",
            "Inverted admin": "confirmed",
            "Rounding": "refuted",
        },
    )
    out = await h.run(h.request(preset="best"))
    swap = _by_title(out.findings, SWAP[2])
    assert swap.verify_status == "uncertain"
    assert swap.state == "summary_only"
    assert "app/service.py" not in {c["path"] for c in h.fake.review_comments}


async def test_a_crashed_explorer_is_left_out(harness: Harness) -> None:
    h = harness
    _explorers(h)
    h.spec(f"explore:{DEEPSEEK}", crash_runs=99)
    h.spec(
        "verify",
        verdicts_by_title={
            "SQL injection": "confirmed",
            "Inverted": "confirmed",
            "swapped": "confirmed",
        },
    )
    out = await h.run(h.request(preset="best"))

    assert out.status == "posted"
    assert out.verdict != "clean"
    sqli = _by_title(out.findings, SQLI[2])
    assert sorted(sqli.votes) == sorted([GLM, LARGE])
    assert len([c for c in h.calls("explore") if c["model"] == DEEPSEEK]) == 2


async def test_lossy_verifier_notes_use_the_formatter(harness: Harness) -> None:
    h = harness
    _explorers(h)
    h.spec("verify", notes="All of them look right to me, c1 and c2 are real.\nVERIFY COMPLETE\n")
    h.spec(
        "format:verify",
        verdicts_by_title={
            "SQL injection": "confirmed",
            "Inverted": "confirmed",
            "swapped": "confirmed",
            "Rounding": "refuted",
        },
    )
    out = await h.run(h.request(preset="best"))

    (fmt,) = h.calls("format")
    assert fmt["target"] == "verify" and fmt["job_dir"] == "verify-format"
    assert len(fmt["candidates"]) == 4
    assert out.status == "posted"
    assert all(f.verify_status == "confirmed" for f in out.findings)
    assert [r.title for r in out.refuted] == [ROUNDING[2]]


async def test_verifier_crash_leaves_candidates_unconfirmed(harness: Harness) -> None:
    h = harness
    _explorers(h)
    h.spec("verify", crash_runs=99)
    out = await h.run(h.request(preset="best"))

    assert out.status == "posted"
    assert out.verdict == "incomplete"
    assert {f.verify_status for f in out.findings} == {"uncertain"}
    inline = {c["path"] for c in h.fake.review_comments}
    # Two or more votes and high severity still go inline (tagged unconfirmed).
    assert {"app/db.py", "app/handlers.py"} <= inline
    assert "app/service.py" not in inline


async def test_balanced_preset_verifies_a_single_explorer(harness: Harness) -> None:
    h = harness
    h.spec("explore", notes=notes_for(h.repo, SQLI, ROUNDING), opened_paths=OPENED)
    h.spec("verify", verdicts_by_title={"SQL injection": "confirmed", "Rounding": "refuted"})
    out = await h.run(h.request(preset="balanced"))

    assert [c["model"] for c in h.calls("verify")] == [LARGE]
    assert [f.title for f in out.findings] == [SQLI[2]]
    assert [r.title for r in out.refuted] == [ROUNDING[2]]


async def test_no_candidates_skips_the_verifier(harness: Harness) -> None:
    h = harness
    for model in (GLM, LARGE, DEEPSEEK):
        h.spec(f"explore:{model}", notes=notes_for(h.repo), opened_paths=OPENED)
    out = await h.run(h.request(preset="best"))

    assert h.calls("verify") == []
    assert out.status == "posted"
    assert out.findings == []
    assert out.verdict == "clean"
