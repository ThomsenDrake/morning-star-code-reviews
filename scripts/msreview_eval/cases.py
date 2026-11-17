"""Answer keys: one YAML file per evaluation case under ``eval/cases/``.

A case file says how to build the PR (commits made of patches and generators
applied to the ledger fixture), what the PR looks like on GitHub, where the
planted bugs are, which edits are cosmetic, and what a passing review looks
like. The schema extends DESIGN's ``{case, pr, bugs: [{id, path, lines,
keywords}], cosmetic_paths}`` with the fields below.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

REPO_ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = REPO_ROOT / "eval"
CASES_DIR = EVAL_DIR / "cases"
FIXTURE_DIR = EVAL_DIR / "fixtures" / "ledger"
CHANGES_DIR = EVAL_DIR / "fixtures" / "changes"

Verdict = Literal["clean", "findings", "incomplete"]
Generator = Literal["large_pr"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Location(_Model):
    path: str = Field(min_length=1)
    lines: tuple[int, int]

    @field_validator("lines")
    @classmethod
    def _ordered(cls, value: tuple[int, int]) -> tuple[int, int]:
        start, end = value
        if start < 1 or end < start:
            raise ValueError(f"bad line range {value}")
        return value


class BugKey(Location):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,20}$")
    description: str = ""
    keywords: list[str] = Field(min_length=1)
    # Text that must occur in the head file within ``lines`` (checked by tests).
    expect_text: str | None = None
    # Other places where a correct report of the same bug may be anchored,
    # e.g. the definition of a callee in an unchanged file.
    alt_locations: list[Location] = Field(default_factory=list)
    tolerance: int | None = Field(default=None, ge=0, le=20)

    def locations(self) -> list[Location]:
        return [Location(path=self.path, lines=self.lines), *self.alt_locations]


class CosmeticKey(Location):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,20}$")
    expect_text: str | None = None


class CommitSpec(_Model):
    message: str = Field(min_length=1)
    generators: list[Generator] = Field(default_factory=list)
    patches: list[str] = Field(default_factory=list)  # file names in CHANGES_DIR


class PullRequestSpec(_Model):
    branch: str = Field(pattern=r"^msreview-eval/[A-Za-z0-9._-]{1,80}$")
    title: str = Field(min_length=1, max_length=200)
    body: str = ""
    # Commits pushed when the branch is first created (None = all). E2 starts
    # with 1 so that the second commit can arrive after the first review.
    initial_stage: int | None = Field(default=None, ge=1)


class EvalSettings(_Model):
    default: bool = True  # included when scripts/eval.py gets no --cases
    runs: int | None = Field(default=None, ge=1)  # overrides --runs (E6 runs once)


class Expectations(_Model):
    verdicts: list[Verdict] = Field(default_factory=list)  # empty = any verdict
    # Acceptable verdicts when at least one bug was missed (E6: incomplete).
    verdicts_if_missed: list[Verdict] | None = None
    min_detected_per_run: int | None = Field(default=None, ge=0)
    max_fp_medium: int | None = Field(default=None, ge=0)
    max_cosmetic_hits: int | None = Field(default=0, ge=0)
    # Literal strings that must not appear in the rendered report outside code
    # spans (a quoted URL in a code span is not a link, an escaped @ is no mention).
    forbidden_output: list[str] = Field(default_factory=list)
    # Regular expressions that must not match anywhere in the rendered report.
    forbidden_patterns: list[str] = Field(default_factory=list)
    max_cost_usd: float | None = Field(default=None, gt=0)
    # Every open inline comment's evidence occurs in its anchored head lines (DESIGN E1).
    valid_anchors: bool = False
    scope: Literal["full", "incremental", "carry_over"] | None = None
    on_demand: list[str] = Field(default_factory=list)  # informational


class CaseKey(_Model):
    case: str = Field(pattern=r"^[a-z0-9_-]{1,20}$")
    name: str
    pr: int | None = None  # PR number; usually supplied by make_test_prs.py output
    eval: EvalSettings = Field(default_factory=EvalSettings)
    pull_request: PullRequestSpec
    build: list[CommitSpec] = Field(min_length=1)
    tolerance: int = Field(default=3, ge=0, le=20)
    bugs: list[BugKey] = Field(default_factory=list)
    fixed: list[BugKey] = Field(default_factory=list)  # fixed by a later commit
    cosmetic: list[CosmeticKey] = Field(default_factory=list)
    cosmetic_paths: list[str] = Field(default_factory=list)
    expect: Expectations = Field(default_factory=Expectations)

    @model_validator(mode="after")
    def _check(self) -> CaseKey:
        stage = self.pull_request.initial_stage
        if stage is not None and stage > len(self.build):
            raise ValueError(f"initial_stage {stage} exceeds the {len(self.build)} commits")
        ids = [b.id for b in self.bugs] + [b.id for b in self.fixed]
        ids += [c.id for c in self.cosmetic]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate ids in case {self.case}: {ids}")
        return self

    @property
    def initial_stage(self) -> int:
        return self.pull_request.initial_stage or len(self.build)

    def tolerance_for(self, bug: BugKey) -> int:
        return self.tolerance if bug.tolerance is None else bug.tolerance


def load_case(path: Path) -> CaseKey:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return CaseKey.model_validate(data)


def load_cases(directory: Path = CASES_DIR) -> dict[str, CaseKey]:
    """All cases keyed by their id, in file-name order."""
    cases: dict[str, CaseKey] = {}
    for path in sorted(directory.glob("*.yaml")):
        key = load_case(path)
        if key.case in cases:
            raise ValueError(f"case id {key.case!r} is defined twice")
        cases[key.case] = key
    return cases


def select_cases(cases: dict[str, CaseKey], wanted: list[str] | None) -> list[CaseKey]:
    """Cases named in ``wanted`` (in that order), or the default eval set."""
    if not wanted:
        return [c for c in cases.values() if c.eval.default]
    unknown = [name for name in wanted if name not in cases]
    if unknown:
        raise SystemExit(f"unknown case(s): {', '.join(unknown)}; known: {', '.join(cases)}")
    return [cases[name] for name in wanted]
