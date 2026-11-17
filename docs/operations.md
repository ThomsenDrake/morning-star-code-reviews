# Operations

This page covers running the reviewer over time: what it leaves in your Mistral
workspace, what it costs, how to control telemetry, and how to fix common problems.

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
msreview: execution <id> on deployment <name> (workspace namespace <first 8 chars>)
msreview: scope=<full|incremental|carry_over> ...
msreview agent: phase=<phase> model=<model> ... complete=<bool> ...
msreview usage: <role>:<model> llm=.. tools=.. in=.. cached=.. out=.. reasoning=.. cost=$..; est_cost=$..
msreview result: status=<status> verdict=<verdict> findings=<n> review=<url>
```

Model-written text never goes to the log. `--verbose` adds more detail from the
SDKs. After a run is terminated (for example by a cancel or a deadline), a warning
"Activity not found on completion" may appear. It is expected and harmless.

## Troubleshooting

Start with `msreview doctor`, which checks the key, Workflows access, the agent
environment and the App settings. In Actions, the step summary and the `::error` or
`::warning` annotation give the reason for a failure or a report.

### "the Mistral API key was refused by the Workflows API (… HTTP 401)"

The review stops with exit code 2 before it starts a worker. The preflight call
`GET /v1/workflows/workers/whoami` was refused.

- Check that the key is correct and still active. In Actions, check that the secret
  `MISTRAL_API_KEY` exists in this repository or organisation, and that the workflow
  passes it.
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

### The verdict is `incomplete`

The summary's Coverage section and the reasons explain why. Usually some reviewable
files were not opened, an agent reached its tool, time or cost budget, or an agent's
notes ended early. Options: raise `max-tool-calls`, `explorer-minutes` or
`max-cost-usd`; exclude generated or vendored paths with `exclude`; or split very large
PRs. Open findings always give the verdict `findings`, even when coverage is
incomplete.

If a run fails after it has started, the existing summary comment gets a notice that
the latest run did not finish. The label becomes `msreview: incomplete`.

### Other messages

- **"the agent environment is not installed; run `msreview setup`"**, or doctor
  reports a version mismatch: run `msreview setup`. It rebuilds the environment when the
  version differs. In the Action, both environments are built in each job.
- **"SERVER_URL is set to a server other than https://api.mistral.ai"**: unset
  `SERVER_URL`, or set `MSREVIEW_ALLOW_CUSTOM_SERVER=1` if you really mean it.
- **The deadline was reached:** raise `deadline-minutes` and the job's
  `timeout-minutes` together, and keep the deadline about 5 minutes below the timeout.
- **"N finding(s) could not be placed on the diff"**: GitHub refused those
  comment positions. The findings are in the summary.
- **Threads of fixed findings stay open:** this is expected. See
  [setup-github-app.md](setup-github-app.md#why-not-contents-write).
- **No labels appear:** the App needs Issues: write, and `labels` must be on in both
  the input and the repository file.
- **HTTP 429 from the models:** the SDK retries automatically. With the `best` preset,
  three explorers run at the same time, so a workspace with low rate limits may want
  `single` or `balanced`.
