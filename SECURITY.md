# Security policy

## Reporting a vulnerability

Please do **not** report security problems in public issues, discussions or pull
requests.

Report them privately through GitHub: on
<https://github.com/ThomsenDrake/morning-star-code-reviews>, open the **Security**
tab and choose **Report a vulnerability**. If that option is not available, open a
public issue that only asks for a private contact. Do not include any details in that
issue.

Please include:

- the version (`msreview --version`, or the action ref that you use);
- how you run it (Action, fork template or CLI), and the relevant configuration;
- the steps to reproduce, and what an attacker gains;
- any proof of concept. Use your own repositories and workspaces.

This is a small project. We aim to acknowledge reports within a week, and to agree on
a disclosure date with you. A fix is released as a new version, and the
[CHANGELOG](CHANGELOG.md) credits you unless you prefer otherwise.

## Supported versions

Only the latest release receives security fixes. Pin the action to a full commit SHA
of a release, and update when a new release is published.

## Scope

In scope, for example:

- a leak of the Mistral API key, the GitHub App private key, or an installation
  token: into logs, comments, reports, workflow payloads, agent processes or child
  processes;
- PR content that makes the reviewer execute code, write to the repository, or reach
  a network peer other than the expected Mistral and GitHub endpoints;
- bypasses of the output sanitiser: mentions, links to other hosts, raw HTML, or forged
  hidden markers or review state in posted comments;
- an escape of the agents' tool confinement, for example reading files outside the
  checkout or reading `.git/`;
- weaknesses in the fork template (`examples/workflows/morning-star-code-review-forks.yml`)
  that let an untrusted author get a review without a maintainer's approval for that head
  commit, or obtain write access;
- payload forgery or replay against the run's Workflows worker.

Out of scope:

- review quality: missed bugs, wrong findings, or text that prompt injection steered
  but that is still sanitised. [docs/security.md](docs/security.md) describes these
  residual risks.
- cost or rate-limit consumption within the configured budgets;
- vulnerabilities in GitHub, Mistral's platform, or third-party packages, including
  `mistralai-agents` and its dependencies. Report them to their maintainers. Do tell us
  if this project's use of them makes the problem worse.

The threat model and its known limits are described in [docs/security.md](docs/security.md).
