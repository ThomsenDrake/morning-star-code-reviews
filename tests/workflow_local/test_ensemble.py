"""Ensemble + verify pipeline runs (ADDENDUM D), in-process with the stub agent."""

from __future__ import annotations

import json
import time

import pytest
from harness import Harness
from wfkit import AUTHZ, ROUNDING, SQLI, SWAP, compare_body, notes_for, point_fake_at

from morning_star_review.activities.agent_launcher import AgentJobInvalid
from morning_star_review.core.contract import DraftFinding, FinalFinding, FormatterDraft

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


async def test_a_fatal_explorer_failure_cancels_the_others_at_once(harness: Harness) -> None:
    h = harness
    _explorers(h)
    # Two slow explorers and one that refuses its job (exit 3: AgentJobInvalid, fatal).
    for model in (GLM, LARGE):
        h.spec(f"explore:{model}", notes=notes_for(h.repo, SQLI), opened_paths=OPENED, sleep=60)
    h.spec(f"explore:{DEEPSEEK}", exit_code=3)
    started = time.monotonic()
    with pytest.raises(AgentJobInvalid):
        await h.run(h.request(preset="best"))

    # The run did not wait for the slow explorers: they were cancelled, which stops
    # their agent processes, and the failure was reported.
    assert time.monotonic() - started < 30
    assert {c["model"] for c in h.calls("explore")} == {GLM, LARGE, DEEPSEEK}
    assert [c["run"] for c in h.calls("explore") if c["model"] != DEEPSEEK] == [1, 1]
    assert h.calls("format") == [] and h.calls("verify") == []
    outcome = h.outcome_file()
    assert outcome["status"] == "failed"
    assert "explore" in outcome["reason"] and "AgentJobInvalid" in outcome["reason"]
    for model in (GLM, LARGE):
        assert not (h.run_dir / f"explore-{model}" / "result.json").exists()


PROSE_NOTES = (
    "I looked at the change. In app/db.py:6 the query is built with an f-string from the "
    "name, which is a SQL injection; severity high.\n"
    "SUMMARY: One injection problem.\nREVIEW COMPLETE\n"
)
NOTHING = {"findings": [], "prior_status": [], "summary": "Nothing to report."}


def _sqli_draft(h: Harness) -> dict[str, object]:
    path, evidence, title, severity = SQLI
    finding = DraftFinding(
        path=path,
        line=h.repo.line(path, evidence),
        severity=severity,  # type: ignore[arg-type]
        category="security",
        title=title,
        evidence=evidence.strip(),
        explanation="The name is pasted into the SQL text.",
    )
    return FormatterDraft(findings=[finding], prior_status=[], summary="One.").model_dump()


async def test_a_lossy_draft_from_any_explorer_blocks_clean(harness: Harness) -> None:
    h = harness
    for model in (GLM, LARGE):
        h.spec(f"explore:{model}", notes=notes_for(h.repo), opened_paths=OPENED)
    # Prose that names a location and a severity; the formatter finds nothing in it.
    h.spec(f"explore:{DEEPSEEK}", notes=PROSE_NOTES, opened_paths=OPENED)
    h.spec("format:review", draft=NOTHING, usage={"llm_calls": 1})
    out = await h.run(h.request(preset="best"))

    assert out.status == "posted", out.reason
    assert [c["job_dir"] for c in h.calls("format")] == [f"format-{DEEPSEEK}"]
    assert out.findings == []
    assert out.verdict == "incomplete"
    assert f"the {DEEPSEEK} explorer could not be fully parsed" in out.report_markdown
    assert out.usage[f"formatter:{LARGE}"].llm_calls == 1


