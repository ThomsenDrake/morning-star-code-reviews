# Morning Star Code Reviews

An open-source GitHub pull-request reviewer. Each review runs as a
[Mistral Workflow](https://docs.mistral.ai/) and uses Mistral-hosted models. It posts
as **your own GitHub App**, or it writes a report when it cannot post.

- Command: `msreview` (Python package `morning_star_review`, distribution `morning-star-code-reviews`)
- Runs as: a GitHub Action (composite) or a local CLI
- Status: 0.1.1, alpha. See [Limitations](#limitations).
- License: Apache-2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE). The agent
  environment installs a proprietary component; see [Limitations](#limitations).

## How it works

- **Your infrastructure, your keys.** The reviewer runs in your GitHub Actions job (or
  on your machine). It starts a short-lived Workflows worker in your Mistral workspace,
  runs one workflow execution, then stops the worker. No hosted review service is involved.
- **Read-only agents explore the change.** One or more explorer agents read the diff and
  the repository through five read-only tools (list, read, read base version, search,
  show diff). They cannot run code, write files or use the network. Pull request code
  is never executed.
- **Ensemble and verification.** The default `best` preset runs three explorers from
  different model families in parallel. A deterministic merge combines their findings
  and records which models voted for each one. Then a verifier agent re-reads the code
  to confirm or refute each candidate.
- **Deterministic placement.** Findings are anchored to diff lines by code, not by the
  model. The code that a finding quotes is matched against the head file, and the
  finding moves to the line that really holds it. Suggestion blocks are posted only
  when they pass validation: for example, the file must still parse after the change
  is applied.
- **Honest verdicts.** Each PR gets one summary comment and one label: `clean`,
  `findings` or `incomplete`. The verdict is `clean` only when every reviewable file was
  read and every agent finished. A review that found nothing but could not finish is
  `incomplete`, never a guessed `clean`. Findings that the verifier could neither
  confirm nor refute are listed as "possible issues" and do not make the verdict
  `findings` on their own.
- **Incremental and idempotent.** On later pushes, only the files changed since the last
  reviewed commit are reviewed. Findings whose code changed are marked as addressed.
  Hidden markers make retries safe: a retry never posts duplicates.

A longer description is in [docs/architecture.md](docs/architecture.md).

## Quickstart: GitHub Action

You need a Mistral API key for a workspace that has **Workflows** access. Mistral
Workflows is a Public Preview product.

1. **Create a GitHub App** (once per account or organisation). Step-by-step guide:
   [docs/setup-github-app.md](docs/setup-github-app.md).
   - Repository permissions: **Contents: Read**, **Pull requests: Read & write**,
     **Issues: Read & write** (for the verdict labels), **Metadata: Read**.
   - No webhook: clear "Active" under Webhook. The Action calls GitHub itself.
   - Generate a private key and note the App's **Client ID**.
2. **Install the App** on the repositories that you want reviewed.
3. **Add to each repository** (Settings → Secrets and variables → Actions):
   - Secret `MISTRAL_API_KEY`: your Mistral API key.
   - Secret `MSREVIEW_APP_PRIVATE_KEY`: the full contents of the App's `.pem` file.
   - Variable `MSREVIEW_APP_CLIENT_ID`: the App's Client ID.

   Organisation-level secrets and variables also work.
4. **Copy the workflow**
   [`examples/workflows/morning-star-code-review.yml`](examples/workflows/morning-star-code-review.yml)
   to `.github/workflows/` in your repository. Pin the action to the full commit SHA of
   the release that you reviewed.
5. **Open a pull request.** The review appears as one summary comment, inline comments
   from `<your-app>[bot]`, and a `msreview: …` label. The full report is also in the job's
   step summary and in an artifact.

The workflow does not need `actions/checkout`. If the App credentials are missing or
the App is not installed, the run does not fail: it produces a report (step summary
and artifact) that tells you why nothing was posted.

**Pull requests from forks** get no secrets on `pull_request`, so the example skips
them. To review them, use the hardened `pull_request_target` template
[`examples/workflows/morning-star-code-review-forks.yml`](examples/workflows/morning-star-code-review-forks.yml).
It reviews automatically for trusted authors. For other authors, a maintainer must add a
label, and the approval applies to one head commit only. The job checks that the person
who added the label has write, maintain or admin access, because the triage role can
add labels too. Read [docs/security.md](docs/security.md) before you use it.

## Quickstart: CLI

Requires Python 3.12 to 3.14, `git` and [uv](https://docs.astral.sh/uv/), on macOS or Linux.

```bash
uv tool install git+https://github.com/ThomsenDrake/morning-star-code-reviews@v0.1.1
msreview setup                 # builds the agent environment (pinned, hash-checked)
export MISTRAL_API_KEY=...     # or use a key command, see below
msreview doctor                # checks the key, Workflows access and the installation
msreview review https://github.com/OWNER/REPO/pull/123
```

Without App credentials the CLI only reports. It prints the review as Markdown and
saves `report.md`, `findings.json` and `run.json` under `./msreview-reports/`. To post
as your App, give the CLI the App's client ID and private key:

```bash
export MSREVIEW_APP_CLIENT_ID=Iv23li...
export MSREVIEW_APP_PRIVATE_KEY_PATH=~/.config/morning-star-review/app.pem   # chmod 600
msreview app-check OWNER/REPO       # is the App installed, with which permissions?
msreview review OWNER/REPO#123      # posts as <your-app>[bot]
msreview review OWNER/REPO#123 --post never --json   # report only, JSON outcome
```

Only a short-lived child process reads the App key. It is never passed to the review
process or to the agents.

### Keep keys out of files and shell history

Each key can come from a command that prints it, for example a secrets-manager CLI.
The command runs once per review, and its output is kept only in memory.

```toml
# ~/.config/morning-star-review/config.toml
[mistral]
api_key_command = "op read op://Private/Mistral/api-key"

[app]
client_id = "Iv23li..."
private_key_command = "op read op://Private/msreview-app/private-key"

[defaults]
preset = "best"
```

The same settings exist as environment variables: `MSREVIEW_MISTRAL_API_KEY_COMMAND`,
`MSREVIEW_APP_PRIVATE_KEY_COMMAND` and `MSREVIEW_APP_CLIENT_ID`. Any CLI that prints
the secret works (`op`, `pass`, `vault`, `aws secretsmanager`, `gcloud secrets`, …).
All commands and flags are in [docs/cli.md](docs/cli.md).

## Presets and models

| Preset | Explorers (effort) | Verifier | When to use |
|---|---|---|---|
| **`best`** (default) | `zai-glm-5-3`, `mistral-large-4`, `deepseek-v4-1-flash-1017` (all high), in parallel | `zai-glm-5-3` (high) | Highest confidence: three model families vote, one verifies |
| `balanced` | `zai-glm-5-3` (high) | `mistral-large-4` (high) | One explorer, checked by a different family |
| `single` | `zai-glm-5-3` (high) | none | Lower cost and latency |
| `fast` | `zai-glm-5-3-flash` (high) | none | Lowest cost |
| `mistral` | `mistral-large-4` (high) | `mistral-large-4` (high) | Mistral's own model only |

A formatter model (`mistral-large-4`, low effort; `zai-glm-5-3-flash` for `fast`) runs
only when an agent's notes cannot be parsed. You can override the explorers, verifier
and efforts for each run. The default per-run cost cap is **$10.00**.

**Rate limits make a review slower, not fail.** When a model answers with a rate limit
or a temporary error, the agent waits (15 s at first, up to 3 minutes per wait) and
tries again, and the wait does not count against its own time budget. The whole run
has one time limit, `deadline-minutes` (default 90). 15 minutes before it, the run
winds down and still posts what it has, with the verdict `incomplete` (or `findings`)
and the reason.
Set the job's `timeout-minutes` about 20 above the deadline (the example workflow uses
110). See [docs/operations.md](docs/operations.md#rate-limits-and-the-time-limit).

**Measured results** (2026-11-17, pre-release build): 60 report-mode runs on our own
planted-bug fixture project. There were four cases (planted bugs, clean refactor,
prompt injection, an 80-file PR) and three runs each:

| Config | Recall | False positives ≥ medium | Mean cost / run | Mean time |
|---|---:|---:|---:|---:|
| `best` preset | 18/18 | 0 | $0.43 | 86 s |
| `zai-glm-5-3` alone | 18/18 | 0 | $0.21 | 35 s |
| `mistral-large-4` alone | 18/18 | 0 | $0.09 | 82 s |
| `deepseek-v4-1-flash-1017` alone | 18/18 | 0 | $0.05 | 31 s |
| `zai-glm-5-3-flash` alone | 17/18 | 0 | $0.02 | 49 s |

The strong single models tied with `best` on this small benchmark. `best` is the default
because every planted bug got all three votes and its anchors and suggestions were the
most reliable. No run leaked an injected mention, link or marker. Several issues found
in that run have since been fixed, and the numbers have not been measured again yet.
The method, the per-case tables and the caveats are in [docs/models.md](docs/models.md)
and [`eval/`](eval/).

## Configuration

Add an optional `.morning-star-review.yml` to the repository's default branch. It is
always read from the PR's base commit, so a PR cannot change how it is reviewed.

```yaml
version: 1
preset: best
review:
  inline_min_severity: medium
  instructions: |
    Money is stored as integer cents; flag float arithmetic on amounts.
exclude: ["docs/**"]
```

The repository file can only *lower* budgets. The Action always uses the file. The
CLI does not check who owns the repository: without `--trust-repo-config` it ignores
the file's model and budget settings (`preset`, `models`, `ensemble`, `verify`,
`reasoning_effort`, `budgets`) and the settings that steer the review (`exclude`,
`review.instructions`, `review.path_instructions`, `review.inline_min_severity`) for
**every** repository, including your own, and the summary lists what it ignored. So
the example above has an effect in the CLI only with `--trust-repo-config`; pass it
when you trust whoever can merge to the base branch. The full schema, every Action
input and the precedence rules are in [docs/configuration.md](docs/configuration.md).

## Limitations

- **Mistral Workflows is in Public Preview.** Its APIs and behaviour can change. This
  release pins `mistralai-workflows==3.19.0` and `mistralai==3.7.0`. Your workspace
  must have Workflows access. `deepseek-v4-1-flash-1017` is a Public Preview model.
- **Proprietary harness.** The agent environment installs `mistralai-agents`
  (Apache-2.0). That package depends on `mistralai-vibe-harness` (declared
  "Proprietary", ships a compiled core) and `mistralai-vibe-sdk` (no license
  metadata). This project does not vendor or redistribute them: they come from PyPI
  when you run `msreview setup` or the Action, under their publisher's terms. See [`NOTICE`](NOTICE).
- **Threads of fixed findings are not resolved automatically.** GitHub lets a token
  resolve a review thread only when it has **Contents: write**. That permission also
  lets the token push code, so we **do not recommend** granting it, and msreview always
  requests tokens with Contents: read. When the flagged code changes, the bot replies
  "Looks addressed" and hides its comment as resolved. You can resolve the thread yourself.
- **Fork PRs** need the separate `pull_request_target` template and a maintainer's
  approval for each push from an untrusted author, or an on-demand CLI review.
- **Deployment records accumulate.** Each PR leaves about one inactive deployment
  record in your workspace, and the API cannot delete these records. See
  [docs/operations.md](docs/operations.md).
- **Findings can be wrong or missing.** The reviewer only comments: it never approves
  or requests changes. It does not replace human review.
- No GitHub Enterprise Server support. Only `github.com` and `api.mistral.ai`.

## Documentation

| Document | Contents |
|---|---|
| [docs/setup-github-app.md](docs/setup-github-app.md) | Creating and installing the GitHub App |
| [docs/configuration.md](docs/configuration.md) | Repository file, Action inputs and outputs, CLI flags, environment variables, precedence |
| [docs/cli.md](docs/cli.md) | `msreview` commands, user config file, exit codes |
| [docs/models.md](docs/models.md) | Models, efforts, presets, evaluation method and results |
| [docs/security.md](docs/security.md) | Threat model, fork template, residual risks |
| [docs/operations.md](docs/operations.md) | Deployments, costs, telemetry, troubleshooting |
| [docs/architecture.md](docs/architecture.md) | How a review runs, step by step |
| [CHANGELOG.md](CHANGELOG.md) · [SECURITY.md](SECURITY.md) | Releases · reporting vulnerabilities |
