# Configuration

Settings come from four places:

| Source | Who controls it | Trusted for models and budgets? |
|---|---|---|
| **Action inputs** (`with:` in your workflow) | The repository's maintainers | Yes |
| **CLI flags** and the CLI user config (`~/.config/morning-star-review/config.toml`) | You, on your machine | Yes |
| **Repository file** `.morning-star-review.yml`, read at the PR's **base** commit | Whoever can merge to the base branch | In the Action: yes. In the CLI: only with `--trust-repo-config` |
| **Built-in defaults** (the `best` preset and the default budgets) | This project | n/a |

The repository file is always read from the base commit of the pull request, never
from its head. A PR that edits the file takes effect only after it is merged.

## Precedence

### Models (explorers, ensemble, verifier, formatter, efforts)

From lowest to highest precedence:

1. The built-in default preset (`best`).
2. The CLI user config's `[defaults] preset`. This is a default only: a trusted
   repository file still wins over it.
3. The trusted repository file: its `preset`, then `models`, `ensemble`, `verify`
   and `reasoning_effort`.
4. An **explicit run preset** (`--preset`, Action input `preset`). It replaces all
   of layer 3, so the run gets that preset's pipeline unchanged. The summary
   warns about the repository settings that were not used.
5. **Explicit run model overrides**: `--explorers`, `--explorer-model`, `--verify` /
   `--no-verify`, `--verifier-model`, `--formatter-model`, `--effort`,
   `--verifier-effort` (and the matching Action inputs).

For explorers, the last layer that names any wins, in this order: preset →
`models.explorer` → `ensemble.explorers` → `explorer-model` → `explorers`.

Every model must be in the run's **allowed models** (Action input `allowed-models`,
CLI `[defaults] allowed_models`). By default, these are the four supported models in
[models.md](models.md). An unknown or disallowed model, or an effort that a model does
not accept, never fails the run. The setting is skipped, the preset value is used, and
the summary shows a warning.

### Budgets

Budgets do not follow the model order:

- The **repository file can only lower** a budget. A value above the default is
  ignored, and the summary shows a warning.
- **Run overrides** (flags, Action inputs, and the CLI `[defaults] max_cost_usd`) set
  the budget directly. A value above the default cap raises the cap, up to the hard
  limits below.
