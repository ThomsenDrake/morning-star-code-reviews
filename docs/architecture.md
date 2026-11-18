# Architecture

This document describes how a review runs, from the trigger to the posted comment. It
is meant for contributors and for operators who want to know what happens inside a
run.

## Overview

```
pull_request event (or `msreview review <PR>`)
  │
  ├─ token step (Action: read-only token) / mint child (CLI): App key → installation
  │  token (1 repo, 1 hour)
  │
  ├─ driver process (`msreview ci` / `msreview review`), in an empty working directory
  │    1. preflight: whoami with the Mistral key (refused key → exit 2, nothing started)
  │    2. cheap GitHub pre-check: closed, draft, superseded, already reviewed → skip
  │    3. choose the per-PR deployment name; (CI) terminate orphaned executions of this PR
  │    4. start a Workflows worker in-process, registered on that deployment
  │    5. wait until the deployment reports the worker, then start the execution
  │    6. poll until it ends (signals and worker crashes terminate it; at the deadline
  │       it is terminated and the PR still gets the failure notice)
  │    7. always: terminate the execution if it is still running, then stop the worker
  │    8. write the outcome: stdout / step summary / outputs / artifact; exit code
  │       (Action with App credentials: a publish plan instead of posting)
  │
  └─ (Action) token step: write token, minted after the review; then publish step
     (`msreview ci-publish`): posts the publish plan
            │
            ▼
   Workflow `morning-star-code-review` (history visible in Studio)
     fetch_pr → prepare → explore[m1] ‖ explore[m2] ‖ …     (one agent process each)
              → structure[m_i] (parse the notes; formatter only if needed)
              → merge → verify                              (when the pipeline is on)
              → finalize → publish
     any failure → report_failure (best effort) → execution FAILED
```

The worker runs **inside the driver process**, for exactly one execution. No
long-running service exists. The next review starts its own worker.

`--local` (CLI) runs the same workflow code in-process without a Workflows server.
This mode is used for development and for the evaluation.

## Time limits and rate limits

A rate limit makes a review slower. It never fails the run, drops an agent, or uses
up a model call. One overall limit keeps a run bounded.

**The run clock.** The driver plans it at the start: the hard deadline H
(`deadline-minutes` after the start, default 90) and the wind-down W, 15 minutes
before H. For a deadline under an hour, the margins shrink in proportion, to at most
half. The driver passes both times to the activities in its in-memory run context
(the workflow code still reads no clock). Each agent job gets absolute limits from
them:

| Phase | Tools close at | Last model call, last wait ends |
|---|---|---|
| explore | W (or earlier, by its own budget) | W + 5 min, at most H - 10 min |
| structure (formatter) | (no tools) | H - 6 min |
| verify | H - 12 min | H - 8 min |
| verify formatter | (no tools) | H - 4 min |

With verification on, the verifier keeps a reserved window: it may start until the
verify cutoff V = H - 15 min, and explore and structure end early enough for that
(`RunClock.limits(phase, verify=True)`): the structure formatter by V - 1.5 min (an
agent process may run 1 min past its limit, plus the hand-over), the explorers' last
model call 4 min before that, and their tools close 5 min before their last call (or
at W when it is earlier). For D = 90: explorer tools close at 64.5, last call 69.5,
structure 73.5, V = 75. The verifier does not start at or after V: then every
candidate stays unconfirmed, and the verdict says why. A `finalize` that runs at or
after W (with verification: at or after the explorers' wind-down) adds the reason "the
review was finished early to stay within its N-minute time limit", so such a review is never
`clean`. If the execution still runs at H, the driver terminates it and records the
failure itself (the failure notice and `msreview: incomplete`), from the sealed
snapshot that `fetch_pr` left in the run directory.

**A refused model call**, in order:

1. The model SDK tries the call up to 5 times in about 10 seconds; its wait for a
   `Retry-After` header is capped at 60 seconds.
2. The agent's runner then waits and runs the turn again in the same session. For
   rate limits and temporary unavailability (429, 5xx, capacity) the number of retries
   has no limit; the run's limits and the cost cap end them. Lost streams and
   timeouts stop after 6 failures in a row. The waits grow 15, 30, 60, 120, then 180 s
   (plus up to 25 % jitter), at least the API's `Retry-After` (at most 5 minutes), and
   start again at 15 s after a successful reply.
3. Before a rate-limit wait, the runner writes the model's cooldown to a small file in
   the run directory. Every agent of the run that uses the model reads it before its
   next call and waits too. The file is advisory: the last writer wins.
4. The agent's meter pauses its own clocks (explore, grace, rescue and formatter
   time) for these waits and for the SDK's retries of a call that was refused in the
   end, and records it in `meter.json`. A refused call does not count as a model call.
   SDK waits inside a call that finally succeeds are part of that call's time.

