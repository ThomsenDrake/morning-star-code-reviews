"""Stand-in for ``python -m morning_star_review.agent <phase> <job_dir>`` in workflow tests.

Invoked through a small wrapper script as::

    stub_agent.py --scenario <scenario.json> -m morning_star_review.agent <phase> <job_dir>

It checks the job like the real runner (``job.json`` against the contract, the input
file against ``input_sha256``, ``candidates.json`` for verify jobs), records the call in
the scenario's log, and writes a canned ``result.json``.

Scenario JSON::

    {
      "log": "<calls.jsonl>",
      "env_dump": "<dir>",              # optional: write env-<phase>.json there
      "specs": {"<key>": {...spec...}}
    }

Spec keys are tried in this order: ``format:<target>:<model>``, ``format:<target>``
(format jobs only), ``<phase>:<model>``, ``<phase>``. A spec may set ``notes``,
``draft``, ``state``, ``stop_reason``, ``complete``, ``rescued``,
``budget_exhausted``, ``exhausted_by``, ``opened_paths``, ``usage`` (Usage fields),
``error``, ``exit_code`` (exit without a result), ``crash_runs`` (exit 1 on the first N
runs of this job directory) and ``sleep`` (seconds before finishing).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from morning_star_review.core.contract import AgentJob, AgentRunResult, Usage


def _spec(specs: dict[str, Any], phase: str, model: str, target: str) -> dict[str, Any]:
    keys = [f"{phase}:{model}", phase]
    if phase == "format":
        keys = [f"format:{target}:{model}", f"format:{target}", *keys]
    for key in keys:
        if key in specs:
            return dict(specs[key])
    return {}


def _verdicts(cands: list[dict[str, Any]], by_title: dict[str, str]) -> list[dict[str, Any]]:
    """One verdict per candidate whose title contains a key of ``by_title``."""
    out = []
    for c in cands:
        for needle, status in by_title.items():
            if needle.lower() in c["title"].lower():
                out.append(
                    {"candidate_id": c["candidate_id"], "status": status, "reason": "checked"}
                )
                break
    return out


def _verdict_notes(verdicts: list[dict[str, Any]]) -> str:
    blocks = [
        f"VERDICT {v['candidate_id']}\nstatus: {v['status']}\nreason: {v['reason']}\nEND VERDICT"
        for v in verdicts
    ]
    return "\n\n".join([*blocks, "VERIFY COMPLETE"]) + "\n"


def _runs(job_dir: Path) -> int:
    counter = job_dir / "stub_runs.txt"
    n = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(n))
    return n


def main(argv: list[str]) -> int:
    if len(argv) != 6 or argv[0] != "--scenario" or argv[2] != "-m":
        print(f"stub agent: bad arguments {argv!r}", file=sys.stderr)
        return 2
    scenario = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    phase, job_dir = argv[4], Path(argv[5])
    job = AgentJob.model_validate_json((job_dir / "job.json").read_text(encoding="utf-8"))
    if job.phase != phase:
        print("stub agent: phase mismatch", file=sys.stderr)
        return 3
    input_path = Path(job.input_path)
    if not input_path.is_absolute():
        input_path = job_dir / input_path
    data = input_path.read_bytes()
    if hashlib.sha256(data).hexdigest() != job.input_sha256:
        print("stub agent: input digest mismatch", file=sys.stderr)
        return 3
    cands: list[dict[str, Any]] = []
    if phase == "verify" or (phase == "format" and job.format_target == "verify"):
        cands = json.loads((job_dir / "candidates.json").read_text(encoding="utf-8"))
    candidate_ids = [c["candidate_id"] for c in cands]
    runs = _runs(job_dir)
    entry = {
        "phase": phase,
        "model": job.model,
        "target": job.format_target,
        "job_dir": job_dir.name,
        "run": runs,
        "cwd_empty": not any(Path.cwd().iterdir()),
        "candidates": candidate_ids,
        "input_chars": len(data.decode("utf-8", "replace")),
        "max_tool_calls": job.budgets.max_tool_calls,
        "max_cost_usd": job.budgets.max_cost_usd,
        "diffs_present": all(
            (job_dir / ref.diff_file).is_file() for ref in job.changed_files if ref.diff_file
        ),
    }
    with Path(scenario["log"]).open("a", encoding="utf-8") as log:
        log.write(json.dumps(entry) + "\n")
    if scenario.get("env_dump"):
        dump = Path(scenario["env_dump"]) / f"env-{phase}.json"
        dump.write_text(json.dumps({**os.environ, "__cwd__": os.getcwd()}), encoding="utf-8")

    spec = _spec(scenario.get("specs", {}), phase, job.model, job.format_target)
    if spec.get("sleep"):
        time.sleep(float(spec["sleep"]))
    if runs <= int(spec.get("crash_runs", 0)):
        print("stub agent: simulated crash", file=sys.stderr)
        return 1
    if "exit_code" in spec:
        print("stub agent: simulated exit", file=sys.stderr)
        return int(spec["exit_code"])
    by_title = spec.get("verdicts_by_title")
    if by_title is not None:
        verdicts = _verdicts(cands, by_title)
        if phase == "verify":
            spec["notes"] = _verdict_notes(verdicts)
        else:
            spec["draft"] = {"entries": verdicts}
    usage = Usage(model=job.model, **spec.get("usage", {"llm_calls": 3, "tool_calls": 2}))
    result = AgentRunResult(
        phase=phase,
        state=spec.get("state", "Completed"),
        stop_reason=spec.get("stop_reason"),
        notes=spec.get("notes") if phase != "format" else None,
        draft=spec.get("draft") if phase == "format" else None,
        rescued=spec.get("rescued", False),
        complete=spec.get("complete", phase != "format" or spec.get("draft") is not None),
        budget_exhausted=spec.get("budget_exhausted", False),
        exhausted_by=spec.get("exhausted_by", []),
        opened_paths=spec.get("opened_paths", []),
        usage=usage,
        error=spec.get("error"),
    )
    (job_dir / "result.json").write_text(result.model_dump_json(), encoding="utf-8")
    print(f"stub agent: {phase} done for {job.model}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
