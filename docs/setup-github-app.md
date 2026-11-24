# Setting up the GitHub App

Morning Star Code Reviews posts as a GitHub App that **you** create and own. Your
reviews appear as `<your-app-slug>[bot]`. You choose the permissions, and no hosted
service holds your credentials. One App can serve every repository of an account or
organisation.

You need the App to post reviews. Without it, every run is a report: the review is
still produced, but only as the Action's step summary and artifact, or as CLI output.

## 1. Create the App

1. Open **Settings → Developer settings → GitHub Apps → New GitHub App**.
   - For a personal account: <https://github.com/settings/apps/new>.
   - For an organisation: **Organisation settings → Developer settings → GitHub Apps**.
2. Fill in the form:

   | Field | Value |
   |---|---|
   | GitHub App name | Any name, for example "Acme Code Review". The slug becomes the bot login (`acme-code-review[bot]`). |
   | Homepage URL | Any URL, for example your repository |
   | Callback URL, Setup URL | Leave empty |
   | Request user authorization (OAuth) during installation | Off |
   | **Webhook → Active** | **Off** (clear the box). The Action and the CLI call GitHub themselves, so the App needs no webhook. |
   | Where can this GitHub App be installed? | "Only on this account" (recommended) |

3. Under **Permissions → Repository permissions**, set exactly these:

   | Permission | Level | Why |
   |---|---|---|
   | **Contents** | Read-only | Fetch the PR's commits and read the repository config file at the base commit |
   | **Pull requests** | Read and write | Read the PR; post the review, its inline comments, replies and the summary comment |
   | **Issues** | Read and write | Create the `msreview: …` status labels and set them on the PR |
   | **Metadata** | Read-only | Mandatory for every App |

   Leave everything else at "No access". Do not subscribe to any events. Do not grant
   any organisation or account permissions.

4. Click **Create GitHub App**.
5. On the App's page, note the **Client ID** (it looks like `Iv23li…`). msreview
   uses the client ID, not the numeric App ID.
6. Under **Private keys**, click **Generate a private key**. Your browser downloads a
   `.pem` file. Keep it secret: `msreview enable` reads it once (step 3), and then you
   delete it. You can generate more keys and delete old ones at any time, so rotation
   does not need downtime (step 4).

### Why not Contents: write?

GitHub resolves a review thread for an integration only if its token has
**Contents: write**. Pull requests: write alone is refused with "Resource not
accessible by integration". Contents: write also lets the token push commits, so we
do not recommend it for a reviewer. msreview always requests tokens with Contents:
read, even if the App was granted more. When the flagged code of an earlier finding
changes, the bot replies "Looks addressed in `<sha>`" and hides its own comment as
resolved. The thread stays open until a person resolves it.

### Without Issues: write

The status labels need Issues: write. If the App does not have it:

- **CLI:** labels are skipped, and the outcome records a note. `msreview app-check`
  shows it.
- **Action:** set the input `labels: "false"` in the workflow file. Otherwise the
  token steps request a permission that the installation does not have, and GitHub
  refuses the token. The run then falls back to report mode. `msreview update` keeps
  this edit.

## 2. Install the App

On the App's page, click **Install App**. Choose the account, then either **All
repositories** or **Only select repositories**. You can change this list later in the
installation's settings.

`msreview enable` checks that the App is installed on each repository. If it is not,
it prints a link where you can add the repository, and skips it. If a workflow runs in
a repository that is not in the list, the reviewer does not fail. It reports
"Not posted: the GitHub App '<slug>' is not installed on <owner>/<repo>".

## 3. Enable your repositories

