# The `msreview` CLI

The CLI has two jobs:

- **Enable repositories.** `msreview enable`, `status`, `update` and `disable` add the
  review workflow and its secrets to your repositories, and keep them up to date (see
  [Enabling repositories](#enabling-repositories)).
- **Review one pull request on demand.** `msreview review` uses the same code path as
  the GitHub Action. It posts as your GitHub App when the App credentials are set and
  the App is installed. Otherwise it prints a report.

## Install

Requirements: Python 3.12 to 3.14, `git`, and [uv](https://docs.astral.sh/uv/), on
macOS or Linux.

```bash
uv tool install git+https://github.com/ThomsenDrake/morning-star-code-reviews
# or a specific release:
uv tool install git+https://github.com/ThomsenDrake/morning-star-code-reviews@v0.3.0

msreview setup      # only for local reviews
```

The package is not on PyPI. To enable repositories, you need only `uv tool install`
and the GitHub CLI `gh`. For local reviews, installation creates **two environments**:

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

## Enabling repositories

Four commands set up and maintain the GitHub Action in your repositories: `enable`,
`status`, `update` and `disable`. They do not need the agent environment
(`msreview setup`).

They make every change through the [GitHub CLI](https://cli.github.com/) `gh`
(`gh api`, `gh secret set`, `gh variable set`) with your own login. Only the checks of
where the App is installed are made as the App, with its private key (GitHub does not
show App installations to a `gh` login). Before they change anything, they check
`gh auth status`. Your login needs the scopes `repo` and
`workflow`; GitHub refuses changes to files under `.github/workflows/` without
`workflow`. (`msreview status` and `msreview enable --secrets-only` need only `repo`.)
To add a missing scope:

```bash
gh auth refresh -h github.com -s workflow
```

When `GH_TOKEN` or `GITHUB_TOKEN` is set, `gh` uses that token instead of your login,
and that token needs the same scopes.

msreview passes secret values to `gh` on stdin, never on the command line, so they do
not appear in the process list or in your shell history. `gh` encrypts each value with
the repository's public key before it is uploaded. msreview writes no key to disk.

### `msreview enable`

```
msreview enable OWNER/REPO [OWNER/REPO ...] [--app-key-file PATH] [--client-id ID]
                [--preset NAME] [--pr | --direct] [--secrets-only | --no-secrets]
                [--force] [--version vX.Y.Z] [--verify | --no-verify]
```

Sets up the review workflow in one or more repositories.

```bash
msreview enable you/repo1 you/repo2 --app-key-file ~/Downloads/<app>.private-key.pem
msreview enable you/repo3 --app-key-file app.pem --preset balanced --pr
msreview enable --secrets-only you/repo1 you/repo2 --app-key-file new.pem   # key rotation
```

**1. It collects the values once**, before it changes any repository:

| Value | Where it comes from |
|---|---|
| Mistral API key | The environment variable `MSREVIEW_ENABLE_MISTRAL_API_KEY`, else a hidden prompt. Not needed with `--no-secrets` |
| App Client ID | `--client-id`, else `MSREVIEW_ENABLE_APP_CLIENT_ID`, else the Client ID that the CLI uses for posting (`MSREVIEW_APP_CLIENT_ID` or `[app] client_id` in the user config file), else a prompt (shown as you type; the Client ID is not a secret) |
| App private key | The file in `--app-key-file`, read once, else `MSREVIEW_ENABLE_APP_PRIVATE_KEY` (the PEM text). Optional with `--no-secrets` |

Without a terminal (for example in a script), there are no prompts: set the
environment variables instead.

msreview checks the formats: the private key must be an unencrypted RSA key in PEM
format, the Client ID must look like one (`Iv…`, not the numeric App ID), and the
Mistral key must not be empty or contain spaces. It warns when
the key file can be read by group or others. It does not delete the file: delete it
yourself when you are done. Unless you pass `--no-verify`, it also checks the keys:

- the Mistral key, with a read-only call to the Workflows API (no worker is started);
- the App key and Client ID, by asking GitHub for the App's details (this also gives
  the App's slug).

The values are kept in memory only, and are dropped after use.

**2. For each repository**, it:

1. Checks that the repository exists and that you can reach it.
2. Checks that the App is installed on the repository. It asks GitHub as the App,
   with a token signed by the App's private key: GitHub does not show App
   installations to a `gh` login. If the App is not installed there, msreview prints
   a link where you can add the repository, and skips it: the installation's settings
   (`https://github.com/settings/installations/<id>`), or the App's install page when
   the App is not installed on that account at all. `--force` continues anyway; the
   reviews are then reports until you install the App. With `--no-secrets` and no App
   key, this check is not made (and no Client ID is needed).
3. Compares the workflow file that it would write with the file at
   `.github/workflows/morning-star-code-review.yml`. The new file comes from the
   template that is packaged with msreview. It pins the action to the full commit SHA
   of the release (`--version`, default: the version of the installed msreview), with
   a `# vX.Y.Z` comment, and sets `timeout-minutes: 110`. With `--preset`, it sets the
   `preset` input.
   - The same file is already there: it is not committed again.
   - A different file is there: msreview shows a short summary of the differences,
     changes nothing in this repository, and counts it as failed. `--force` replaces
     the file. To move an existing file to a new release, use `msreview update`.
   - `--force --no-secrets` on a workflow from 0.1.x (which maps
     `secrets.MISTRAL_API_KEY`) moves it to `MSREVIEW_MISTRAL_API_KEY`; if that secret
     is not set, msreview warns that reviews fail until you set it.
4. Sets the secrets `MSREVIEW_MISTRAL_API_KEY` and `MSREVIEW_APP_PRIVATE_KEY`, and the
   variable `MSREVIEW_APP_CLIENT_ID`. Values that already exist are replaced. The
   secrets are set before the workflow file, so the workflow never runs without them.
   msreview sets, replaces and deletes only these `MSREVIEW_` names. A secret named
   `MISTRAL_API_KEY` (for example, one that another workflow uses) is never set,
   overwritten or deleted; if the repository has one, msreview prints a note that it
   was left untouched.
5. Commits the workflow file directly to the default branch, with the message
   "Enable Morning Star Code Review". With `--pr`, msreview points the branch
   `msreview/enable` at the default branch, commits the file there, and opens a pull
   request (or updates the one that is open). Do not push your own work to that
   branch: msreview resets it each time. That pull request is itself reviewed by the
   workflow it adds: GitHub runs `pull_request` workflows from the pull request's
   branch, and the secrets are already set.
6. Prints one result line for the repository, and notes on the lines below it, for
   example:

   ```
   you/repo1: enabled: secrets and variable set; workflow v0.3.0: committed 1a2b3c4 to main
     the existing secret MISTRAL_API_KEY is left untouched (msreview uses MSREVIEW_MISTRAL_API_KEY)
   ```

A failing or skipped repository does not stop the others. When any repository failed
or was skipped, the exit code is 1.

| Flag | Meaning |
|---|---|
| `--app-key-file PATH` | The App's private key (`.pem`). Read once |
| `--client-id ID` | The App's Client ID (`Iv23li…`). Without it, see the table above |
| `--preset NAME` | Write `preset: NAME` into the workflow: `best`, `balanced`, `single`, `fast` or `mistral`. Without it, the `preset` line stays a comment: the repository file `.morning-star-review.yml` chooses the preset, else `best` |
| `--direct` | Commit the workflow file directly to the default branch (the default) |
| `--pr` | Open a pull request from the branch `msreview/enable` instead |
| `--secrets-only` | Set only the secrets and the variable; do not touch the workflow file. Use it to rotate keys. Needs only the `repo` scope |
| `--no-secrets` | Write only the workflow file; do not touch the secrets and the variable |
| `--force` | Replace a different workflow file, and go on where the App is not installed |
| `--version vX.Y.Z` | The release to pin. Default: the version of the installed msreview |
| `--verify`, `--no-verify` | Check the keys online before any repository is changed (the default), or check only their formats |

**Key rotation.** Generate a second private key on the App's page, or create a new
Mistral key. Run `msreview enable --secrets-only` for your repositories (it asks for
the Mistral key again: enter the new one, or the current one if you rotate only the App
key). Then delete the old App key, or revoke the old Mistral key. GitHub accepts every
active key of an App, so there is no downtime. `msreview status` lists your
repositories. A workflow from 0.1.x that maps `secrets.MISTRAL_API_KEY` does not read
the new `MSREVIEW_MISTRAL_API_KEY`: move it with `msreview update --force` (see
[`msreview update`](#msreview-update)).

### `msreview status`

```
msreview status [OWNER/REPO ...] [--client-id ID] [--app-key-file PATH] [--json]
```

Shows how each repository is set up, and changes nothing. Without repository names,
it lists every repository that your App's installations can see. GitHub shows that
list only to the App, so msreview needs the App's Client ID (`--client-id`, else
`MSREVIEW_ENABLE_APP_CLIENT_ID`, else the Client ID that the CLI uses for posting) and
its private key (`--app-key-file`, else `MSREVIEW_ENABLE_APP_PRIVATE_KEY`, else the key
that the CLI uses for posting). With repository names, it needs no key.

For each repository, it shows:

- whether `.github/workflows/morning-star-code-review.yml` exists, which release it
  pins (from the `# vX.Y.Z` comment and the commit SHA), and the preset it sets;
- whether the secrets `MSREVIEW_MISTRAL_API_KEY` and `MSREVIEW_APP_PRIVATE_KEY` and
  the variable `MSREVIEW_APP_CLIENT_ID` exist. GitHub returns only the names of
  secrets: no one can read their values back. `unknown` means that your login cannot
  list them;
- the conclusion and the date of the last review run of the workflow (runs that PR
  comments start are not counted).

```
you/repo1
  workflow  v0.3.0 (1a2b3c4), preset balanced
  secrets   MSREVIEW_MISTRAL_API_KEY set, MSREVIEW_APP_PRIVATE_KEY set
  variable  MSREVIEW_APP_CLIENT_ID set
  last run  success (2026-11-21)
```

A workflow file from 0.1.x maps `secrets.MISTRAL_API_KEY`. For such a repository,
status checks that name instead, shows it as `legacy secret name MISTRAL_API_KEY`, and
adds a `hint` line that says how to move to `MSREVIEW_MISTRAL_API_KEY`:

```
you/repo2
  workflow  v0.1.1 (89abcde)
  secrets   legacy secret name MISTRAL_API_KEY set, MSREVIEW_APP_PRIVATE_KEY set
  variable  MSREVIEW_APP_CLIENT_ID set
  last run  success (2026-11-20)
  hint      the workflow uses the secret MISTRAL_API_KEY (the 0.1.x name); to move to MSREVIEW_MISTRAL_API_KEY, run `msreview enable you/repo2 --secrets-only`, then `msreview update you/repo2 --force` (it re-renders the file)
```

A workflow file from before 0.3.0 does not run on PR comments, so `/msreview dismiss`
takes effect only at the next review. Status adds a `hint` line for it too:
`` the workflow does not run on comments, so `/msreview dismiss` takes effect at the
next review only; `msreview update you/repo3 --force` adds it (it re-renders the file) ``.

`--json` prints the same information as a JSON list (`legacy_secret` is
`"MISTRAL_API_KEY"` for such a repository, else `null`; `workflow.commands` says
whether the workflow runs on comments). The exit code is 1 when a repository could
not be read.

### `msreview update`

```
msreview update (OWNER/REPO ... | --all) [--version vX.Y.Z] [--pr] [--force]
                [--client-id ID] [--app-key-file PATH]
```

Pins the workflow file to a new release, in repositories that already have it. Name
the repositories, or pass `--all` for every repository of the App that has the
workflow (repositories without it are left out; `--all` needs the Client ID and the
private key, as for `msreview status`). `--version` is the release to pin (default: the version of the
installed msreview). A named repository without the workflow file fails: use
`msreview enable` for it.

- Only the pinned release changes. The rest of the file stays as it is, including a
  preset that was set with `msreview enable --preset` and edits that you made.
- `--force` writes the file again from the template of the installed msreview. The
  preset is still kept; other edits are not.
- A repository that is already on that release gets no commit.
- A workflow from 0.1.x maps `secrets.MISTRAL_API_KEY`. Without `--force`, that line
  stays as it is (the workflow keeps working), and msreview prints how to move to
  `MSREVIEW_MISTRAL_API_KEY`: first `msreview enable OWNER/REPO --secrets-only`, then
  `msreview update OWNER/REPO --force`. If you pass `--force` while
  `MSREVIEW_MISTRAL_API_KEY` is not set, msreview still writes the file and warns that
  reviews fail until you set it. msreview does not delete the old `MISTRAL_API_KEY`.
- A workflow from before 0.3.0 has no `issue_comment` trigger. Without `--force` it
  keeps it that way (the review works; `/msreview dismiss` takes effect at the next
  review), and msreview prints a hint. `--force` adds the trigger.

The change is committed to the default branch with the message "Update Morning Star
Code Review to vX.Y.Z", or with `--pr` through a pull request from the branch
`msreview/update`.

```bash
uv tool install --reinstall git+https://github.com/ThomsenDrake/morning-star-code-reviews@vX.Y.Z
msreview update --all
```

### `msreview disable`

```
msreview disable OWNER/REPO [OWNER/REPO ...] [--keep-secrets] [--pr]
```

Removes `.github/workflows/morning-star-code-review.yml` from each repository, and
deletes the secrets `MSREVIEW_MISTRAL_API_KEY` and `MSREVIEW_APP_PRIVATE_KEY` and the
variable `MSREVIEW_APP_CLIENT_ID`. It never deletes a secret named `MISTRAL_API_KEY`,
even when a 0.1.x workflow used it (other workflows may use it too); it then prints a
note, and you delete it yourself if nothing else needs it. With `--keep-secrets`, the
secrets and the variable stay, for example when another workflow uses them. The file
is removed with a commit "Disable Morning Star Code Review" on the default branch, or
with `--pr` through a pull request from the branch `msreview/disable`. With `--pr`,
the result line says `workflow removal: opened <url>`, and while that pull request is
open, a second run prints `workflow removal: already proposed in <url>` and makes no
new commit. The secrets and the variable are still deleted at once, so until you merge
the pull request, the workflow runs without them. The App stays installed: remove the
repository from the installation in the App's settings if you want that too.

## Local review commands

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
| `--no-labels`, `--no-suggestions` | Do not set status labels, or do not post suggestion blocks |
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
`msreview: <model> rate-limited (explore); waiting 60s (total waited 1m 45s)`,
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

## Posting as your App

This section is about local reviews only. The repositories that you enabled get their
reviews posted by the workflow, so you do not need the App's private key on your
computer. Without App credentials, `msreview review` produces a report.

If you also want the CLI to post as your App, generate a **separate** private key on
the App's page for this computer, so that you can revoke it on its own. Then give the
CLI the Client ID and one of these:

```bash
export MSREVIEW_APP_CLIENT_ID=Iv23li...

# A command that prints the PEM, for example from a password manager (nothing on disk)
export MSREVIEW_APP_PRIVATE_KEY_COMMAND='<a command that prints the key>'

# Or a file that only you can read (msreview refuses keys that group or others can read)
mkdir -p ~/.config/morning-star-review
install -m 600 path/to/app.private-key.pem ~/.config/morning-star-review/app.pem
export MSREVIEW_APP_PRIVATE_KEY_PATH=~/.config/morning-star-review/app.pem
```

```bash
msreview app-check OWNER/REPO                         # is the App installed, with which permissions?
msreview review OWNER/REPO#123                        # posts as <your-app>[bot]
msreview review OWNER/REPO#123 --post never --json    # report only, JSON outcome
```

Only a short-lived child process reads the App key. It signs a JWT, mints an
installation token for the one repository under review, and exits. The key is never
passed to the review process, the Workflows worker or the agents. The same settings
can go into the user config file (below).

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
python = "~/.cache/morning-star-review/agent-0.3.0/bin/python"   # MSREVIEW_AGENT_PYTHON
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

### `msreview review`

| Code | Meaning |
|---|---|
| 0 | Posted, report, skipped or superseded, and `--fail-on` was not triggered |
| 1 | The review failed (see the reason in the output) |
| 2 | Usage, configuration or preflight error (bad flags, no key, a key refused by the Workflows API, missing agent environment) |
| 3 | The `--fail-on` threshold was met |
| 130 | Cancelled (Ctrl-C or SIGTERM). The workflow execution is terminated and the worker is stopped first |

### `msreview enable`, `status`, `update` and `disable`

| Code | Meaning |
|---|---|
| 0 | Every repository succeeded (an unchanged repository counts as a success) |
| 1 | At least one repository failed or was skipped (for `status`: could not be read). The other repositories were still processed; the result lines say which ones need attention |
| 2 | Nothing was changed: usage error, `gh` missing or not logged in, a missing scope, no Client ID, or a value that failed its format or online check |
