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
   | **Issues** | Read and write | Create the `msreview: …` verdict labels and set them on the PR |
   | **Metadata** | Read-only | Mandatory for every App |

   Leave everything else at "No access". Do not subscribe to any events. Do not grant
   any organisation or account permissions.

4. Click **Create GitHub App**.
5. On the App's page, note the **Client ID** (it looks like `Iv23li…`). msreview
   uses the client ID, not the numeric App ID.
6. Under **Private keys**, click **Generate a private key**. Your browser downloads a
   `.pem` file. Keep it secret. You can create a second key and delete the first one at
   any time, so rotation does not need downtime.

### Why not Contents: write?

GitHub resolves a review thread for an integration only if its token has
**Contents: write**. Pull requests: write alone is refused with "Resource not
accessible by integration". Contents: write also lets the token push commits, so we
do not recommend it for a reviewer. msreview always requests tokens with Contents:
read, even if the App was granted more. When the flagged code of an earlier finding
changes, the bot replies "Looks addressed in `<sha>`" and hides its own comment as
resolved. The thread stays open until a person resolves it.

### Without Issues: write

The verdict labels need Issues: write. If the App does not have it:

- **CLI:** labels are skipped, and the outcome records a note. `msreview app-check`
  shows it.
- **Action:** set the input `labels: "false"`. Otherwise the token step requests a
  permission that the installation does not have, and GitHub refuses the token. The run
  then falls back to report mode.

## 2. Install the App

On the App's page, click **Install App**. Choose the account, then either **All
repositories** or **Only select repositories**. You can change this list later in the
installation's settings.

If a repository is not in the list, the reviewer does not fail. It reports
"Not posted: the GitHub App '<slug>' is not installed on <owner>/<repo>".

## 3. Give the credentials to the Action

For each repository (Settings → Secrets and variables → Actions) or once for the
organisation:

| Kind | Name | Value |
|---|---|---|
| Secret | `MISTRAL_API_KEY` | A Mistral API key for a workspace with Workflows access |
| Secret | `MSREVIEW_APP_PRIVATE_KEY` | The full contents of the `.pem` file, including the `-----BEGIN` and `-----END` lines |
| Variable | `MSREVIEW_APP_CLIENT_ID` | The App's Client ID |

With the GitHub CLI:

```bash
gh secret set MISTRAL_API_KEY --repo OWNER/REPO            # paste the key when prompted
gh secret set MSREVIEW_APP_PRIVATE_KEY --repo OWNER/REPO < path/to/app.private-key.pem
gh variable set MSREVIEW_APP_CLIENT_ID --repo OWNER/REPO --body "Iv23li..."
```

Then copy [`examples/workflows/morning-star-code-review.yml`](../examples/workflows/morning-star-code-review.yml)
into `.github/workflows/`. The App key is used only by the
`actions/create-github-app-token` step. That step mints a one-hour installation token
for the current repository, with Contents: read, Pull requests: write and Issues: write.

## 4. Give the credentials to the CLI (optional)

The CLI needs the App only to post. Choose one way to supply the private key:

```bash
# A file that only you can read (msreview refuses keys that group or others can read)
mkdir -p ~/.config/morning-star-review
install -m 600 path/to/app.private-key.pem ~/.config/morning-star-review/app.pem
export MSREVIEW_APP_CLIENT_ID=Iv23li...
export MSREVIEW_APP_PRIVATE_KEY_PATH=~/.config/morning-star-review/app.pem

# Or a command that prints the PEM (nothing on disk)
export MSREVIEW_APP_PRIVATE_KEY_COMMAND='op read "op://Private/msreview-app/private-key"'
```

The same settings can go into the `[app]` table of
`~/.config/morning-star-review/config.toml` (see [cli.md](cli.md)). Only a short-lived
child process reads the key. It signs a JWT, mints an installation token for the one
repository under review, and exits. The key never reaches the review process, the
Workflows worker or the agents.

## 5. Check the setup

```bash
msreview app-check OWNER/REPO
```

Example output:

```
The GitHub App 'acme-code-review' is installed on OWNER/REPO.
  contents: read
  issues: write
  metadata: read
  pull_requests: write
```

The command exits 1 when the App is not installed or when Pull requests: write is
missing. `msreview doctor` also checks that the client ID and the key are set, and that
the key file has mode 0600.