The process that launches an agent re-reads `meter.json` on every heartbeat and
extends the agent's time allowance by the waits, up to the job's run limit. An
explorer stopped at its run limit still returns its partial notes. The timeouts of
the long activities and of the workflow sit above the largest allowed deadline
(300 minutes); heartbeats detect a stuck activity. The driver retries Workflows API
errors (429, 5xx, network) with backoff until the deadline, and GitHub API requests
wait for a rate limit up to 15 minutes in total, never past H.

## Two environments

| Environment | Contains | Runs |
|---|---|---|
| **Worker** (`uv tool install`, or the Action's `venv-worker`) | `mistralai`, `mistralai-workflows`, httpx, pydantic, PyJWT, PyYAML | The CLI, the driver, the Workflows worker and all activities |
| **Agent** (`msreview setup`, or the Action's `venv-agent`) | The same, plus `mistralai-agents` | Only the agent subprocesses (`python -m morning_star_review.agent <phase> <job dir>`) |

They are separate because importing the Agents SDK registers many extra Workflows
activities and plugins (MCP tools, connectors, harness execution). A worker would
then serve all of them on its task queue. The worker environment contains only the
SDK's built-ins and our own activities. Tests enforce the boundary: no module outside
`morning_star_review.agent` may import the Agents SDK, and the agent package is never
imported in the worker environment.

Both environments install the same version of this package. The Action builds both
from `uv.lock` with `--frozen`. `msreview setup` builds the agent environment from
hash-pinned requirements exported from the same lock file.

## Large data stays on disk

Each run has a **run directory** on the runner or your machine. It holds the git
checkout, the PR file list with GitHub's patches, the anchor map, the rendered diffs,
the prompts, one job directory per agent (job, notes, result, usage meter, coverage,
log), and the report files. Activities pass each other **paths and SHA-256 digests**.
They do not pass the data itself, so the execution history contains only metadata,
notes (capped), findings and the report. Each activity checks the digests before it
uses a file.

The worker for a run executes all of that run's activities, so the run directory is
always local. If it is missing, the activity fails without retrying.

## Activities

### `fetch_pr`

Reads the PR and its files. It reads `.morning-star-review.yml` **at the PR's base
commit** through the Contents API, and resolves the effective configuration from the
preset, the file, the run overrides and the operator caps (see
[configuration.md](configuration.md)). It also reads the prior review state. The prior state is a compressed
JSON marker in the bot's own summary comment, trusted only when your App's bot wrote
it. It also reads review threads (who resolved what) and the permission of each person
who resolved a thread. It then decides whether to **skip**, in this order:

1. the head moved past `expected-head-sha` (superseded);
2. the PR is closed;
3. the PR is a draft;
4. this head was already reviewed **completely** with the same configuration hash and
   the same major version.

`--force` bypasses the last three.

### `prepare`

- **Checkout:** fetches the head (`refs/pull/<n>/head`, which also works for forks),
  the merge base and, when known, the prior reviewed head into a fresh directory. Git
  runs with hardened settings: no hooks, no filters, no submodules, and no system or
  global config. Nothing in the checkout is executed.
- **Scope:**
  - `full`;
  - `incremental`: only the files changed since the last completely reviewed head;
  - `carry_over`: nothing reviewable changed, so the previous result is reused
    without calling a model.

  An incremental review requires a complete previous review, the same configuration
  and major version, a head that descends from the previous head without merge
  commits, and a moderate number of changed files.
- **Pre-pass:** drops non-reviewable files (lock files, minified bundles, source maps,
  snapshots, binaries, `exclude` globs). A file counts as generated only when its base
  version also had the marker. The pre-pass then ranks the rest by kind, size and risk,
  and puts diffs into the prompt in rank order until the `inline_diff_chars` budget is
  used. The remaining reviewable files are "on demand": the agent reads them with
  `show_file_diff`. A hard ceiling of 20,000 changed lines keeps huge PRs bounded and
  marks the review partial.
- **Related definitions:** for function names called on added lines, it finds their
  definitions at the head with language-agnostic patterns. Definitions in unchanged
  files are preferred. Up to 25 names and 12,000 characters are added to the prompt, so
  the agent can check argument order and types at call sites.
- Builds the **anchor map**: for each file, the head lines that GitHub accepts for
  inline comments, and the hunks that they belong to.
- Writes the explorer prompt. All PR-controlled text (title, description, diffs, related
  definitions, prior findings) goes inside untrusted blocks with a random boundary for
  each run. The repository's `instructions` are marked as guidance from the base branch.

### `explore` (one per explorer, in parallel)

Each explorer runs as a separate process in the agent environment. Its environment is
built from an allowlist: the Mistral key, empty home and temp directories, and
telemetry switched off. It has a new process group, so it can be stopped as a whole.
The agent has five read-only tools, confined to the checkout. A budget hook counts LLM
calls, tool calls, time, context size and estimated cost before each call:

- When a tool or time budget runs out, the tools close. The agent then gets a few more
  calls to write up what it found.
- If it stops without usable notes, a short **rescue** turn with no tools asks it to
  write them.
- The cost cap is hard: no call starts if the run's share of the budget cannot pay for
  it.
- A turn that fails with a temporary model error (HTTP 429, an unavailable provider) is
  run again in the same session after a growing wait, until the run's time limit or
  the cost share stops it. The wait does not count against the explorer's own time
  (see [Time limits and rate limits](#time-limits-and-rate-limits)).

The explorer writes plain-text **notes**, not JSON. Models follow a simple line format
far more reliably than a schema while they are still using tools:

```
FINDING
file: ledger/service.py
line: 125
severity: high
category: correctness
title: prorate arguments are swapped
evidence: fee = prorate(days_total, days_used, amount)
why: …
fix: …
suggestion:
<<<
    fee = prorate(amount, days_used, days_total)
>>>
END FINDING
…
SUMMARY: …
REVIEW COMPLETE
```

Notes also carry the explorer's view of each prior finding (`fixed`, `still_open` or
`unsure`). That view is advisory only (see `finalize`). An explorer is **complete**
when its notes end properly (the completion line or a final summary), every block is
closed, and it did not run out of budget. **Coverage** is recorded by the tools
themselves, not taken from the model's claims: a file counts as reviewed when its diff
was in the prompt or an explorer opened it.

### `structure`

Parses each explorer's notes. The parser accepts the usual deviations: list markers,
bold text, missing `END` lines, line ranges, field aliases, and multi-line code written
on one line with literal `\n` escapes. The **formatter** model runs only when the
parser loses something: unparsed blocks, fewer findings than `FINDING` markers, or
findings written as prose. The formatter has no tools and must fill a strict schema.
It is best effort: if it fails, the parsed findings are kept, and the draft is marked
as lossy, so the verdict cannot be `clean`. The result is one draft for each explorer.

### `merge` (ensembles and verified runs)

A pure function, with no model involved. It combines the explorers' drafts into
candidates. Two findings are the same when they are on the same path, their ranges
overlap or are within three lines in the same hunk, and their evidence, titles or
category and range agree. A candidate keeps the highest severity, the longest
explanation and the best anchor. Its `votes` list the models that reported it. At
most 60 candidates go on, ordered by severity, then votes.

### `verify`

One agent with the same five tools, its own budget (30 tool calls, 8 minutes by
default; a flash model gets 25 tool calls unless a budget is set) and no schema. It receives the candidates inside an untrusted block and
writes one `VERDICT <id> … END VERDICT` block for each: `confirmed`, `refuted` or
`uncertain`, with optional corrections to line, evidence or severity, and a short
reason. It must not add candidates. A candidate that it does not address counts as
`uncertain`. If the verifier fails, every candidate is `uncertain`. The verifier is
skipped when the run has reached its verify cutoff; then too every candidate is
`uncertain`, and the verdict reasons say that the verifier did not run.

### `finalize`

Deterministic: no model and no network. The activity reads the clock once, for the
review time, the duration and whether the run had reached its wind-down.

1. **Anchoring.** It normalises each finding's path and checks the line against
   GitHub's own patch. When the quoted evidence is on that line, the finding is
   `inline`. Otherwise the finding moves (`snapped`) to the nearest commentable line
   within ±5 lines whose text matches the evidence, or to the only matching line in the
   file. If nothing matches, the finding keeps its line when GitHub accepts comments
   there. If not, it becomes file-level, or "outside the diff" for a path that the PR
   does not change. Only `inline` and `snapped` findings become inline comments, and
   ranges are at most 20 lines.
2. **Ids and de-duplication.** A finding's id is a hash of its path and the text of
   the anchored line, not the title. So the same flagged code keeps its id across runs,
   and an earlier comment is recognised. Duplicates are merged, and the
   `max_inline_comments` cap and `inline_min_severity` are applied. The other findings
   are listed in the summary only.
3. **Verification policy.** `confirmed` candidates are published. `uncertain` ones are
   listed under "Possible issues (not confirmed)", or posted inline with an
   "(unconfirmed)" tag when at least two explorers reported them at high or critical
   severity. `refuted` ones are dropped and kept in `findings.json`.
4. **Prior findings.** Code decides each prior finding, not the models:
   - If the flagged code changed (or the file was deleted), the finding is resolved.
   - If a person with write access resolved the thread (and, when
     `author_may_dismiss: false`, that person is not the PR author), the finding is
     dismissed, and a new finding on the same code is not posted again.
   - If anyone else resolved the thread, the finding is kept open as "resolved without a
     fix".
   - Otherwise the finding stays open and is carried over.

   The explorers' opinions about prior findings are advisory only.
5. **Suggestions.** A suggestion block is kept only if all of these hold, otherwise it
   is dropped and the finding stays:
   - the anchor is exact;
   - the range is in one hunk and at most 20 lines;
   - the replacement is not empty, changes something, and is not a no-op for the
     flagged code;
   - it keeps the original's indentation style;
   - it does not repeat the lines just outside the range, and does not change the line
     count by more than 10;
   - the file still parses after the replacement is applied (Python, JSON, YAML, TOML),
     or the bracket balance stays the same for other languages;
   - it contains no hidden marker (`<!--`, `morning-star-review:`) and no line that
     would close the fence.

   Code in a suggestion is kept as written. `@decorators`, `#` comments and URLs are
   allowed, because GitHub does not link anything inside a code block. The fence is
   longer than any backtick run in the code.
6. **Verdict:**
   - `findings` if any finding is open: new, carried, inline or summary-only, or
     resolved without a fix. A *possible issue* (a new finding that the verifier left
     `uncertain` and that is listed only in the summary) does not count here.
   - Otherwise `incomplete` if the review was not complete.
   - Otherwise `clean`.

   "Complete" requires all of these: the primary explorer finished within its budget;
   the other explorers finished, or their notes parsed in full; every draft parsed
   without loss; the verifier finished when it had work; nothing was cut short; the
   run had not reached its wind-down; and every reviewable file was in the prompt or
   opened.
7. **Rendering and sanitising.** The summary, the inline comments and the report are
   built from templates. All model text goes through the sanitiser:
   - hidden markers are removed;
   - HTML is escaped;
   - mentions and issue references are broken with a zero-width joiner;
   - links to hosts other than github.com become code;
   - headings are flattened;
   - each comment is capped in length.

### `publish`

If posting is possible, it writes to GitHub in an order that makes retries safe:

1. Re-read the PR. If the head moved, post nothing and return the report as
   `superseded`.
2. Delete any **pending** review that an interrupted earlier attempt left behind.
3. Read the inline comments that are already posted, and find them by their hidden
   finding markers. Post only the new ones, as **one** `COMMENT` review on the head
   commit. If GitHub rejects a position (HTTP 422), post the comments one by one, and
   list the ones that GitHub still rejects in the summary. The review never approves
   and never requests changes.
4. For prior findings whose code changed, reply "Looks addressed in `<sha>`: the
   flagged code changed." (or "File removed."). Then try to resolve the thread.
   GitHub refuses this unless the token has Contents: write. msreview requests Contents:
   read on purpose, so it then **hides its own comment as resolved** instead. It
   records the thread, so later runs do not reply again or retry.
5. Create or update the **summary comment** last. It carries the state marker
   (findings, comment ids, thread ids, verdict, configuration hash, head). The state
   therefore records only what was really posted. A retry after a crash finds every
   earlier write through its marker.
6. Set the verdict label (`msreview: clean`, `findings` or `incomplete`), create it if
   it is missing, and remove the other two. This step is best effort: a failure becomes
   a note, never a failed run.

**Report mode.** When posting is not possible (no App credentials, App not installed,
missing permission, `post: never`), `publish` writes nothing to GitHub. It returns the
report with a first line that explains why.

**Deferred publish (Action).** With App credentials, the review step has a read-only
token only. `publish` then writes a **publish plan** (`publish_plan.json`: the request,
the snapshot and the final review) instead of posting, and `msreview ci` copies it out
of the run directory before it removes that directory. After the review, the Action
mints the write token, and `msreview ci-publish` runs the same steps 1 to 6 from the
plan. It first checks that the token's bot is the one the review was made for. In the
CLI, `publish` mints a new token through the mint child when the first is older than
45 minutes.

### `report_failure`

If any step fails, this activity runs before the execution ends as FAILED. It does
two things, both best effort and from fixed templates only: it adds a notice to the
existing summary comment that the latest run did not finish, and it sets
`msreview: incomplete`. With deferred publish it writes a failure plan instead, and
the publish step posts the notice. At the hard deadline the driver runs the same code
itself, because the execution was terminated.

## Guards and seals

Deployment names are predictable, so another process could try to send tasks to
your worker. Every activity first checks that the task belongs to this execution and
to the exact request that the driver started (by digest). Every payload between
activities carries an HMAC seal, computed with a key that exists only in the worker's
memory for this run. A payload sealed elsewhere, for another execution or type, or
changed after sealing, is rejected without a retry. `publish` posts only sealed
content.

The workflow code itself does no I/O and reads no clock, environment or randomness.
Everything that touches the outside world is an activity.

## Source layout

```
src/morning_star_review/
  cli.py, driver.py, doctor.py, setup_agent.py, usersettings.py, outputs.py, platform.py
  deferred.py            the publish plan of the Action's publish step
  workflow.py            the workflow definition
  activities/            one module per activity, plus the agent launcher
  agent/                 agent runner, tools, budget hooks, rate-limit policy, prompts
                         (agent environment only)
  core/                  contracts, config, models and presets, notes grammar, sanitiser,
                         seals, guards, redaction, child-process helpers, run clock
  diff/                  patch parsing, rendering, pre-pass, anchors, scope, related definitions
  github/                HTTP client, App auth and mint child, fetch, publish, labels, state marker
  review/                anchoring, merge, reconcile, verdict, suggestions, rendering, finalize
```

The packages `core`, `diff` and `review` do no network I/O. Most of the
logic is tested without a model or a server:

- unit tests;
- GitHub client tests against a fake API;
- scripted-model agent tests;
- whole-workflow tests in local mode;
- an opt-in platform smoke test.
