# Operations

This page covers running the reviewer over time: what it leaves in your Mistral
workspace, what it costs, how rate limits and the time limit work, how to control
telemetry, and how to fix common problems.

## Deployments

Each review starts its own Workflows worker, for that run only. The worker registers
on a **deployment** named after the pull request:

```
msreview-<mode>-<owner>-<repo>-<hash>-pr<number>        e.g. msreview-ci-acme-shop-3f9a1c-pr42
```

`<mode>` is `ci` (Action) or `cli`. `<hash>` is a short hash of `owner/repo`, which
keeps repositories with similar names apart. The worker stops when the review ends, and
the deployment becomes inactive about a minute later.

**Deployment records accumulate, and you cannot delete them.** Workflows deployments
that a self-started worker creates cannot be deleted through the API. Because the name
is stable for each PR, every push to a PR reuses the same record. Expect **about one
record per reviewed PR** (and one per PR per mode if you also use the CLI).
Occasionally a run finds another live worker on its PR's deployment, for example when
an old run has not stopped yet. It then waits up to 30 seconds and, if it must, uses a
variant name with an `-x<hex>` suffix.

To see them:

```bash
msreview doctor --deployments     # lists up to 100 msreview-* deployments, active or not
```

Inactive records cost nothing and do not affect reviews. If they clutter your
workspace, use a **dedicated workspace** for code review. That is our recommendation
anyway (see [security.md](security.md)).

Executions are named `msreview-<mode>-<owner>-<repo>-pr<number>-<sha7>-<random>`. You
can find them in Studio under the workflow `morning-star-code-review`, and search them
by `pr_url` or `repo`. In CI, a new run first terminates any execution that an earlier
CI run left running for the same PR.

## Costs

Cost depends on the preset, the size of the change, and how much the agents read.

| Preset | Typical cost per review | Typical time |
|---|---|---|
| `best` | $0.10–$0.20 for small PRs, about $1.30 for an 80-file PR | 40 s–2.5 min |
| `single` (`zai-glm-5-3`) | $0.03–$0.08 for small PRs, about $0.65 for an 80-file PR | about 1 min or less |
| `fast` | under $0.01 for small PRs, about $0.05 for an 80-file PR | 30 s–1.5 min |

These numbers come from the evaluation in [models.md](models.md). They are estimates
at list prices, with cached input tokens charged at the full input price, so your bill
is usually lower.

**Controls:**

- **`max-cost-usd`** (default $10.00) is a hard cap for each run. It is checked before
  every model call, using the cost so far plus the projected cost of the next call. The
  cap is split between the roles: 5% for the formatter, 20% for the verifier (when
  the verifier is on), and the rest shared equally by the explorers. An agent that reaches
  its share stops early, and the summary says so.
