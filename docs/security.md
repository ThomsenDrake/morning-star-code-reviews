# Security model

This document explains what the reviewer protects, what it trusts, and which risks
remain. To report a vulnerability, see [SECURITY.md](../SECURITY.md).

## What runs where

| Component | Holds | Talks to |
|---|---|---|
| Token steps (Action) or the `msreview _mint` child process (CLI) | The GitHub App private key, and briefly a signed JWT | `api.github.com` |
| Driver and Workflows worker (one process: `msreview ci` / `msreview review`) | The Mistral API key, the installation token (in memory; read-only in the Action), a per-run seal key | `api.mistral.ai`, `api.github.com`, `github.com` (git fetch) |
| Publish step (Action: `msreview ci-publish`) | The write installation token (in memory) and the publish plan | `api.github.com` |
| `git` child processes | The installation token, only as a per-command header for `fetch` | `github.com` |
| Agent processes (explorer, verifier, formatter), in the agent environment | The Mistral API key only | `api.mistral.ai` |

Pull request content (title, description, diff, files, comments) is **untrusted
input**. It goes into prompts, the local run directory and the workflow payloads.
It is **never executed**: nothing is installed, built, tested or run from the PR.

## Secrets

- **The GitHub App private key** exists only in the Action's two token steps, or in the
  CLI's short-lived mint child. That child signs a JWT, mints an installation token for the
  one repository under review, and exits. The key never reaches the driver, the
  worker, the agents, payloads or logs. A key file must have mode 0600. A key command
  runs only inside the mint child.
