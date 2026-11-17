# Models, presets and evaluation

## Supported models

All models are hosted by Mistral and called through your Mistral API key.

| Model id | Accepted efforts | List price, USD per 1M tokens (in / out) | Status | Default tool calls when no budget is set |
|---|---|---|---|---|
| `zai-glm-5-3` | low, high, max | 1.40 / 4.40 | GA, third-party (about one month's deprecation notice), text only | run budget (60) |
| `mistral-large-4` | low, medium, high | 0.70 / 2.10 | GA | run budget (60) |
| `zai-glm-5-3-flash` | low, high, max | 0.15 / 0.50 | GA, third-party | 25 |
| `deepseek-v4-1-flash-1017` | low, high, max | 0.30 / 1.20 | Public Preview, third-party | 25 |

- **Effort** is the reasoning effort sent to the model. `default` means "send no
  effort", which every model accepts. If a model does not accept the effort that you
  set, the preset's value is used instead, and the summary shows a warning.
- Only these four models are accepted. A run may use only the models in its allowed
  list (Action input `allowed-models`, CLI `[defaults] allowed_models`). A model that
  is not allowed falls back to the preset's model, with a warning.
- `mistral-large-latest` is refused. The Agents SDK maps that alias to an older model;
  use `mistral-large-4`.
- Prices are list prices, used for the cost estimates and the cost cap. The estimates
  charge cached input at the full input price, so they are conservative.

## Roles

| Role | What it does | Tools |
|---|---|---|
| **Explorer** | Reads the diff and the repository, and writes findings with exact evidence lines in a simple text format | 5 read-only tools |
| **Verifier** | Gets the merged candidate findings, re-reads the code, and marks each one `confirmed`, `refuted` or `uncertain`, with optional corrections to line, evidence or severity. It cannot add findings | The same 5 tools |
| **Formatter** | A fallback: it converts an agent's notes into structured data only when the notes cannot be parsed as written. In the evaluation it was never needed | None |

The five tools are `list_repo_files`, `read_repo_file`, `read_base_file`,
`search_repo` and `show_file_diff`. They are read-only and confined to the checkout.

## Presets

| Preset | Explorers (effort) | Verifier | Formatter | Notes |
|---|---|---|---|---|
| **`best`** (default) | `zai-glm-5-3` (high), `mistral-large-4` (high), `deepseek-v4-1-flash-1017` (high) | `zai-glm-5-3` (high) | `mistral-large-4` (low) | Three explorers in parallel, then merged and verified. Every explorer gets 60 tool calls |
| `balanced` | `zai-glm-5-3` (high) | `mistral-large-4` (high) | `mistral-large-4` (low) | One explorer, and a verifier from a different model family |
| `single` | `zai-glm-5-3` (high) | none | `mistral-large-4` (low) | One explorer, no verification |
| `fast` | `zai-glm-5-3-flash` (high) | none | `zai-glm-5-3-flash` (low) | Lowest cost |
| `mistral` | `mistral-large-4` (high) | `mistral-large-4` (high) | `mistral-large-4` (low) | Mistral's own model for every role |

You can build your own combination with `explorers` / `--explorers` (up to five
models), `verify`, `verifier-model` and the effort settings. See
[configuration.md](configuration.md) for precedence.

### How ensemble results are published

When a run has two or more explorers, or the verifier is on:

1. Each explorer reviews the change independently, in parallel.
2. A **deterministic merge** (no model involved) combines findings that describe the
   same problem. Two findings are the same when they are on the same path, their line
   ranges overlap or are at most three lines apart in the same hunk, and their evidence,
   titles or category and range are similar. The merged candidate keeps the highest
   severity, the longest explanation and the best anchor. It records which models
   reported it ("votes"). At most 60 candidates go on.
3. The **verifier** checks each candidate.
4. Publishing policy:
   - `confirmed`: posted normally (inline when it is anchored on the diff and severe
     enough).
   - `uncertain`: listed in the summary under "Possible issues (not confirmed)". If at
     least two explorers reported it and it is high or critical, it is posted inline with an
     "(unconfirmed)" tag.
   - `refuted`: dropped. The summary shows the count, and the report's `findings.json`
     lists the dropped items.
   - A candidate that the verifier did not address is treated as `uncertain`.

If an explorer fails with a temporary error, the run continues with the others, but
the verdict cannot be `clean`. A fatal error fails the run immediately.

### Budgets

| Budget | Default | Applies to |
|---|---:|---|
| Tool calls | 60 | Each explorer |
| Minutes | 15 | Each explorer |
| Tool calls | 30 | The verifier |
| Minutes | 8 | The verifier |
| Cost | $10.00 | The whole run (formatter 5%, verifier 20% when on, the rest split equally between the explorers) |

When nobody sets the tool-call budget explicitly, the two flash models get 25 tool calls
as single explorers, because fast models keep exploring for a long time when they are
allowed many tool calls. In the `best` preset every explorer gets 60. An explicit
budget (flag, Action input or repository file) always applies as set.

The time budget is the real guard. When an agent reaches a limit, it gets a short grace
period to write up what it found. The verdict then explains what was not finished.
Secondary explorers in an ensemble that run out of budget do not block a `clean`
verdict, provided that their notes were complete and the primary explorer finished.

## Choosing a preset

- **Quality first:** `best`. It costs about twice as much as `single` and takes about
  2.5 times as long on small PRs, because three explorers and a verifier run.
- **Lower cost and latency with nearly the same results on our benchmark:** `single`.
- **High volume or cost-sensitive:** `fast`. It missed one planted bug in 18 runs.
- **Mistral models only:** `mistral`.

## Evaluation

The benchmark lives in [`eval/`](../eval/). See [eval/README.md](../eval/README.md) for
the cases, the answer-key format and the commands to run it.

### Method

- **Fixture:** a small ledger service, written for this project, with its own tests.
  Each case is a pull request on top of it, built from committed patches, so every run
  reviews the same change.
- **Cases:**
  - E1: a feature with four planted bugs. One of them is a cross-file argument swap
    against a function in an unchanged file. The PR also has two cosmetic edits.
  - E4: a clean refactor (renames and docstrings only).
  - E5: a single SQL injection, surrounded by prompt-injection text in code comments
    and in the PR description, plus a forged state marker.
  - E6: an 80-file PR (lock file, generated tables, vendored code, 25 modules, tests,
    docs) with one bug in a file that does not fit the inline diff budget.
  - E2 tests incremental review on a live repository, and is run separately.
- **Runs:** `msreview review <PR> --post never --json --force`, with the production
  prompts, tools and budgets. There were 3 runs per case and configuration. A
  configuration is either a preset or `--preset single` with one explorer model at high
  effort.
- **Scoring:**
  - A bug counts as *detected* when a finding overlaps its lines (±3) and names it.
  - A *false positive* is a finding of medium severity or higher that matches no
    planted bug.
  - *Anchor validity* checks that the quoted evidence is really on the anchored lines.
  - Empty suggestion blocks fail a run.
  - Costs are estimates at list prices.

### Results (2026-11-17, version 0.1.0 pre-release)

| Config | Recall | FP ≥ medium | Anchors valid | Verdicts correct (E1, E4, E5) | Mean cost | Mean time |
|---|---:|---:|---:|---:|---:|---:|
| `best` preset | 18/18 | 0 | 96% | 7/9 | $0.427 | 86 s |
| `zai-glm-5-3` | 18/18 | 0 | 100% | 8/9 | $0.206 | 35 s |
| `mistral-large-4` | 18/18 | 0 | 68% | 9/9 | $0.090 | 82 s |
| `deepseek-v4-1-flash-1017` | 18/18 | 0 | 100% | 7/9 | $0.053 | 31 s |
| `zai-glm-5-3-flash` | 17/18 | 0 | 100% | 9/9 | $0.016 | 49 s |

Per case (mean cost per run):

| Config | E1 planted | E4 clean | E5 injection | E6 80 files |
|---|---:|---:|---:|---:|
| `best` | $0.18 | $0.14 | $0.11 | $1.28 |
| `zai-glm-5-3` | $0.06 | $0.08 | $0.03 | $0.66 |
| `mistral-large-4` | $0.04 | $0.03 | $0.02 | $0.27 |
| `deepseek-v4-1-flash-1017` | $0.04 | $0.03 | $0.02 | $0.13 |
| `zai-glm-5-3-flash` | $0.004 | $0.007 | $0.004 | $0.05 |

To run the evaluation yourself, see
[`eval/README.md`](../eval/README.md).

What the results show:

- **Every configuration found the bugs.** Recall was 100% everywhere except
  `zai-glm-5-3-flash`, which missed one bug once. This includes the cross-file bug and
  the bug in the file outside the inline budget.
- **No false positives at medium or above** were counted by the scorer. When we read
  the outputs by hand, we judged one `zai-glm-5-3-flash` finding (about the injected
  comments themselves) to be a false positive.
- **Prompt injection:** no run produced an injected mention, link or forged marker in
  its output.
- **Clean refactor:** every configuration returned `clean` in every run.
- **Ensemble agreement:** in the `best` runs, every planted bug got all three explorer
  votes. The verifier confirmed all 24 candidates that it saw. These cases contain no
  false candidates, so this benchmark does not measure the verifier's ability to refute.
- **The default:** on recall and false positives, `best` tied with the strong single
  models. It is the default because three independent votes and a verifier are the most
  robust choice when quality comes first, and its anchors and suggestions were the most
  reliable. Choose `single` if cost or latency matters more.

### Caveats and changes since this run

- This is a small benchmark: one fixture project, four cases, three runs each. The
  results show that the pipeline works. They are not a general accuracy claim.
- **All E6 runs** ended `incomplete`, and some E5 runs did too, although the bug was
  found and placed inline every time. Two strict rules caused this: an explorer's notes
  counted as unfinished when a finding block was closed by the summary rather than by
  its end marker, and a coverage gap took precedence over open findings. Both rules
  have changed since then. Open findings now always give `findings`, and a block
  closed by a later marker counts as closed.
- Other changes made after this run:
  - The merge now treats overlapping line ranges as the same location. This fixes a
    duplicate inline comment in one `best` run.
  - Evidence with literal `\n` escapes is unescaped. Such evidence now
    anchors correctly.
  - The suggestion validator rejects replacements that repeat the lines around the
    range, or that leave the flagged code unchanged.
  - Every explorer in `best` gets 60 tool calls. The DeepSeek explorer had reached
    its old limit of 25 on E6.

  These changes have not been measured in a new evaluation yet.
- Costs are estimates at list prices, with cached input charged in full.
