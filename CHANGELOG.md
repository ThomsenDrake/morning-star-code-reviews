# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Before 1.0, a minor version change can
break compatibility, and a review made by an older minor version is reviewed again in
full.

## [0.1.1] - 2026-11-18

Rate limits now make a review slower. They no longer make it fail.

### Upgrade note

- **Raise `timeout-minutes` in your workflow.** The default `deadline-minutes` is now
  90 for the Action and the CLI (it was 40 and 45). Workflows copied from the 0.1.0
  examples set `timeout-minutes: 45`, so GitHub would cancel the job before a slow
  review can post. Set `timeout-minutes: 110`, or keep a shorter job and lower both,
  for example `deadline-minutes: "30"` with `timeout-minutes: 50`. Keep
  `timeout-minutes` about 20 above `deadline-minutes`.
- `deadline-minutes` (and the CLI's `--deadline-minutes`) now accepts 20 to 300 (it was
  1 to 55). A value below 20 is rejected.

### Changed

- **Rate limits and temporary model errors wait instead of failing.** HTTP 429, 5xx
  and "capacity" errors no longer fail the run, drop an explorer or the verifier, or
  use up a model-call slot:
  - a refused model turn is retried with no count limit. The waits grow 15 s, 30 s,
    60 s, 120 s, then 180 s (plus up to 25 % jitter). A `Retry-After` from the API is
    honoured up to 5 minutes. A successful reply starts the curve again at 15 s;
  - the time spent waiting does not count against the agent's own time budget
    (explorer, verifier or formatter minutes);
  - agents of one run that use the same model share a cooldown, so they do not all
    retry a limited model at the same moment;
  - lost streams and request timeouts are still retried, but at most 6 times in a row;
  - when a full wait would end after the agent's last allowed model call (see the
    wind-down below), the agent makes one last, shorter wait before the time limit:
    it ends 35 s before that call's limit, so the last try still gets its time. If
    that try is refused too, the agent stops, and the review says that the run's time
    limit stopped it. A wait that is not made does not make other agents of the same
    model wait;
  - the model SDK's own wait on a `Retry-After` header is capped at 60 s for each try.
    Longer waits happen in the agent's retry loop, where they are logged and counted.
- **One overall time limit with a wind-down.** `deadline-minutes` is the hard limit of
  the whole run. 15 minutes before it (less for deadlines under an hour), the run winds
  down: explorers stop opening files and write their notes, and the review is
  published with what it has. It is never `clean` then: the verdict is `incomplete`
  (or `findings`), with the reason "the review was finished early to stay within its
  90-minute time limit". If the hard limit is reached anyway, the PR still gets the failure
  banner and the `msreview: incomplete` label.
- **The verifier keeps a reserved window.** With verification on (for example in the
  default preset `best`), the verifier may start until 15 minutes before the deadline
  (less for deadlines under an hour), and the explorers stop early enough for that:
  they stop opening files 25.5 minutes before the deadline and make their last model
  call 20.5 minutes before it (for the default deadline of 90: minutes 64.5 and 69.5).
  So an explorer that is rate-limited the whole time can no longer crowd out the
  verifier; it keeps the notes it wrote, its findings are verified with the others,
  and the review says that the time limit stopped it. Without verification the
  timeline is unchanged.
- **Internal timeouts no longer cut a slow run short.** The per-step and workflow
  timeouts now sit above the largest allowed deadline; heartbeats still detect a stuck
  step. Mistral Workflows API errors (429, 5xx, network) while starting, waiting for
  the worker and polling are retried with backoff until the deadline.
- **GitHub API rate limits wait longer.** One request now waits up to 15 minutes in
  total for a rate limit (it was 3 waits of at most 60 s), but never past the run's
  deadline.
- **Action: the token that posts is minted after the review.** The review step now
  uses a read-only App token. A new publish step mints the write token after the
  review and posts the result, so a long review can no longer outlive the token's
  60-minute lifetime. The App's private key is used only in the two token steps.
  `remove-label` is now handled by the publish step.
- **CLI: the App token is minted again before posting** when it is older than
  45 minutes. The private key still stays in the short-lived mint process.
- The example workflows set `timeout-minutes: 110` and pin `@v0.1.1`.

### Added

- **Rate-limit waits are visible.** While a model is rate-limited, the log shows
  `msreview: <model> rate-limited (explore); waiting 60s (total waited 1m 45s)`.
  The run details of the summary get a `Rate-limit wait` column (for example
  `7m 10s (5x)`) and the total wait, when any model had to wait. The start of a run
  prints the deadline and the wind-down time.
- `msreview ci-publish`: the Action's publish step.

### Fixed

- Failure reasons no longer repeat the error type (they read
  `MsreviewBadRequest: MsreviewBadRequest: ...`).

## [0.1.0] - 2026-11-17

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
    which covers one head commit only. The label counts only when the person who added
    it has write, maintain or admin access (the triage role can add labels too).
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
  Uncertain findings go to "Possible issues (not confirmed)"; on their own they do not
  make the verdict `findings`, and `fail-on` and the `findings` output do not count
  them. Refuted findings are dropped and kept in the report JSON.
- **Budgets:** per-run cost cap, and tool-call and time budgets for each agent. A
  tool-call budget that you set applies as set, also above a model's own default (25
  for the flash models), up to the hard limit of 200. A turn that fails with a
  temporary model error (for example HTTP 429) is retried while the agent's deadline
  and cost share allow.
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
  It can only lower budgets. The Action always uses it. Unless you pass
  `--trust-repo-config`, the CLI ignores the file's model and budget settings and the
  settings that steer the review (`exclude`, `review.instructions`,
  `review.path_instructions`, `review.inline_min_severity`) for every repository.
- **Security measures:**
  - read-only, confined agent tools;
  - hardened git;
  - allowlisted child environments;
  - HMAC-sealed payloads and pinned activities;
  - output sanitising (markers, mentions, links, HTML);
  - redaction of logs and errors;
  - telemetry off by default.
- **Evaluation harness** in `eval/`: a planted-bug fixture project, answer keys,
  scripts to open the test PRs and to score runs, and the results
  summary in `docs/models.md`.
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

[0.1.1]: https://github.com/ThomsenDrake/morning-star-code-reviews/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/ThomsenDrake/morning-star-code-reviews/releases/tag/v0.1.0
