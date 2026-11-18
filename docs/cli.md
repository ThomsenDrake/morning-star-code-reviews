# The `msreview` CLI

The CLI reviews a single pull request on demand. It uses the same code path as the
GitHub Action. It posts as your GitHub App when the App credentials are set and the App
is installed. Otherwise it prints a report.

## Install

Requirements: Python 3.12 to 3.14, `git`, and [uv](https://docs.astral.sh/uv/), on
macOS or Linux.

```bash
uv tool install git+https://github.com/ThomsenDrake/morning-star-code-reviews
# or a specific release:
uv tool install git+https://github.com/ThomsenDrake/morning-star-code-reviews@v0.1.1

msreview setup
```

The package is not on PyPI. Installation creates **two environments**:

- The **CLI environment** (`uv tool install`) holds the driver and the Workflows worker.
  It does not contain the Agents SDK.
- The **agent environment** (`msreview setup`) is at
  `~/.cache/morning-star-review/agent-<version>/`. It holds the Agents SDK that the
  review agents run in. Its dependencies are pinned and hash-checked
  (`--require-hashes`), and this package is installed into it from the same source as
  the CLI. Both environments therefore always run the same version.

The two environments are separate because the Agents SDK registers extra activities
that a Workflows worker must not serve. After you upgrade the CLI, run
`msreview setup` again. `msreview doctor` reports a version mismatch.

## Commands

### `msreview review <PR>`

Review one pull request. `<PR>` can be any of these forms:
`https://github.com/OWNER/REPO/pull/123` (also with `/files` and similar suffixes),
`github.com/OWNER/REPO/pull/123`, `OWNER/REPO/pull/123`, `OWNER/REPO#123`, or the
REST URL `https://api.github.com/repos/OWNER/REPO/pulls/123`.

```bash
msreview review OWNER/REPO#123                       # post if possible, else report
msreview review OWNER/REPO#123 --post never          # report only
msreview review OWNER/REPO#123 --preset single       # one explorer, no verifier
msreview review OWNER/REPO#123 --explorers zai-glm-5-3,mistral-large-4 --verify
msreview review OWNER/REPO#123 --json > outcome.json # machine-readable outcome
msreview review OWNER/REPO#123 --force               # review again, or review a draft/closed PR
```

Flags:

| Flag | Meaning |
|---|---|
| `--post auto\|never` | `auto` (default) posts as the App when possible. `never` produces a report only |
| `--preset NAME` | `best`, `balanced`, `single`, `fast` or `mistral` (see [models.md](models.md)) |
| `--explorers A,B,…` | Explorer models, run in parallel as an ensemble (up to 5) |
| `--explorer-model ID` | One explorer model |
| `--formatter-model ID` | The model that turns unparseable notes into structure |
| `--effort LEVEL` | Explorer reasoning effort: `default`, `low`, `medium`, `high` or `max` |
| `--verify`, `--no-verify` | Turn the verifier on or off |
| `--verifier-model ID`, `--verifier-effort LEVEL` | The verifier's model and effort |
| `--max-cost-usd X` | Cost limit for the whole run (default 10.00, at most 100) |
| `--max-tool-calls N`, `--explorer-minutes N` | Budget per explorer (defaults 60 and 15; at most 200 and 25). A flash explorer outside `best` gets 25 tool calls unless you set this |
| `--verifier-max-tool-calls N`, `--verifier-minutes N` | Verifier budget (defaults 30 and 8; at most 200 and 25). A flash verifier gets 25 tool calls unless you set this |
| `--deadline-minutes N` | Overall time limit for the whole run (default 90, 20 to 300). The run winds down 15 minutes before it and still posts what it has. Rate-limit waits make a run slower, so they count against this limit only (see [operations.md](operations.md#rate-limits-and-the-time-limit)) |
| `--expected-head-sha SHA` | Skip the review if the PR head is no longer this commit |
| `--out DIR` | Where to write `report.md`, `findings.json` and `run.json` (default `./msreview-reports`). The files are replaced on each run |
| `--json` | Print the outcome as JSON instead of Markdown |
| `--force` | Review drafts, closed PRs and head commits that were already reviewed |
| `--trust-repo-config` | Let the repository file choose models, budgets, exclusions and instructions (see [configuration.md](configuration.md#untrusted-repository-files-cli)) |
| `--read-token env\|gh\|none` | Read-only GitHub token, used when there is no App token: `env` uses `GITHUB_TOKEN` or `GH_TOKEN`, `gh` runs `gh auth token`, `none` uses no token |
| `--fail-on LEVEL` | `never`, `findings`, `high` or `critical`: exit 3 when the open findings reach this level (possible issues that the verifier could not confirm do not count) |
| `--no-labels`, `--no-suggestions` | Do not set verdict labels, or do not post suggestion blocks |
| `--local` | Run the workflow in this process, without starting a Workflows worker (for development and debugging) |
| `--verbose` | More log output on stderr |
| `--telemetry` | Turn on the SDK telemetry for this run (off by default) |

You need a read token to make report-mode reviews of private repositories without the
App, for example `--read-token gh`. Public repositories work without a token, but
GitHub's limits for unauthenticated requests are low.

**Output.** Progress lines go to stderr. The result goes to stdout: the review as
Markdown, or the outcome as JSON with `--json`. The JSON outcome has, among others,
`status`, `verdict`, `verdict_reasons` (for example why the review is `incomplete`),
`possible_issues` (the number of findings that the verifier could not confirm) and
`findings`. The last stderr lines are stable and easy to parse:

```
msreview usage: explorer:zai-glm-5-3 llm=6 tools=9 in=… cached=… out=… reasoning=… cost=$0.0612; …; est_cost=$0.19
msreview result: status=posted verdict=findings findings=4 review=https://github.com/…
```

When a model is rate-limited, a progress line says so, for example
`msreview: mistral-large-4 rate-limited (explore); waiting 60s (total waited 1m 45s)`,
and the usage line adds `rl_wait=<seconds>` for that model. The review continues.

When the CLI posts as your App, it mints the App token before the review and mints a
new one before posting if the first is older than 45 minutes (installation tokens
expire after 60 minutes). Both times, only the short-lived mint process reads the
private key.

### `msreview doctor [--models] [--deployments]`

Checks the setup and changes nothing:

- the version, `git`, the user config file and the agent environment (and that its version matches);
- the GitHub App settings for posting (optional: a warning, not a failure);
- the Mistral key (it is never printed), and Workflows access through `whoami`;
- `--models`: sends one 1-token chat completion to each allowed model;
- `--deployments`: lists the `msreview-*` deployments in your workspace, active and inactive, with worker counts and the last heartbeat (see [operations.md](operations.md#deployments)).

Exit code 0 when every required check passes.

### `msreview setup [--force] [--check] [--source SPEC]`

Builds the agent environment. `--check` only verifies it. `--force` rebuilds it.
`--source` installs this package from a pip spec or a local path instead of the source
that the CLI was installed from. Use it, for example, with a local checkout during
development.

### `msreview app-check OWNER/REPO`

Shows whether your GitHub App is installed on the repository, and which permissions it
grants. The exit code is 1 when the App is not installed or when Pull requests: write
is missing. It also notes when Issues: write (labels) is missing.

### `msreview ci` and `msreview ci-publish`

The entry points that the GitHub Action runs. `ci` runs the review, and `ci-publish`
posts its result with a write token minted after the review (see
[security.md](security.md#github-app-tokens)). They read the `MSREVIEW_*` variables
that `action.yml` sets. You do not normally run them yourself.

## User config file

`~/.config/morning-star-review/config.toml` (or the path in `$MSREVIEW_CONFIG`). Every
key is optional. Environment variables win over the file.

```toml
[mistral]
# A command that prints the Mistral API key. It runs once per review, and the output
# is kept in memory only. MISTRAL_API_KEY and MSREVIEW_MISTRAL_API_KEY_COMMAND win.
api_key_command = "op read op://Private/Mistral/api-key"

[app]
client_id = "Iv23li..."                                   # MSREVIEW_APP_CLIENT_ID
private_key_command = "op read op://Private/msreview-app/private-key"   # MSREVIEW_APP_PRIVATE_KEY_COMMAND
# private_key_path = "~/.config/morning-star-review/app.pem"            # MSREVIEW_APP_PRIVATE_KEY_PATH (mode 0600)

[defaults]
preset = "best"            # your default; a trusted repository file and --preset win over it
max_cost_usd = 10.0        # same as --max-cost-usd
read_token = "env"         # env | gh | none
fail_on = "never"          # never | findings | high | critical
allowed_models = ["zai-glm-5-3", "mistral-large-4", "zai-glm-5-3-flash", "deepseek-v4-1-flash-1017"]

[agent]
python = "~/.cache/morning-star-review/agent-0.1.1/bin/python"   # MSREVIEW_AGENT_PYTHON
```

The order for the Mistral key is: `MSREVIEW_MISTRAL_API_KEY_COMMAND`, then
`MISTRAL_API_KEY`, then `[mistral] api_key_command`. For the App key, a command wins
over a path from the same source, and environment variables win over the file.

A key command can be any program that prints the secret on stdout, for example
`op read …`, `pass show …`, `vault kv get -field=key …`, or
`aws secretsmanager get-secret-value --query SecretString --output text …`. The command
runs through your shell and must finish within 60 seconds. Its output, without leading
and trailing whitespace, is the secret. The output is never logged, and error messages
show neither the command nor its output.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Posted, report, skipped or superseded, and `--fail-on` was not triggered |
| 1 | The review failed (see the reason in the output) |
| 2 | Usage, configuration or preflight error (bad flags, no key, a key refused by the Workflows API, missing agent environment) |
| 3 | The `--fail-on` threshold was met |
| 130 | Cancelled (Ctrl-C or SIGTERM). The workflow execution is terminated and the worker is stopped first |