- Budgets for tool calls and time limit each agent (see [configuration.md](configuration.md#budgets)).
- A head commit that was already reviewed with the same configuration is skipped
  before any worker starts. Later pushes are reviewed **incrementally**: only the files
  changed since the last reviewed commit. A push without reviewable changes carries the
  previous result over without calling a model.
- `concurrency: cancel-in-progress` in the example workflows cancels a running review
  when a newer push arrives.
- Set a spending limit on the Mistral workspace as a backstop.

**Where to see the cost:** the "Run details" block in the summary comment (hide it
with `summary.show_usage: false`), the `msreview usage:` line in the log, and `run.json`
in the report.

## Rate limits and the time limit

Mistral limits the requests and tokens per minute for each model in a workspace (see
[models.md](models.md#rate-limits)). With `best`, three explorers run at the same
time, so a workspace with low limits gets HTTP 429 answers. **A rate limit makes a
review slower. It does not make the review fail**, drop an explorer or skip the
verifier.

**How a refused model call is handled:**

1. The model SDK tries the call up to 5 times in about 10 seconds. Short bursts end
   here. The SDK waits at most 60 seconds for a `Retry-After` header.
2. If the call still fails with a rate limit or a temporary unavailability (HTTP 5xx,
   "capacity"), the agent waits and runs the turn again in the same session. The waits
   are 15 s, 30 s, 60 s, 120 s, then 180 s each time, plus up to 25 % jitter. When the
   API sends `Retry-After`, the agent waits at least that long (at most 5 minutes). A
   successful reply starts the curve again at 15 s. The number of these retries has no
   limit.
3. Agents of the same run that use the same model share the wait. When one of them is
   told to slow down, the others also wait before their next call.
4. The waiting time does not count against the agent's own budget (`explorer-minutes`,
   `verifier-minutes`, the formatter's time), and a refused call does not count as a
   model call. Only the run's time limit and the cost cap still apply.

A lost stream or a request timeout is retried the same way, but at most 6 times in a
row, because such an error can be permanent.

**The time limit.** `deadline-minutes` (default 90, allowed 20 to 300; CLI
`--deadline-minutes`) is the hard limit for the whole run, from the start of the
review to the posted result. The run winds down before it. With verification on
(presets `best`, `balanced` and `mistral`, or `verify` set), the verifier keeps a
reserved window: it may start
until 15 minutes before the deadline, and the explorers stop early enough for that,
so a rate-limited explorer cannot crowd it out.

| Minute (default deadline 90) | Without verification | With verification |
|---|---|---|
| 64.5 | | Explorers stop opening files and write their notes |
| 69.5 | | Explorers make their last model call |
| 73.5 | | Explorer notes are formatted for the last time |
| 75: wind-down | Explorers stop opening files and write their notes | Verify cutoff: a verifier that has not started is skipped |
| 78 | | The verifier stops opening files |
| 80 | Explorers make their last model call | |
| 82 | | The verifier makes its last model call |
| 84 | Explorer notes are formatted for the last time | |
| 86 | | Verifier notes are formatted for the last time |
| 90: deadline | Hard limit | Hard limit |

The exact rule, with H the deadline, W the wind-down and k = D / 60 minutes, kept
between 0.5 and 1 (D is `deadline-minutes`):

- W = H - 15k minutes.
- Without verification: explorers stop opening files at W and make their last model
  call by min(W + 5k, H - 10k) minutes; explorer notes are formatted by H - 6k.
- With verification: the verify cutoff is V = H - 15k. Explorer notes are formatted by
  V - 1.5 minutes (an agent may run 1 minute past its limit, plus 30 s for the hand-over
  to the verifier). Explorers make their last model call 4k minutes before that, and
  stop opening files 5k minutes before their last call (earlier when W is earlier).
- The verifier stops opening files at H - 12k and makes its last model call by
  H - 8k; its notes are formatted by H - 4k. Publishing has the rest.

For a deadline under 60 minutes the margins shrink in proportion, to at most half. For
example, with `deadline-minutes: "20"` and verification on, explorers stop opening
files at minute 6.5, make their last model call by 9, and the verifier may start until
12.5. Without verification they stop opening files at 12.5 and make their last call by
15.

A review that reached the wind-down (with verification: the explorers' wind-down) is
still posted, with the verdict `incomplete` (or `findings`, when it has open findings)
and the reason "the review was finished early to stay within its 90-minute time limit". When
the models waited one minute or more in total, the reason adds "models were
rate-limited for N min in total". An explorer that the limit stopped keeps the notes it
wrote, and the reasons add "the <model> explorer did not finish its review: the run
reached its time limit". With verification, its findings are verified with the
others. Candidates that a skipped verifier did not check stay under "Possible issues
(not confirmed)", and the reasons add "the verifier did not run: the run reached its
time limit".

An agent that waits on a rate limit when its last model call comes near makes one
last, shorter wait: it ends 35 seconds before the limit, so the last try still has
time. If that try is refused too, the agent stops there.

If the run does not finish by the deadline anyway (for example, because GitHub does
not answer), msreview stops it. The summary comment gets the notice that the latest
run did not finish, and the label becomes `msreview: incomplete`.

**Set `timeout-minutes` = `deadline-minutes` + 20.** GitHub cancels a job at its
`timeout-minutes`, and a cancelled job posts nothing. The 20 minutes leave room to
install the environments before the review and to post after it. The example
workflows use `timeout-minutes: 110` with the default deadline of 90. For a shorter
job, lower both, for example `deadline-minutes: "30"` with `timeout-minutes: 50`.

**Slow runs cost runner minutes.** Waiting costs no model tokens, but a GitHub-hosted
runner bills every minute that the job runs: a `best` review that waits on rate
limits for a long time can use up to about 110 runner minutes. If your workspace is
rate-limited often, ask Mistral for higher limits, use `single` or `balanced`, or
lower `deadline-minutes`.

**What you see:**

- In the log, once for each wait:
  `msreview: <model> rate-limited (explore); waiting 60s (total waited 1m 45s)`.
- At the start: `msreview: deadline 90 min; wind-down at 75 min`. At the wind-down:
  `msreview: wind-down: 75 of 90 minutes used; finishing the review with what it has`.
- The `msreview agent:` lines end with `rl_wait=<seconds>`, and the `msreview usage:`
  line adds `rl_wait=<seconds>` for each model that waited.
- When a model waited, the summary's "Run details" get a `Rate-limit wait` column (for
  example `7m 10s (5x)`: the total wait and the number of waits) and the total wait.

GitHub API rate limits are handled the same way: a request waits for the limit to
reset, up to 15 minutes in total, but never past the deadline.

## Telemetry

Telemetry is **off by default**:

- the worker runs with `OTEL_ENABLED=false`;
- the agents run with `AGENTS_SDK_TELEMETRY_ENABLED=0`, `AGENTS_SDK_STUDIO_TRACING_ENABLED=0`
  and `OTEL_ENABLED=false`.

`--telemetry` (CLI) or `telemetry: "true"` (Action) leaves these switches unset, so the
SDKs use their own defaults. This does not affect the Workflows execution history in
Studio. That history is part of how Workflows runs, and you always have it.

## Logs

The job log (or stderr in the CLI) contains short status lines that do not change
between versions:

```
msreview: deadline <D> min; wind-down at <W> min
msreview: execution <id> on deployment <name> (workspace namespace <first 8 chars>)
msreview: scope=<full|incremental|carry_over> ...
msreview: <model> rate-limited (<phase>); waiting <n>s (total waited <time>)
msreview agent: phase=<phase> model=<model> ... complete=<bool> ... rl_wait=<seconds>
msreview usage: <role>:<model> llm=.. tools=.. in=.. cached=.. out=.. reasoning=.. cost=$..; est_cost=$..
msreview result: status=<status> verdict=<verdict> findings=<n> review=<url>
```

The `rate-limited` line appears only while a model is rate-limited, and the usage line
adds `rl_wait=<seconds>` for a model that waited (see
[Rate limits and the time limit](#rate-limits-and-the-time-limit)).

Model-written text never goes to the log. `--verbose` adds more detail from the
SDKs. After a run is terminated (for example by a cancel or a deadline), a warning
"Activity not found on completion" may appear. It is expected and harmless.

## Troubleshooting

Start with `msreview doctor`, which checks the key, Workflows access, the agent
environment and the App settings. In Actions, the step summary and the `::error` or
`::warning` annotation give the reason for a failure or a report.

### `msreview setup` fails with a network timeout

The first `setup` downloads the agent environment, which includes a large compiled
wheel. On a slow connection uv's default 30-second HTTP timeout can expire; run it
again with a longer one, for example `UV_HTTP_TIMEOUT=180 msreview setup`.

### `msreview doctor` reports HTTP 429 for a model

The model is rate-limited at that moment. `doctor` does not wait, but a review does:
it waits for the limit and continues (see
[Rate limits and the time limit](#rate-limits-and-the-time-limit)). Run
`doctor --models` again later to check that the model answers.

### "the Mistral API key was refused by the Workflows API (… HTTP 401)"

The review stops with exit code 2 before it starts a worker. The preflight call
`GET /v1/workflows/workers/whoami` was refused.

- Check that the key is correct and still active. In Actions, check that the secret
  `MSREVIEW_MISTRAL_API_KEY` exists in this repository or organisation, and that the
  workflow passes it (`msreview status OWNER/REPO` shows both). A workflow from 0.1.x
  passes `secrets.MISTRAL_API_KEY` instead: then that secret must hold the key, and
  `msreview enable --secrets-only` (which sets only `MSREVIEW_MISTRAL_API_KEY`) does
  not change what it uses.
- Check that the key's workspace has **Workflows** access (Public Preview). A key that
  works for chat completions can still be refused here.
- In the CLI, check which key is used: `MSREVIEW_MISTRAL_API_KEY_COMMAND` wins over
  `MISTRAL_API_KEY`, which wins over the config file's `api_key_command`.
- `msreview doctor` shows "workflows access: ok" and the first characters of the
  workspace namespace when the key works. Compare the namespace with the workspace you
  expect.

HTTP 403 has the same causes. "cannot reach the Mistral Workflows API" means a
network problem or an outage, not a key problem.

### The review was not posted ("report" status)

Nothing failed. The review is in the step summary, the artifact or the CLI output. The
first line of the report says why it was not posted:

| Reason | Fix |
|---|---|
| "the GitHub App '…' is not installed on owner/repo" | Install the App on that repository (App settings → Install App → configure). Check with `msreview app-check owner/repo` |
| "no GitHub App credentials were configured" | Set the variable `MSREVIEW_APP_CLIENT_ID` and the secret `MSREVIEW_APP_PRIVATE_KEY` (Action), or `MSREVIEW_APP_CLIENT_ID` and a key path or command (CLI) |
| "the GitHub App token step did not succeed" | See the token step's log. Common causes: a wrong client ID; a secret that does not contain the full PEM ("Invalid keyData"); the App not installed; or `labels: "true"` while the App lacks Issues: write (set `labels: "false"` or grant the permission) |
| "the App installation … lacks Pull requests: write" | Give the App Pull requests: Read & write. The installation owner must then accept the new permissions (GitHub shows a banner on the installation page) |
| "posting was disabled (post: never)" | Remove `post: never` or `--post never` |
| "the pull request received new commits while this review ran" | Nothing to fix. The newer push gets its own review |

### The review was skipped

- **already reviewed:** this head commit was already reviewed in full with the same
  configuration. Push a new commit, change the configuration, or use `--force` in the CLI.
- **draft:** enable `review.drafts` or the input `review-drafts`, or use `--force`.
- **closed:** use `--force` in the CLI to review a closed PR anyway.
- **superseded:** the PR head moved on after the run was triggered.
- **The job shows as skipped for a fork PR:** this is expected with the standard
  workflow. See [Pull requests from forks](security.md#pull-requests-from-forks).
- **The fork review fails at "Check that the label was added by someone with write
  access":** the person who added `msreview: safe-to-review` has less than write access
  (for example the triage role), or the permission could not be read. Remove the label;
  someone with write, maintain or admin access can add it again.

### The verdict is `incomplete`

The summary's Coverage section and the reasons explain why. Usually some reviewable
files were not opened, an agent reached its tool, time or cost budget, or an agent's
notes ended early. Options: raise `max-tool-calls`, `explorer-minutes` or
`max-cost-usd`; exclude generated or vendored paths with `exclude`; or split very large
PRs. When the reason is "the run reached its N-minute time limit and was finished
early", the run reached its wind-down, usually because the models were rate-limited
for a long time (the reason and the run details give the total wait). Run the review
again later, raise `deadline-minutes` together with `timeout-minutes`, or use a preset
with fewer agents. Open findings always give the verdict `findings`, even when coverage is
incomplete. Possible issues (findings that the verifier could not confirm, listed only
in the summary) do not count as open findings: with only those, the verdict follows
coverage. See [models.md](models.md#how-ensemble-results-are-published).

If a run fails after it has started, the existing summary comment gets a notice that
the latest run did not finish. The label becomes `msreview: incomplete`.

### Other messages

- **"no Mistral API key: the Action input `mistral-api-key` is empty"**: the
  `MSREVIEW_MISTRAL_API_KEY` secret (or, in a workflow from 0.1.x, `MISTRAL_API_KEY`)
  is missing or not available to this workflow, or the workflow does not pass it to the
  action. `msreview status OWNER/REPO` shows which name the workflow uses and whether it
  is set. After `msreview update --force` moved a 0.1.x workflow to the new name, run
  `msreview enable OWNER/REPO --secrets-only` to set it. GitHub does not enforce
  `required: true` for the inputs of a composite action, so msreview checks it itself.
- **"the agent environment is not installed; run `msreview setup`"**, or doctor
  reports a version mismatch: run `msreview setup`. It rebuilds the environment when the
  version differs. In the Action, both environments are built in each job.
- **"SERVER_URL is set to a server other than https://api.mistral.ai"**: unset
  `SERVER_URL`, or set `MSREVIEW_ALLOW_CUSTOM_SERVER=1` if you really mean it.
- **The deadline was reached** ("the review did not finish within the N-minute
  deadline"): the run did not finish even after the wind-down. Raise
  `deadline-minutes` and the job's `timeout-minutes` together, and keep the timeout
  about 20 minutes above the deadline.
- **The job was cancelled at `timeout-minutes` and nothing was posted:** the job's
  timeout is too close to the deadline, for example a workflow copied from the 0.1.0
  example (`timeout-minutes: 45` with the new default deadline of 90). Set
  `timeout-minutes: 110`, or lower both, for example `deadline-minutes: "30"` with
  `timeout-minutes: 50`.
- **"N finding(s) could not be placed on the diff"**: GitHub refused those
  comment positions. The findings are in the summary.
- **Threads of fixed findings stay open:** this is expected. See
  [setup-github-app.md](setup-github-app.md#why-not-contents-write).
- **No labels appear:** the App needs Issues: write, and `labels` must be on in both
  the input and the repository file.
- **HTTP 429 from the models:** expected in workspaces with low limits. The agents
  wait and continue; the review only takes longer. See
  [Rate limits and the time limit](#rate-limits-and-the-time-limit). An agent stops
  retrying only at the run's time limit, at its cost share, or after 6 lost streams or
  timeouts in a row; its error says which. If reviews are often slow, use `single` or
  `balanced`, which run fewer agents at the same time.
