# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Before 1.0, a minor version change can
break compatibility, and a review made by an older minor version is reviewed again in
full.

## [0.1.0] - Unreleased

First public release.

### Added

- **GitHub Action** (`action.yml`, composite). It reviews a pull request as a Mistral
  Workflow and posts as your own GitHub App. Inputs cover presets, models, efforts,
  verification, budgets, labels, suggestions, `fail-on` and deadlines. The Action
  has outputs, writes a step summary, and uploads an artifact with `report.md`,
  `findings.json` and `run.json`.
- **Example workflows:**
  - `examples/workflows/morning-star-code-review.yml` for same-repository PRs;
  - `examples/workflows/morning-star-code-review-forks.yml`, a hardened
    `pull_request_target` template for fork PRs. It reviews trusted authors
    automatically. Other authors need a maintainer's `msreview: safe-to-review` label,
    which covers one head commit only.
- **CLI `msreview`:**
  - `review` (post or report);
  - `doctor` (with `--models` and `--deployments`);
  - `setup` (a hash-pinned agent environment);
  - `app-check`;
  - `ci` (the Action's entry point).

  You install it with `uv tool install git+https://github.com/ThomsenDrake/morning-star-code-reviews`.
- **Key commands:** the Mistral key and the App's private key can come from any command
  that prints them, for example a secrets-manager CLI (`[mistral] api_key_command`,
  `[app] private_key_command`, or the `MSREVIEW_*_COMMAND` variables). The output is
  kept in memory only.
- **Presets:** `best` (the default: three explorers from different model families,
  then a deterministic merge with votes, then a verifier), `balanced`, `single`, `fast`
  and `mistral`. Supported models: `zai-glm-5-3`, `mistral-large-4`,
  `zai-glm-5-3-flash`, `deepseek-v4-1-flash-1017`.
- **Verifier:** each merged candidate is confirmed, refuted or marked uncertain.
  Uncertain findings go to "Possible issues (not confirmed)". Refuted findings are
  dropped and kept in the report JSON.
- **Deterministic anchoring:** code anchors findings to diff lines, and evidence must
  match the head file. Inline comments are posted in one `COMMENT` review, with a
  one-by-one fallback when GitHub rejects a position.
- **Validated suggestion blocks:** a suggestion is posted only when the range is
  exact and small, the change is real, the indentation is the same, and the file still
  parses. Empty or no-op suggestions are never posted.
- **Verdict labels:** `msreview: clean`, `msreview: findings` and
  `msreview: incomplete`. There is one label for each PR, it is created when missing,
  and it is best effort.
- **Incremental reviews:** later pushes review only the files changed since the last
  complete review. Findings whose code changed are answered and hidden as resolved. A
  thread that a person with write access resolves dismisses its finding.
- **Report mode:** when the App is missing or not installed, or `post: never` is set,
  the full review is still produced, together with the reason it was not posted.
- **Cross-file context:** the definitions of functions called on added lines are added
  to the prompt.
- **Repository configuration** `.morning-star-review.yml`, read from the base commit.
  It can only lower budgets. The CLI ignores its model and budget settings unless you
  pass `--trust-repo-config`.
- **Security measures:**
  - read-only, confined agent tools;
  - hardened git;
  - allowlisted child environments;
  - HMAC-sealed payloads and pinned activities;
  - output sanitising (markers, mentions, links, HTML);
  - redaction of logs and errors;
  - telemetry off by default.
- **Evaluation harness** in `eval/`: a planted-bug fixture project, answer keys,
  scripts to open the test PRs and to score runs, and the first results
  (`eval/results/2026-11-17.md`).
- **Documentation** in `docs/`.

### Known limitations

- Mistral Workflows is a Public Preview product. This release pins
  `mistralai-workflows==3.19.0`, `mistralai==3.7.0` and `mistralai-agents==1.1.3`.
- The agent environment installs `mistralai-vibe-harness`, which is proprietary, as a
  dependency of `mistralai-agents`. See `NOTICE`.
- Review threads are not resolved automatically, because that needs Contents: write,
  which we do not request. The bot replies and hides its comment instead.
- Each reviewed PR leaves about one inactive deployment record in the Mistral
  workspace. The API cannot delete these records.
- Only github.com is supported (no GitHub Enterprise Server).

[0.1.0]: https://github.com/ThomsenDrake/morning-star-code-reviews/releases/tag/v0.1.0
