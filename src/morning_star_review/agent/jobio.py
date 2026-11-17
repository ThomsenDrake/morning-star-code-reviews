"""Reading agent jobs and writing agent results in a job directory (DESIGN 6.8).

A job directory holds ``job.json`` (an ``AgentJob``), the phase input it names
(``prompt.txt`` or ``notes.txt``, checked against its SHA-256), and the files the
runner maintains: ``meter.json``, ``coverage.json``, ``notes.partial.txt`` and,
at the end, ``result.json``. The verify phase also reads ``candidates.json``.
Paths in ``job.json`` are absolute or relative to the job directory.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from morning_star_review.core.contract import AgentJob, AgentRunResult, Candidate
from morning_star_review.core.errors import MsreviewRunDirMissing
from morning_star_review.core.jsonio import (
    FileTooLarge,
    read_bytes,
    read_json,
    sha256_bytes,
    write_json_atomic,
    write_model,
    write_text_atomic,
)

__all__ = [
    "CANDIDATES_FILE",
    "JOB_FILE",
    "InvalidJob",
    "JobFiles",
    "add_coverage",
    "candidate_ids_from_prompt",
    "load_candidates",
    "load_job",
    "read_coverage",
    "read_input",
    "write_partial_notes",
    "write_result",
]

JOB_FILE = "job.json"
CANDIDATES_FILE = "candidates.json"  # verify phase: MergeResult JSON or a list of Candidate
MAX_INPUT_BYTES = 4 * 1024 * 1024  # prompt.txt / notes.txt
MAX_JOB_BYTES = 8 * 1024 * 1024  # job.json lists every changed file

_CANDIDATES = TypeAdapter(list[Candidate])


class InvalidJob(Exception):  # noqa: N818 - reads like the condition it reports
    """The job directory cannot be run as given; retrying will not help (exit code 3)."""


@dataclass(frozen=True, slots=True)
class JobFiles:
    """Absolute paths of the files a job uses."""

    job_dir: Path
    input: Path
    meter: Path
    coverage: Path
    result: Path
    partial_notes: Path


def _resolve(job_dir: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else job_dir / path


def load_job(job_dir: str | Path, phase: str) -> tuple[AgentJob, JobFiles]:
    """Read and check ``job.json``; raise ``InvalidJob`` for anything a retry cannot fix."""
    root = Path(job_dir)
    if not root.is_dir():
        raise InvalidJob(f"job directory not found: {root}")
    root = root.resolve()
    try:
        raw = read_json(root / JOB_FILE, max_bytes=MAX_JOB_BYTES)
        job = AgentJob.model_validate(raw)
    except MsreviewRunDirMissing as exc:
        raise InvalidJob(f"{JOB_FILE} is missing") from exc
    except (ValueError, ValidationError, FileTooLarge) as exc:
        # ValidationError is a ValueError; json errors too. Keep the first line only.
        first = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        raise InvalidJob(f"{JOB_FILE} is invalid: {first}") from exc
    if job.phase != phase:
        raise InvalidJob(f"{JOB_FILE} is for phase {job.phase!r}, not {phase!r}")
    files = JobFiles(
        job_dir=root,
        input=_resolve(root, job.input_path),
        meter=_resolve(root, job.meter_path),
        coverage=_resolve(root, job.coverage_path),
        result=_resolve(root, job.result_path),
        partial_notes=_resolve(root, job.partial_notes_path),
    )
    if phase in ("explore", "verify") and not Path(job.repo_dir).is_dir():
        raise InvalidJob("repo_dir is not a directory")
    return job, files


def read_input(job: AgentJob, files: JobFiles) -> str:
    """The phase input, after checking it against ``input_sha256``."""
    try:
        data = read_bytes(files.input, max_bytes=MAX_INPUT_BYTES)
    except MsreviewRunDirMissing as exc:
        raise InvalidJob("the input file named in job.json is missing") from exc
    except FileTooLarge as exc:
        raise InvalidJob("the input file is too large") from exc
    if sha256_bytes(data) != job.input_sha256.lower():
        raise InvalidJob("the input file does not match input_sha256")
    return data.decode("utf-8", "replace")


def write_result(files: JobFiles, result: AgentRunResult) -> None:
    write_model(files.result, result)


def write_partial_notes(files: JobFiles, text: str) -> None:
    write_text_atomic(files.partial_notes, text)


def read_coverage(path: Path) -> list[str]:
    """Paths recorded so far (insertion order); an unreadable file counts as empty."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [p for p in data if isinstance(p, str)]


def add_coverage(path: Path, repo_path: str, known: list[str]) -> None:
    """Append ``repo_path`` to the coverage set (``known`` is the in-memory copy)."""
    if repo_path in known:
        return
    known.append(repo_path)
    write_json_atomic(path, known)


def load_candidates(job_dir: Path) -> list[Candidate] | None:
    """Candidates of a verify job, or ``None`` when ``candidates.json`` is absent."""
    path = job_dir / CANDIDATES_FILE
    if not path.exists():
        return None
    try:
        data: Any = read_json(path, max_bytes=MAX_INPUT_BYTES)
        if isinstance(data, dict):
            data = data.get("candidates", [])
        return _CANDIDATES.validate_python(data)
    except (ValueError, ValidationError, FileTooLarge) as exc:
        raise InvalidJob(f"{CANDIDATES_FILE} is invalid") from exc


# A candidate header line in a rendered verify prompt, e.g. "CANDIDATE c3", at column 0.
# The renderer indents continuation lines of candidate text and any such line in other
# PR-derived text, so untrusted text cannot add ids (looser forms like "id: c3" could
# be injected through a PR description).
_ID_LINE = re.compile(r"^CANDIDATE\s+([A-Za-z0-9_-]{1,40})\s*$")


def candidate_ids_from_prompt(prompt: str) -> list[str]:
    """Fallback when ``candidates.json`` is missing: ids named on header lines of the prompt."""
    ids: list[str] = []
    for line in prompt.splitlines():
        m = _ID_LINE.match(line)
        if m and m.group(1) not in ids:
            ids.append(m.group(1))
    return ids