async def test_every_explorers_formatter_usage_is_reported(harness: Harness) -> None:
    h = harness
    h.spec(f"explore:{GLM}", notes=PROSE_NOTES, opened_paths=OPENED)
    h.spec(f"explore:{LARGE}", notes=notes_for(h.repo, SQLI), opened_paths=OPENED)
    h.spec(f"explore:{DEEPSEEK}", notes=PROSE_NOTES, opened_paths=OPENED)
    h.spec("format:review", draft=_sqli_draft(h), usage={"llm_calls": 1, "output_tokens": 50})
    h.spec("verify", verdicts_by_title={"SQL injection": "confirmed"})
    out = await h.run(h.request(preset="best"))

    assert out.status == "posted", out.reason
    assert sorted(c["job_dir"] for c in h.calls("format")) == [
        f"format-{DEEPSEEK}",
        f"format-{GLM}",
    ]
    formatter = out.usage[f"formatter:{LARGE}"]
    assert (formatter.llm_calls, formatter.output_tokens) == (2, 100)
    (sqli,) = out.findings
    assert sorted(sqli.votes) == sorted([GLM, LARGE, DEEPSEEK])
    assert out.verdict == "findings"
    # Each explorer's sealed result and draft were kept for finalize, by seal.
    kept = sorted(p.name.split("-")[0] for p in (h.run_dir / "payloads").iterdir())
    assert kept == ["ExploreResult"] * 3 + ["ReviewDraft"] * 3


HANDLERS_FIXED = (
    '"""Request handlers."""\n'
    "\n"
    "\n"
    "def can_delete(user, item):\n"
    '    """Only admins and owners may delete."""\n'
    "    if user.is_admin:\n"
    "        return user.is_admin\n"
    "    return item.owner == user.name\n"
)
DB_FIXED = (
    '"""Database access."""\n'
    "\n"
    "\n"
    "def find_user(conn, name):\n"
    '    """Look a user up by name."""\n'
    '    query = "SELECT * FROM users WHERE name = ?"\n'
    "    return conn.execute(query, (name,)).fetchone()\n"
)


async def test_prior_claims_of_every_explorer_count_and_the_primary_wins(
    harness: Harness,
) -> None:
    h = harness
    for model in (GLM, LARGE, DEEPSEEK):
        h.spec(f"explore:{model}", notes=notes_for(h.repo, SQLI, AUTHZ), opened_paths=OPENED)
    h.spec("verify", verdicts_by_title={"SQL injection": "confirmed", "Inverted": "confirmed"})
    first = await h.run(h.request(preset="best"))
    assert first.status == "posted", first.reason
    ids = {f.title: f.id for f in first.findings}
    sqli_id, authz_id = ids[SQLI[2]], ids[AUTHZ[2]]

    # Both flagged lines are gone at the new head.
    prior = h.repo.head
    h.repo.commit({"app/db.py": DB_FIXED, "app/handlers.py": HANDLERS_FIXED}, "fix both")
    point_fake_at(h.fake, h.repo)
    h.fake.compare[f"{prior}...{h.repo.head}"] = compare_body(prior)
    h.reset_calls()

    def claims(sqli: str, authz: str) -> str:
        return f"PRIOR {sqli_id}: {sqli} - checked\nPRIOR {authz_id}: {authz} - checked\n"

    # The primary says the injection is fixed and is unsure about the check; another
    # explorer says both are still open, the third says nothing.
    h.spec(f"explore:{GLM}", notes=notes_for(h.repo, priors=claims("fixed", "unsure")))
    h.spec(f"explore:{LARGE}", notes=notes_for(h.repo, priors=claims("still_open", "still_open")))
    h.spec(f"explore:{DEEPSEEK}", notes=notes_for(h.repo))
    for model in (GLM, LARGE, DEEPSEEK):
        h.specs[f"explore:{model}"]["opened_paths"] = OPENED
    h.save()
    out = await h.run(h.request(preset="best"))
    assert out.status == "posted", out.reason

    # The primary's "fixed" wins over "still_open": the injection thread gets resolved.
    # Its "unsure" is no claim, so the other explorer's "still_open" keeps the check open.
    replies = [c["body"] for c in h.fake.review_comments if c["in_reply_to_id"] is not None]
    assert any(sqli_id in body for body in replies)
    assert not any(authz_id in body for body in replies)
