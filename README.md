# Morning Star Code Reviews

An open-source GitHub pull-request reviewer. Each review runs as a
[Mistral Workflow](https://docs.mistral.ai/) and uses Mistral-hosted models. It posts
as **your own GitHub App**, or it writes a report when it cannot post.

- Command: `msreview` (Python package `morning_star_review`, distribution `morning-star-code-reviews`)
- Runs as: a GitHub Action (composite) or a local CLI
- Status: 0.3.0, alpha. See [Limitations](#limitations).
- License: Apache-2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE). The agent
  environment installs a proprietary component; see [Limitations](#limitations).

## How it works

- **Your infrastructure, your keys.** The reviewer runs in your GitHub Actions job (or
  on your machine). It starts a short-lived Workflows worker in your Mistral workspace,
  runs one workflow execution, then stops the worker. No hosted review service is
  involved. Your keys are stored only as GitHub Actions secrets of the repositories
  that you enable.
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
- **Honest verdicts.** The verdict is `clean` only when every reviewable file was read
  and every agent finished. A review that found nothing but could not finish is
  `incomplete`, never a guessed `clean`. Findings that the verifier could neither
  confirm nor refute are listed as "possible issues" and do not make the verdict
  `findings` on their own.
- **One place for what is still open.** Each PR gets one summary comment, updated on
  every push: every finding that is still open (with its id, explanation and fix), and
  a record of the findings that were fixed or dismissed. One label says whether the PR
  is ready to merge: `msreview: ready to merge`, `msreview: findings` or
  `msreview: incomplete`. See [Working with findings](#working-with-findings).
- **Incremental and idempotent.** On later pushes, only the files changed since the last
  reviewed commit are reviewed. Findings whose code changed are marked as fixed.
  Hidden markers make retries safe: a retry never posts duplicates.

A longer description is in [docs/architecture.md](docs/architecture.md).

## Quickstart

You need:

- a Mistral API key for a workspace that has **Workflows** access (Mistral Workflows
  is a Public Preview product);
- the [GitHub CLI](https://cli.github.com/) `gh`, logged in with the scopes `repo` and
  `workflow` (see [GitHub CLI scopes](#github-cli-scopes));
- [uv](https://docs.astral.sh/uv/), on macOS or Linux.

1. **Create a GitHub App** (once). Step-by-step guide:
   [docs/setup-github-app.md](docs/setup-github-app.md).
   - Repository permissions: **Contents: Read**, **Pull requests: Read & write**,
     **Issues: Read & write** (for the status labels), **Metadata: Read**.
   - No webhook: clear "Active" under Webhook. The Action calls GitHub itself.
   - Note the App's **Client ID**, then generate a private key. Your browser downloads
     a `.pem` file.
2. **Install the App** on the repositories that you want reviewed (on the App's page:
   **Install App**).
3. **Install msreview and enable your repositories:**

   ```bash
   uv tool install git+https://github.com/ThomsenDrake/morning-star-code-reviews@v0.3.0
   msreview enable you/repo1 you/repo2 --app-key-file ~/Downloads/<app>.private-key.pem
   ```

   `msreview enable` asks for the Mistral API key once (the input is hidden) and for
   the App's Client ID, and checks both keys before it changes any repository. Then,
   for each repository, it adds the review workflow and stores the keys as that
   repository's GitHub Actions secrets. It prints one result line per repository.
   The keys go **only** into the repositories' secrets: msreview stores nothing on
   your computer. When it is done, delete the `.pem` file.
4. **Open a pull request.** The review appears as one summary comment, inline comments
   from `<your-app>[bot]`, and a `msreview: …` label. The full report is also in the
   job's step summary and in an artifact.

You do not need `msreview setup` for this. It is only for [local reviews](#local-reviews-cli).

### Working with findings

The summary comment is the PR's list of what is still open. msreview edits it in place
after every push (it stays where it was first posted), so a person or a coding agent
can read it at any time, for example with `gh pr view <number> --comments`:

- **Open findings:** a table of every finding that is still open, inline or not, with
  its id (`f` and 12 hex digits), severity, location and status (`new` or `still
  open`, with a link to its thread). A collapsed block below it holds each finding's
  explanation and suggested fix.
- **Fixed and dismissed findings:** a collapsed record of the PR's earlier findings,
  each "fixed in `<commit>`" or "dismissed by `<login>`: `<reason>`".
- **Ready to merge:** a line under the verdict, and the label. The PR is ready when
  the review is complete and no open finding is at or above `review.blocking_severity`
  (default `medium`): low findings alone do not block it.

A finding is **fixed** when a later push changes the code it flags. To **dismiss** a
false positive (or a finding you accept), either resolve its inline thread, or comment
on the PR:

```text
/msreview dismiss f0123456789ab the caller validates this
/msreview dismiss f0123456789ab f00000000000b intended
```

The command works for every finding, also the ones without an inline thread, and it
takes effect within a minute: the comment starts a short run that updates the summary,
the label and the finding's thread without running any model. Only people with write
access can dismiss (the PR author too, unless `review.author_may_dismiss: false`).
A dismissed finding stays dismissed: when a later review reports it again, even in
other words, it is recorded as a repeat of the dismissed one and not shown as open.

### What `msreview enable` changes

In each repository:

| Kind | Name | Value |
|---|---|---|
| Workflow file | `.github/workflows/morning-star-code-review.yml` | The review workflow. It pins the action to the full commit SHA of the release (with a `# v0.3.0` comment), sets `timeout-minutes: 110`, and sets `preset` when you pass `--preset` |
| Secret | `MSREVIEW_MISTRAL_API_KEY` | Your Mistral API key |
| Secret | `MSREVIEW_APP_PRIVATE_KEY` | The App's private key (the contents of the `.pem` file) |
| Variable | `MSREVIEW_APP_CLIENT_ID` | The App's Client ID (not a secret) |

Every name starts with `MSREVIEW_`, so msreview never collides with your own secrets.
A repository's own `MISTRAL_API_KEY` (for example, one that another workflow uses) is
never set, overwritten or deleted. If a repository has one, `msreview enable` prints a
note that it was left untouched.

- By default, the workflow file is committed directly to the default branch with the
  message "Enable Morning Star Code Review". With `--pr`, msreview opens a pull
  request from the branch `msreview/enable` instead, for example when the default
  branch is protected. That pull request is itself reviewed by the workflow it adds:
  GitHub runs `pull_request` workflows from the pull request's branch, and the secrets
  are already set. So its review is a first end-to-end check of the setup.
- You can run it again safely. If the file already has the same content, nothing is
  committed. If a different file is at that path, msreview shows a short summary of
  the differences and does not replace it unless you pass `--force`. To move a
  workflow to a new release, use `msreview update`.
- If the App is not installed on a repository, msreview skips that repository and
  prints a link where you can add it.
- A failing or skipped repository does not stop the others. The exit code is 1 when
  any repository failed or was skipped.
- `--secrets-only` sets only the secrets and the variable. `--no-secrets` writes only
  the workflow file.

All flags are in [docs/cli.md](docs/cli.md#msreview-enable).

### Why the keys are stored in every repository

GitHub has no account-wide Actions secrets for personal accounts. Actions secrets
exist only for a repository, an environment or an organisation. So every repository
needs its own copy, and `msreview enable` sets them for many repositories in one
command. `gh` encrypts each value with the repository's public key before it is
uploaded. After that, no one can read the value back, not you and not msreview:
GitHub gives it only to the repository's workflow runs. Anyone who can push a
workflow to the repository can use its secrets, so enable repositories where you
trust the people with write access.

### Managing enabled repositories

```bash
msreview status                     # every repository that the App can see (needs the App key)
msreview status you/repo1           # one repository
msreview update --all               # pin every enabled repository to this msreview's release
msreview update you/repo1 --pr      # one repository, through a pull request
msreview disable you/repo1          # remove the workflow, msreview's secrets and the variable
msreview disable you/repo1 --keep-secrets
```

`msreview status` changes nothing. For each repository, it shows whether the workflow
file exists and which release it pins, whether the two secrets and the variable exist,
and the conclusion of the last review run. GitHub returns only the names of secrets,
never their values. `msreview update` changes only the pinned release: it keeps a
preset that you chose with `--preset` and edits that you made to the file.
`msreview disable` deletes only the `MSREVIEW_` names; with `--pr`, nothing is removed
until you merge the pull request, and a second run prints
`workflow removal: already proposed in <url>`.

**Workflows from 0.1.x** map `secrets.MISTRAL_API_KEY`, and they keep working:
`msreview update` re-pins them and leaves that line as it is. `msreview status` shows
such a repository's key as `legacy secret name MISTRAL_API_KEY`. To move a repository
to msreview's own name:

```bash
msreview enable --secrets-only you/repo1 --app-key-file ~/Downloads/<app>.private-key.pem
msreview update you/repo1 --force   # re-renders the file (it keeps the preset, not other edits)
```

msreview does not delete the old `MISTRAL_API_KEY`: remove it yourself when no other
workflow uses it.

**Workflows from before 0.3.0** do not run on comments, so a `/msreview dismiss`
comment takes effect only at the next review. `msreview status` and `msreview update`
say so; `msreview update you/repo1 --force` re-renders the file with the comment
trigger (it keeps the preset, not other edits).

**Key rotation.** Generate a second private key on the App's page (an App can have
several active keys), or create a new Mistral key. Then set the new values in your
repositories, and delete or revoke the old key:

```bash
msreview enable --secrets-only you/repo1 you/repo2 --app-key-file ~/Downloads/<app>.private-key.pem
```

This asks for the Mistral key again: enter the new one, or the current one if you
rotate only the App key. There is no downtime, because GitHub accepts every active key
of the App. A 0.1.x workflow that still maps `secrets.MISTRAL_API_KEY` does not read
the new `MSREVIEW_MISTRAL_API_KEY`: move it first (see above).

### GitHub CLI scopes

msreview makes every change through `gh` with your login (`gh api`,
`gh secret set`, `gh variable set`), and passes secret values on stdin, never on the
command line. Only the check of where the App is installed is made as the App, with
its private key, because GitHub does not show App installations to a `gh` login. Your
login needs two scopes:

- `repo`: read the repositories, write the workflow file, and set secrets and variables;
- `workflow`: GitHub refuses changes to files under `.github/workflows/` without it.

`gh auth status` lists the scopes of your login. To add a missing one:

```bash
gh auth refresh -h github.com -s workflow
```

### Manual setup (without `msreview enable`)

You can also do the same steps yourself, for example to use organisation-level secrets
for the repositories of an organisation.

1. Create and install the GitHub App as in steps 1 and 2 above.
2. In each repository (Settings → Secrets and variables → Actions), or once for the
   organisation, add:
   - Secret `MSREVIEW_MISTRAL_API_KEY`: your Mistral API key.
   - Secret `MSREVIEW_APP_PRIVATE_KEY`: the full contents of the App's `.pem` file.
   - Variable `MSREVIEW_APP_CLIENT_ID`: the App's Client ID.

   The `gh` commands for this are in
   [docs/setup-github-app.md](docs/setup-github-app.md#manual-setup-with-gh).
3. Copy the workflow
   [`examples/workflows/morning-star-code-review.yml`](examples/workflows/morning-star-code-review.yml)
   to `.github/workflows/` in your repository. It is made from the same template as the
   file that `msreview enable` writes, but it uses the release tag. Pin the action to the
   full commit SHA of the release that you reviewed.
4. Open a pull request.

The workflow does not need `actions/checkout`. If the App credentials are missing or
the App is not installed, the run does not fail: it produces a report (step summary
and artifact) that tells you why nothing was posted.

### Pull requests from forks

Pull requests from forks get no secrets on `pull_request`, so the standard workflow
skips them. `msreview enable` installs only the standard workflow. To review fork PRs,
add the hardened `pull_request_target` template
[`examples/workflows/morning-star-code-review-forks.yml`](examples/workflows/morning-star-code-review-forks.yml)
yourself. It reviews automatically for trusted authors. For other authors, a maintainer
must add a label, and the approval applies to one head commit only. The job checks that
the person who added the label has write, maintain or admin access, because the triage
role can add labels too. Read [docs/security.md](docs/security.md) before you use it.

### Planned: a mode where you only install the App

A hosted mode is planned in which you only install a GitHub App: no workflow file
and no secrets in your repositories. It is not available yet, and it has no date.
Until then, reviews run only in your own GitHub Actions jobs or on your own machine.

## Local reviews (CLI)

The CLI reviews one pull request on demand. Requires Python 3.12 to 3.14, `git` and
[uv](https://docs.astral.sh/uv/), on macOS or Linux.

```bash
uv tool install git+https://github.com/ThomsenDrake/morning-star-code-reviews@v0.3.0
msreview setup                                       # builds the agent environment (pinned, hash-checked)
read -rs MISTRAL_API_KEY && export MISTRAL_API_KEY   # paste the key; it is not shown or kept in history
msreview doctor                                      # checks the key, Workflows access and the installation
msreview review https://github.com/OWNER/REPO/pull/123
```

The CLI reads the Mistral key from the `MISTRAL_API_KEY` environment variable of your
shell, and does not save it. Without App credentials, the CLI only reports: it prints
the review as Markdown and saves `report.md`, `findings.json` and `run.json` under
`./msreview-reports/`. Use `--read-token gh` to review private repositories in this
mode.

You do not need the App's private key on your computer: the workflow posts the reviews
of the repositories that you enabled. If you also want the CLI to post as your App,
see [docs/cli.md](docs/cli.md#posting-as-your-app). All commands and flags are in
[docs/cli.md](docs/cli.md).

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
  requests tokens with Contents: read. When the flagged code changes (or a
  `/msreview dismiss` command dismisses the finding), the bot replies on the thread and
  hides its comment as resolved. You can resolve the thread yourself.
- **Every PR comment starts a workflow run.** GitHub cannot filter `issue_comment`
  events by text, so each comment starts a run of the review workflow; its job is
  skipped unless the comment holds `/msreview dismiss` by an owner, member or
  collaborator (the bot's own summary edits start skipped runs too). Skipped runs
  cost no runner minutes.
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
| [docs/setup-github-app.md](docs/setup-github-app.md) | Creating and installing the GitHub App, enabling repositories, key rotation |
| [docs/configuration.md](docs/configuration.md) | What `msreview enable` writes, repository file, Action inputs and outputs, CLI flags, environment variables, precedence |
| [docs/cli.md](docs/cli.md) | `msreview` commands (`enable`, `status`, `update`, `disable`, `review`, …), user config file, exit codes |
| [docs/models.md](docs/models.md) | Models, efforts, presets, evaluation method and results |
| [docs/security.md](docs/security.md) | Threat model, fork template, residual risks |
| [docs/operations.md](docs/operations.md) | Deployments, costs, telemetry, troubleshooting |
| [docs/architecture.md](docs/architecture.md) | How a review runs, step by step |
| [CHANGELOG.md](CHANGELOG.md) · [SECURITY.md](SECURITY.md) | Releases · reporting vulnerabilities |