`msreview enable` gives the credentials to each repository and adds the review
workflow. You need the [GitHub CLI](https://cli.github.com/) `gh`, logged in with the
scopes `repo` and `workflow` (`gh auth refresh -h github.com -s workflow` adds a
missing scope).

```bash
uv tool install git+https://github.com/ThomsenDrake/morning-star-code-reviews@v0.3.0
msreview enable you/repo1 you/repo2 --app-key-file ~/Downloads/<app>.private-key.pem
```

It asks for the Mistral API key (hidden) and the App's Client ID once, checks them,
and then, for each repository:

- checks that the App is installed there. If it is not, it prints a link where you
  can add the repository, and skips it;
- sets these secrets and this variable:

  | Kind | Name | Value |
  |---|---|---|
  | Secret | `MSREVIEW_MISTRAL_API_KEY` | A Mistral API key for a workspace with Workflows access |
  | Secret | `MSREVIEW_APP_PRIVATE_KEY` | The full contents of the `.pem` file |
  | Variable | `MSREVIEW_APP_CLIENT_ID` | The App's Client ID |

  The `MSREVIEW_` prefix keeps these apart from the repository's own secrets. A secret
  named `MISTRAL_API_KEY` that another workflow uses is never set, overwritten or
  deleted; if the repository has one, msreview prints a note that it was left
  untouched;
- commits `.github/workflows/morning-star-code-review.yml` to the default branch
  (`--pr`: through a pull request). The action is pinned to the full commit SHA of the
  release. The pull request that `--pr` opens is itself reviewed by the workflow it
  adds (GitHub runs `pull_request` workflows from the pull request's branch, and the
  secrets are already set), which is a first end-to-end check of the setup.

The keys are stored only in the repositories' GitHub Actions secrets. Personal
accounts have no account-wide Actions secrets, so each repository gets its own copy.
msreview stores nothing on your computer. When it is done, **delete the `.pem` file**.
All flags are in [cli.md](cli.md#msreview-enable).

In the workflow, the App key is used only by the two `actions/create-github-app-token`
steps of the Action. Each mints a one-hour installation token for the current
repository only: a read-only token for the review, and, after the review, the token
that posts it (Contents: read, Pull requests: write, Issues: write).

### Manual setup with `gh`

If you do not use `msreview enable`, set the values for each repository yourself
(Settings → Secrets and variables → Actions), or once for an organisation. With the
GitHub CLI:

```bash
gh secret set MSREVIEW_MISTRAL_API_KEY --repo OWNER/REPO   # paste the key when prompted
gh secret set MSREVIEW_APP_PRIVATE_KEY --repo OWNER/REPO < path/to/app.private-key.pem
gh variable set MSREVIEW_APP_CLIENT_ID --repo OWNER/REPO --body "Iv23li..."
```

Then copy [`examples/workflows/morning-star-code-review.yml`](../examples/workflows/morning-star-code-review.yml)
into `.github/workflows/`. The example uses the release tag: pin the action to the full
commit SHA of the release that you reviewed.

## 4. Rotate the keys

An App can have several active private keys, so you can rotate without downtime:

1. On the App's page, under **Private keys**, generate a new key. For the Mistral key,
   create a new key in your Mistral workspace.
2. Set the new values in every enabled repository (`msreview status --app-key-file
   <new>.pem` lists them):

   ```bash
   msreview enable --secrets-only you/repo1 you/repo2 --app-key-file ~/Downloads/<app>.private-key.pem
   ```

   It asks for the Mistral key again: enter the new key, or the current one if you
   rotate only the App key. A workflow from 0.1.x that still maps
   `secrets.MISTRAL_API_KEY` does not read the new `MSREVIEW_MISTRAL_API_KEY`:
   `msreview status` shows it as `legacy secret name MISTRAL_API_KEY`. Move it with
   `msreview update OWNER/REPO --force` after this step, or rotate its
   `MISTRAL_API_KEY` yourself.
3. Delete the old App key on the App's page, or revoke the old Mistral key. Delete the
   new `.pem` file.

## 5. Post from the CLI (optional)

The CLI needs the App only to post the reviews that you start on your computer.
Without it, `msreview review` produces a report, and the enabled repositories still get
their reviews posted by the workflow. If you want the CLI to post, generate a separate
private key for this use and follow [cli.md](cli.md#posting-as-your-app). Only a
short-lived child process reads that key. The key never reaches the review process,
the Workflows worker or the agents.

## 6. Check the setup

```bash
msreview status OWNER/REPO      # the workflow, secrets, variable and last run of a repository
msreview app-check OWNER/REPO   # is the App installed, with which permissions?
```

Example output of `msreview app-check`:

```
The GitHub App 'acme-code-review' is installed on OWNER/REPO.
  contents: read
  issues: write
  metadata: read
  pull_requests: write
```

`msreview app-check` exits 1 when the App is not installed or when Pull requests: write
is missing. `msreview doctor` also checks the App settings of the CLI, if you set them
(step 5).