- If no one sets a tool-call budget, the preset or model default applies (see
  [models.md](models.md#budgets)).

| Budget | Default | Hard limit for overrides |
|---|---:|---:|
| `max_cost_usd`: whole run, all agents together | 10.00 | 100 |
| `max_tool_calls`: per explorer | 60 | 200 |
| `explorer_minutes`: per explorer | 15 | 25 |
| `verifier_max_tool_calls` | 30 | 200 |
| `verifier_minutes` | 8 | 25 |
| `inline_diff_chars`: diff text put directly in the prompt (repository file only) | 80,000 | n/a |

### On/off switches

- `labels` and `suggestions` are on only when **both** the operator (Action input or
  CLI flag) and the repository file leave them on. Either side can turn them off.
- Drafts are reviewed when **either** `review.drafts: true` in the file or the
  Action input `review-drafts: "true"` is set. The CLI's `--force` also reviews drafts,
  closed PRs and commits that were already reviewed.

### Untrusted repository files (CLI)

When you run `msreview review` without `--trust-repo-config`, the repository file's
`preset`, `models`, `reasoning_effort`, `ensemble`, `verify` and `budgets` are
**ignored**, and the summary lists them. A repository that you do not control cannot
choose your models or spend your money. The settings that could steer or empty the
review are ignored too: `exclude`, `review.instructions`, `review.path_instructions`
and `review.inline_min_severity` (a foreign file could otherwise exclude every file or
tell the explorer to report nothing). Settings that can only reduce output (labels,
suggestions, drafts and so on) still apply. In the Action, the file is always trusted,
because the repository's own maintainers control the base branch.

## Repository file: `.morning-star-review.yml`

Every key is optional. An empty file or a missing file means defaults.

```yaml
version: 1                       # the only supported version

preset: best                     # best | balanced | single | fast | mistral

models:                          # replace one role's model
  explorer: zai-glm-5-3          # a single explorer (no ensemble)
  verifier: mistral-large-4
  formatter: mistral-large-4

ensemble:
  explorers:                     # 1-5 distinct models; wins over models.explorer
    - zai-glm-5-3
    - mistral-large-4

verify:
  enabled: true                  # true | false; omit to follow the preset

reasoning_effort:                # default | low | medium | high | max (checked per model)
  explorer: high                 # applies to every explorer
  verifier: high
  formatter: low

budgets:                         # can only LOWER the defaults
  max_cost_usd: 2.00
  max_tool_calls: 40
  explorer_minutes: 10
  verifier_max_tool_calls: 20
  verifier_minutes: 5
  inline_diff_chars: 60000

review:
  drafts: false                  # review draft PRs
  incremental: true              # review only what changed since the last reviewed commit
  inline_min_severity: medium    # critical | high | medium | low: lowest severity posted inline
  max_inline_comments: 15        # 0-25 new inline comments per review
  author_may_dismiss: true       # may the PR author dismiss findings by resolving threads?
  instructions: |                # up to 8 KB of guidance for the agents
    Money is stored as integer cents; flag float arithmetic on amounts.
  path_instructions:             # up to 30 entries, each text up to 1 KB
    - path: "migrations/**"
      instructions: "Every migration must be reversible."

exclude:                         # up to 100 extra globs to skip
  - "docs/**"
  - "*.snap"

summary:
  show_usage: true               # the "Run details" block (models, tokens, cost)

labels: true                     # keep one msreview: clean | findings | incomplete label
suggestions: true                # validated suggestion blocks in inline comments

context:
  related_definitions: true      # show the agents the definitions of called functions
```

### Keys

| Key | Type and limits | Default | Notes |
|---|---|---|---|
| `version` | `1` | `1` | |
| `preset` | preset name | `best` | Ignored when the run sets an explicit preset |
| `models.explorer` / `.verifier` / `.formatter` | model id | from the preset | |
| `ensemble.explorers` | list of 1-5 model ids | from the preset | Two or more entries make an ensemble |
| `verify.enabled` | bool | from the preset | |
| `reasoning_effort.explorer` / `.verifier` / `.formatter` | effort | from the preset | An effort that the model does not accept falls back to the preset value |
| `budgets.*` | numbers > 0 | see [Budgets](#budgets) | Can only lower |
| `review.drafts` | bool | `false` | |
| `review.incremental` | bool | `true` | When `false`, every push is reviewed in full |
| `review.inline_min_severity` | `critical` \| `high` \| `medium` \| `low` | `medium` | Findings below this severity are listed only in the summary |
| `review.max_inline_comments` | 0-25 | 15 | Extra findings are listed in the summary |
| `review.author_may_dismiss` | bool | `true` | If `false`, only other people with write access can dismiss |
| `review.instructions` | text, max 8,192 chars | empty | Given to the agents as repository guidance (never as instructions that override the review rules) |
| `review.path_instructions` | max 30 × `{path: glob, instructions: max 1,024 chars}` | empty | |
| `exclude` | max 100 globs | empty | Gitignore-like: `*` stays within one path segment, `**` crosses segments, a pattern without `/` matches at any depth, and a trailing `/` matches a directory |
| `summary.show_usage` | bool | `true` | Turn off to hide models, tokens and cost in public repositories |
| `labels` | bool | `true` | |
| `suggestions` | bool | `true` | |
| `context.related_definitions` | bool | `true` | Adds the definitions of functions called on added lines, so the agents can check call sites |

The following are excluded without any configuration: lock files, minified bundles,
source maps, snapshots, binary files, and files that a generator marked as
generated in **both** the base and the head. A PR cannot hide a hand-written file by
adding an `@generated` marker to it.

### Invalid files

The file must be YAML (64 KiB at most, no aliases), with only the keys above. When it
is invalid (an unknown key, a wrong type or a value out of range), **the whole file is
ignored**. The review still runs with defaults, and the summary explains the
problem. One bad key cannot partly apply.

## GitHub Action inputs

All inputs are strings. Boolean inputs accept `true`/`false` (also `1`/`0`, `yes`/`no`,
`on`/`off`). An empty value means "not set".

| Input | Default | Meaning |
|---|---|---|
| `mistral-api-key` | (required) | Mistral API key of a workspace with Workflows access |
| `client-id` | empty | GitHub App client ID. Without the App credentials the review is a report |
| `app-private-key` | empty | GitHub App private key (PEM). Only the token step uses it |
| `post` | `auto` | `auto` posts when possible. `never` always produces a report |
| `preset` | empty | `best`, `balanced`, `single`, `fast` or `mistral`. Empty means the repository file's preset, else `best` |
| `explorers` | empty | Comma-separated explorer models (an ensemble). Overrides the preset |
| `explorer-model` | empty | One explorer model (no ensemble) |
| `formatter-model` | empty | Formatter model, used only when notes cannot be parsed |
| `explorer-effort` | empty | `default`, `low`, `medium`, `high` or `max`, for every explorer |
| `verify` | empty | `true` or `false`: run the verifier. Empty means the preset decides |
| `verifier-model` | empty | Verifier model |
| `verifier-effort` | empty | Verifier effort |
| `max-cost-usd` | empty (10.00) | Cost limit for the whole run, in USD |
| `max-tool-calls` | empty (60) | Tool calls per explorer |
| `explorer-minutes` | empty (15) | Minutes per explorer |
| `verifier-max-tool-calls` | empty (30) | Tool calls for the verifier |
| `verifier-minutes` | empty (8) | Minutes for the verifier |
| `allowed-models` | the four supported models | Comma-separated models that the run may use |
| `review-drafts` | `false` | Review draft pull requests |
| `labels` | `true` | Keep one verdict label on the PR. Needs Issues: write on the App; set `"false"` if the App does not have it |
| `suggestions` | `true` | Add validated suggestion blocks to inline comments |
| `fail-on` | `never` | `never`, `findings`, `high` or `critical`: fail the step (exit code 3) when open findings reach this level |
| `deadline-minutes` | `40` | Overall time limit for the review. Keep it below the job's `timeout-minutes` (45 in the examples) |
| `telemetry` | `false` | Turn on the Workflows and Agents SDK telemetry (see [operations.md](operations.md#telemetry)) |
| `pr-url` | empty | The pull request to review. Empty means the PR that triggered the run |
| `expected-head-sha` | empty | Review only this head commit, and skip if the PR has moved on. Empty means the event's head SHA, when the run reviews the triggering PR |
| `remove-label` | empty | A label to remove after the run. The fork template passes `msreview: safe-to-review` |

### Outputs

| Output | Values |
|---|---|
| `status` | `posted`, `report`, `skipped`, `superseded` or `failed` |
| `verdict` | `clean`, `findings` or `incomplete` (empty when skipped before a review) |
| `findings` | Number of open findings |
| `execution-id` | The Mistral Workflows execution id |
| `report-path` | Path of `report.md` on the runner |

The review's Markdown report is also written to the job's step summary. The three
report files (`report.md`, `findings.json`, `run.json`) are uploaded as the artifact
`msreview-<run id>-<attempt>`, which is kept for 14 days.

## CLI flags for `msreview review`

Each flag matches an Action input. The complete command reference is in [cli.md](cli.md).

| Flag | Action input | Default |
|---|---|---|
| `--post auto\|never` | `post` | `auto` |
| `--preset NAME` | `preset` | the repository file's preset (if trusted), else user config `[defaults] preset`, else `best` |
| `--explorers A,B,C` | `explorers` | |
| `--explorer-model ID` | `explorer-model` | |
| `--formatter-model ID` | `formatter-model` | |
| `--effort LEVEL` | `explorer-effort` | |
| `--verify` / `--no-verify` | `verify` | |
| `--verifier-model ID` | `verifier-model` | |
| `--verifier-effort LEVEL` | `verifier-effort` | |
| `--max-cost-usd X` | `max-cost-usd` | user config `[defaults] max_cost_usd`, else 10.00 |
| `--max-tool-calls N` | `max-tool-calls` | 60 |
| `--explorer-minutes N` | `explorer-minutes` | 15 |
| `--verifier-max-tool-calls N` | `verifier-max-tool-calls` | 30 |
| `--verifier-minutes N` | `verifier-minutes` | 8 |
| `--deadline-minutes N` | `deadline-minutes` | 45 (CLI), 40 (Action) |
| `--expected-head-sha SHA` | `expected-head-sha` | |
| `--fail-on LEVEL` | `fail-on` | user config `[defaults] fail_on`, else `never` |
| `--no-labels` | `labels: "false"` | labels on |
| `--no-suggestions` | `suggestions: "false"` | suggestions on |
| `--telemetry` | `telemetry` | off |
| `--trust-repo-config` | (always trusted in the Action) | off |
| `--force` | (none) | off: skip drafts, closed PRs and commits already reviewed |
| `--read-token env\|gh\|none` | (the Action passes `github.token`) | user config `[defaults] read_token`, else `env` |
| `--local` | (none) | off |
| `--out DIR`, `--json`, `--verbose` | (none) | `./msreview-reports`, Markdown output, quiet |

The allowed models for the CLI come from the user config's `[defaults] allowed_models`.

## Environment variables

### Read by the CLI (and by `msreview ci` where noted)

| Variable | Meaning |
|---|---|
| `MISTRAL_API_KEY` | The Mistral API key. The Action sets it from the `mistral-api-key` input |
| `MSREVIEW_MISTRAL_API_KEY_COMMAND` | A command that prints the Mistral key. It wins over `MISTRAL_API_KEY` |
| `MSREVIEW_APP_CLIENT_ID` | GitHub App client ID (CLI posting) |
| `MSREVIEW_APP_PRIVATE_KEY_COMMAND` | A command that prints the App's PEM. It wins over the path |
| `MSREVIEW_APP_PRIVATE_KEY_PATH` | Path of the App's PEM file (must not be readable by group or others) |
| `MSREVIEW_AGENT_PYTHON` | Python of the agent environment. Default: the one that `msreview setup` installed |
| `MSREVIEW_CONFIG` | Path of the user config file. Default: `~/.config/morning-star-review/config.toml` |
| `XDG_CACHE_HOME` | Base of the cache directory. Default: `~/.cache`, so the agent environment is in `~/.cache/morning-star-review/` |
| `GITHUB_TOKEN`, `GH_TOKEN` | Optional read token for `--read-token env` (used to fetch when there is no App token) |
| `MSREVIEW_ALLOW_CUSTOM_SERVER` | Set to `1` to allow a `SERVER_URL` other than `https://api.mistral.ai` |

Environment variables win over the user config file. The key command runs once, and
its output is kept in memory only. It is never logged or written to disk.

### Set by the Action for `msreview ci`

`action.yml` turns every input into an `MSREVIEW_*` variable for `msreview ci`. For
example, `MSREVIEW_PRESET`, `MSREVIEW_EXPLORERS`, `MSREVIEW_MAX_COST_USD`,
`MSREVIEW_FAIL_ON`, `MSREVIEW_LABELS` and `MSREVIEW_REMOVE_LABEL`. It also sets the
App token (`MSREVIEW_GITHUB_TOKEN`), the workflow token (`MSREVIEW_READ_TOKEN`), the
PR URL and head SHA, and the runner paths. `msreview ci` removes both tokens from its
environment at start-up, so child processes never inherit them. You do not set these
yourself. They are listed here so that you can read `action.yml` more easily.