- **The installation token** expires within an hour. It is limited to one repository,
  with Contents: read, Pull requests: write and (for labels) Issues: write. In the
  Action, the review step only gets a read-only token, and the write token is minted
  after the review (see [GitHub App tokens](#github-app-tokens)). Tokens are kept in
  memory. `msreview ci` and `msreview ci-publish` remove them from their environment at
  start-up. A token is passed to `git` only for the fetch command, and never to the
  agents.
- **The Mistral API key** is in the worker's environment, because the Workflows SDK
  needs it there, and in the agents' environment. Every child process gets an
  environment built from an allowlist, never a copy of the parent's. Variables that
  could redirect the key to another endpoint are never passed on.
- **Logs and errors** are redacted: known secret values, `Bearer` and token headers,
  GitHub token patterns, PEM blocks and credentials in URLs. While a review runs in
  Actions, workflow commands are turned off, so model text cannot inject `::` commands
  into the log. The report Markdown goes to the step summary and the artifact, never to
  the log.
- **Payloads carry no secrets.** Tokens and keys stay in the worker's memory (the run
  context), never in workflow inputs or outputs. Tests check that they do not appear in
  outcomes or in child-process environments.

## GitHub App tokens

An installation token expires after 60 minutes. A review can take longer than that,
because rate limits make it slower instead of failing it (up to `deadline-minutes`,
at most 300). So the token that posts is always minted shortly before it is used:

- **Action.** With App credentials, and `post` not set to `never`, the Action mints two
  tokens with the same pinned `actions/create-github-app-token`:
  1. Before the review, a **read-only** token: Contents: read, Pull requests: read,
     and Issues: read when labels are on. The review step (`msreview ci`) uses it to
     fetch the PR and to read the previous review. This step cannot write to GitHub.
  2. After the review, a **write** token: Contents: read, Pull requests: write, and
     Issues: write when labels are on. The publish step (`msreview ci-publish`) posts
     the review, or the failure notice, with it. These two steps also run when the
     review step failed.

  The App's private key is an input of the two token steps only. Without App
  credentials, or with `post: never`, there are no token steps and no publish step.
- **The publish plan.** The review step gives its result to the publish step in a
  file in the runner's temporary directory: the review to post, or the failure to
  report. The file is not sealed, because the seal key exists only in the review
  process. It has the same trust level as the `outcome.json` that the review step
  writes: msreview writes both on the same runner, in the same job, and no PR code
  runs on that runner. The file is not uploaded with the artifact. Before it posts, the
  publish step checks that the write token belongs to the bot that the review was made
  for, and that the PR head did not move during the review. If either check fails, it
  makes a report instead of posting.
- **CLI.** The mint child mints the token before the review. Before posting, the CLI
  starts the mint child again when the token is older than 45 minutes, so the private
  key still never reaches the review process. If that fails, the CLI posts with the old
  token while it is still valid, and otherwise makes a report with the reason.

## Threats and mitigations

| Threat | Mitigation |
|---|---|
| **Prompt injection** in code, comments or the PR description tries to steer the agents | All PR content is wrapped in untrusted blocks with a random boundary for each run. The prompts state that this data is not instructions. The agents have only read-only tools, with no network, shell or write access. Every model output is sanitised before it is posted. The verdict, the coverage, the thread handling and the placement of comments are decided by deterministic code, not by model claims. The evaluation includes an injection case. |
| **An agent tries to read secrets from disk** | Tools are confined to the checkout by resolving real paths. `.git/`, absolute paths, `..` and symlinks that escape the checkout are refused. The checkout contains only fetched tree content. The agent's home and temporary directories are empty scratch directories. |
| **PR code runs during the review** | Nothing from the checkout is executed. Git runs with hooks disabled, with no textconv or external diff, no LFS filters, no submodule recursion, and with system and global git config ignored. |
| **Spam, phishing or pings through bot comments** | Mentions, issue references and links to hosts other than github.com are neutralised. HTML is escaped, images become plain links, and length is capped. Hidden markers cannot be forged: marker text is stripped from model output, and state is trusted only from comments by your App's bot. |
| **Malicious suggestion blocks** | Suggestions are posted only after validation: a small range in one hunk, a real change, the same indentation, and a file that still parses (Python, JSON, YAML, TOML) or keeps a balanced bracket count. No marker and no line that would close the fence is allowed. Code inside the fence is kept as written: GitHub links no mentions, references or URLs inside a code block. A suggestion is never applied automatically: a person must commit it. |
| **Someone dismisses findings without permission** | Resolving a thread dismisses a finding only when the person has write access or more. `review.author_may_dismiss: false` excludes the PR author. A resolve by anyone else is recorded as "resolved without a fix", and the finding stays open. |
| **Foreign tasks on your Workflows task queue** (deployment names are predictable) | Each activity checks that the task belongs to this run's execution and request. Payloads between activities carry an HMAC seal with a key that exists only in the worker's memory. `publish` posts only sealed content. Execution ids have a random suffix. |
| **A `.env` file or environment variables redirect the SDK** | The driver moves to an empty directory before the Workflows SDK is imported. Variables that break or redirect its configuration are removed. A non-default `SERVER_URL` is refused unless you allow it explicitly. |
| **A repository configures expensive models or large budgets** | The repository file is read from the base commit only, and it can only lower budgets. In the CLI, the file's model and budget settings, and the settings that steer the review (`exclude`, `review.instructions`, `review.path_instructions`, `review.inline_min_severity`), are ignored for every repository unless you pass `--trust-repo-config`. |
| **Cost abuse** (huge PRs, or injected text asking the agents to read everything) | A hard USD cap for each run is checked before every model call. Each agent also has limits on tool calls, time and context size. Very large PRs are capped at 20,000 changed lines and marked partial. Commits that were already reviewed are skipped. Concurrency settings cancel superseded runs. |
| **Supply chain** | The third-party actions in `action.yml` and in this project's own workflows are pinned by full commit SHA. The example workflows refer to this action by a release tag, with a comment that asks you to pin the full commit SHA instead. The Action installs from `uv.lock` with `--frozen`. `msreview setup` installs the agent dependencies with `--require-hashes`. The Mistral packages are pinned exactly. |

## Data that leaves your machine or runner

- **To `api.mistral.ai`:** the prompts (PR metadata, diff excerpts, file contents
  that the agents read, related definitions) and the model outputs.
- **Into your Workflows execution history** (visible to members of your Mistral
  workspace in Studio): PR metadata, scope, the agents' notes (capped), findings with
  their evidence lines, and the final report. Large data such as the checkout and full
  diffs stays in the run directory on the runner or your machine.
- **Telemetry is off by default.** See [operations.md](operations.md#telemetry).

We recommend a **dedicated Mistral workspace** for code review: its members can read
the execution history, and you can set a spending limit for the workspace.

## Public repositories

Comments on public PRs are public. This includes the summary's "Run details" block
(models, tokens and cost). Set `summary.show_usage: false` to hide it. Report mode
posts nothing.

## Pull requests from forks

GitHub does not give secrets to `pull_request` runs from forks. The standard example
workflow therefore skips fork PRs. There are two safe ways to review them:

1. **On demand with the CLI:** `msreview review <PR URL>`. A maintainer decides
   when to run it. The fork's head is fetched from the base repository
   (`refs/pull/<n>/head`).
2. **The hardened template**
   [`examples/workflows/morning-star-code-review-forks.yml`](../examples/workflows/morning-star-code-review-forks.yml)
   on `pull_request_target`.

### How the fork template stays safe

`pull_request_target` runs with the base repository's secrets. This is why fork
workflows are often dangerous. The template follows these rules:

- **It never checks out or runs PR code.** There is no `actions/checkout`. The
  reviewer fetches the PR head into a temporary directory and only reads it. The
  agents have read-only tools and no network.
- **The workflow token is read-only.** The workflow level has `permissions: {}`, and
  the review job gets only `contents: read` and `pull-requests: read`. The App
  token does all writes.
- **Only trusted authors are reviewed automatically:** `OWNER`, `MEMBER` or
  `COLLABORATOR`, and only when the PR comes from a fork that the author owns.
- **Other authors need a maintainer's approval for each push.** A maintainer reads the
  change, then adds the label `msreview: safe-to-review`. The review job passes
  `expected-head-sha` from the event, so the approval covers that one head commit. If
  the author pushes again before the review starts, the run is skipped as superseded.
  The label is removed after every review. A separate small job also removes it on every
  new push from an untrusted author, so each push needs a fresh approval.
- **The label counts only from someone with write access.** GitHub also lets the
  **triage** role add labels, so the label alone is not enough. When the job starts
  from a label, its first step uses the read-only workflow token to read the
  repository permission of the person who added the label
  (`GET /repos/{owner}/{repo}/collaborators/{username}/permission`, which needs only
  the Metadata: read permission that every token has). The job stops, before any
  secret is used, unless the permission is write or admin. GitHub reports the maintain
  role as write, the triage role as read, and a custom role as the role it is based on.
  An API error also stops the job.
- **The workflow file comes from the base branch.** On `pull_request_target`, a PR
  cannot change the workflow that reviews it.
- **Pin the action by commit.** Both example workflows refer to the action by a release
  tag (`@v0.1.1`), with a comment that asks you to pin it. Replace the tag with the full
  commit SHA of the release that you reviewed: a tag can be moved to other code, but a
  commit SHA always names the same code.

Anyone with write, maintain or admin access can start a paid review by adding the
label. When someone with less access (for example the triage role) adds it, the run
fails at the permission check and the label stays on the PR. Remove it; someone with
write access can add it again after reading the change.

### Residual risks of reviewing fork PRs

- **Steerable review text.** An attacker controls the code and the description that
  the models read. They can try to make the review miss real problems, report false
  ones, or say misleading things. Sanitising stops pings, links, markers and HTML, but
  it cannot make model judgement trustworthy. Treat a fork review as an extra opinion,
  not as a security gate.
- **Cost.** Each approved run can spend up to `max-cost-usd` (default $10). Trusted
  authors get a review on every push. Lower `max-cost-usd`, choose a cheaper preset for
  the fork workflow, or set a workspace spending limit.
- **Rate limits.** Reviews use your Mistral workspace's rate limits and the App's
  GitHub API quota. A flood of approved runs can slow other work in the same workspace.

## Other residual risks

- Findings can be wrong, and real problems can be missed. The reviewer never approves
  and never requests changes.
- Members of your Mistral workspace can read the execution history, which includes
  code excerpts.
- Each PR leaves about one inactive deployment record in the workspace. The API cannot
  delete these records.
- The agent environment includes a proprietary, compiled component from PyPI
  (`mistralai-vibe-harness`, a dependency of `mistralai-agents`). It runs with the
  Mistral key and the read-only checkout. See [`NOTICE`](../NOTICE).

## Recommendations

- Create the App with exactly the permissions in [setup-github-app.md](setup-github-app.md).
  Do **not** grant Contents: write.
- Pin the action to a full commit SHA that you reviewed, not to a tag. The example
  workflows use a tag only as a placeholder.
- Use a dedicated Mistral workspace with a spending limit.
- Keep `max-cost-usd` at a level you accept for one PR.
- Rotate the App's private key and the Mistral key from time to time. GitHub Apps can
  have two keys at once, so rotation needs no downtime.

## Hardening from the security review (v1)

This section lists what an adversarial review of v1 found, what the code now does, and
what stays a residual risk.

Fixed in code:

- **Sanitiser.** An empty Markdown link (`[]()`) is kept as escaped brackets, never
  deleted, so text on both sides cannot join into a live mention, link, HTML tag,
  heading, suggestion fence or marker. Every sanitised text is also checked: if it
  still holds a marker or changes when it is sanitised again, it is posted as code.
- **Commands found through `PATH`.** git, gh and uv are looked up only in the absolute
  entries of `PATH`, and child processes get only the absolute entries. A `bin/git`
  in a PR checkout never runs, even with `PATH=./bin:$PATH`.
- **The mint child** starts in Python's isolated mode (`-I`), so a `json.py` or other
  module in the current directory cannot run next to the App key command. It reads
  `[app]` from the same file as the rest of the settings (`MSREVIEW_CONFIG`).
- **Report files.** `msreview review` replaces files in the report directory atomically
  and never follows a symbolic link there. It refuses an `--out` that is a symbolic
  link.
- **`msreview setup`** runs uv with `--no-config`, so a `uv.toml` or `[tool.uv]` in the
  current directory cannot change the index.
- **The PR's `.gitattributes`** cannot hide a file from the local diff or `git grep`,
  or re-encode files on checkout: `.git/info/attributes` overrides it.
- **Exclusions.** Only snapshot data in `__snapshots__/` is excluded (code there stays
  reviewable). A generated-file marker does not move with a rename. A PR in which only
  lock files, generated files, minified files or non-media binaries changed gets the
  verdict `incomplete` ("nothing was reviewed"), not `clean`.
- **Untrusted repository files (CLI).** Without `--trust-repo-config`, the repository
  file cannot set `exclude`, `review.instructions`, `review.path_instructions` or
  `review.inline_min_severity`.
- **Terminal output.** Reports have no control characters: paths and code excerpts
  from the PR cannot send terminal escape sequences.
- **Size limits.** A head tree over 1,000,000 files or 4 GiB is not checked out. A PR
  whose file list would not fit a workflow payload fails at once with a clear reason
  (and the failure banner), instead of looping until the deadline. The list of
  excluded files in the prompt has a character budget.
- **Run lifecycle.** The driver trusts only the outcome that its own publish step wrote
  (not the workflow's result, which another worker on the task queue could forge).
  SIGHUP and SIGQUIT stop a run like SIGTERM does, and an agent exits when the process
  that started it is gone. The CI orphan sweep leaves executions with a live worker
  alone. When two runs for one PR start at the same time, the second worker moves to a
  deployment of its own, and a task that reaches the wrong worker is retried.
- **Token lifetime.** A run can last longer than an installation token (60 minutes).
  The Action mints the token that posts after the review, and the CLI mints a new one
  before posting when the first is old (see [GitHub App tokens](#github-app-tokens)).

Residual risks:

- **Fork approval timing.** The `msreview: safe-to-review` label approves the head
  commit at the moment the label is added. If the author pushes while you read, the
  label approves the new commit. Look at the head commit (the "Commits" tab) just
  before you add the label.
- **Triage role.** People with the triage role can add the label. They cannot start a
  review, because the permission check stops the run. But a new run of the review job
  cancels the one in progress for the same PR. So by removing and adding the label
  again, they can cancel an approved review that is running.
- **Trusted authors' forks.** Automatic fork reviews need a trusted author and a head
  repository that the author owns. If other people can push to that fork, their pushes
  are reviewed automatically too.
- **Concurrent runs.** Two runs for the same PR that start within seconds of each other
  can share a task queue for a moment before one moves away. A task that reaches the
  wrong worker in that moment is retried, which normally succeeds.
- **Build requirements.** `msreview setup` installs pinned, hashed dependencies, but
  building the project itself from git fetches its build backend (hatchling) from your
  configured index without hashes.
