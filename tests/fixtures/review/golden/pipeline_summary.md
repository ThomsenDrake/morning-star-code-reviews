<!-- morning-star-review:summary v1 -->
### Morning Star Code Review
**Verdict: 5 open findings (4 critical, 1 medium)** · 2 possible issues (not confirmed) · `aaaaaaa` · full review
**Not ready to merge:** 5 open findings at medium severity or above.

#### Open findings

| ID | Severity | Finding | Location | Status |
|---|---|---|---|---|
| `f96addfc28589` | Critical | Page slice off by one | [`ledger/service.py:14`](https://github.com/octo/ledger/blob/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/ledger/service.py#L14) | new |
| `f93ebfca948b4` | Critical | Ownership check is inverted | [`ledger/service.py:18`](https://github.com/octo/ledger/blob/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/ledger/service.py#L18) | new |
| `f09980c2acb07` | Critical | prorate arguments are swapped | [`ledger/service.py:27`](https://github.com/octo/ledger/blob/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/ledger/service.py#L27) | new |
| `fec35660df683` | Critical | SQL built by string concatenation | [`ledger/service.py:23`](https://github.com/octo/ledger/blob/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/ledger/service.py#L23) | [still open](https://github.com/octo/ledger/pull/7#discussion_r103) |
| `f83da676389d7` | Medium | Total ignores refunds | [`ledger/service.py:30`](https://github.com/octo/ledger/blob/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/ledger/service.py#L30) | [still open (resolved without a fix by drive-by)](https://github.com/octo/ledger/pull/7#discussion_r105) |

<details><summary>Explanations and suggested fixes</summary>

##### `f96addfc28589` · Critical · correctness: Page slice off by one

Each page has size + 1 items, and consecutive pages overlap by one item.

**Suggested fix:** Drop the + 1.

Suggested replacement for line 14:

```python
        end = start + size
```

##### `f93ebfca948b4` · Critical · security: Ownership check is inverted

Anyone except the owner may edit the entry; the owner may not.

**Suggested fix:** Compare with `==`.

Suggested replacement for line 18:

```python
        if user.id == entry.owner_id:
```

##### `f09980c2acb07` · Critical · correctness: prorate arguments are swapped

`prorate(amount, days, total)` is defined in ledger/money.py; days and amount are passed in the wrong order.

**Suggested fix:** Call `prorate(amount, days, total_days)`.

##### `fec35660df683` · Critical · security: SQL built by string concatenation (unconfirmed)

`name` is pasted into the SQL text; a crafted name can read any row.

**Suggested fix:** Pass `name` as a query parameter.

</details>

#### Possible issues (not confirmed)

The verifier could neither confirm nor rule these out. Check them yourself.

| Severity | Finding | Location | Reported by |
|---|---|---|---|
| Medium | prorate has no guard for zero total days | `ledger/money.py` | 1 explorer |
| Low | Description is hard-coded | [`ledger/service.py:33`](https://github.com/octo/ledger/blob/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/ledger/service.py#L33) | 1 explorer |

<details><summary>Fixed and dismissed findings (1)</summary>

| ID | Severity | Finding | Location | Outcome |
|---|---|---|---|---|
| `f0000000000aa` | High | Old null check | `ledger/service.py:20` | fixed in `aaaaaaa` |

</details>

#### Coverage

1 of 1 reviewable changed file reviewed.

Excluded (1): `uv.lock` (lock file).

<details><summary>Run details</summary>

Execution `msreview-ci-octo-ledger-7-aaaaaaa-x1y2` · run `gha:octo/ledger/123/1` · version `0.1.0`

| Role | Model | Effort | LLM calls | Tool calls | Input tokens (cached) | Output tokens (reasoning) | Est. cost | Time |
|---|---|---|---|---|---|---|---|---|
| explorer | zai-glm-5-3 | high | 5 | 7 | 12,000 (3,000) | 900 (400) | $0.06 | 42 s |
| explorer | mistral-large-4 | high | 5 | 7 | 12,000 (3,000) | 900 (400) | $0.05 | 42 s |
| explorer | deepseek-v4-1-flash-1017 | high | 5 | 7 | 12,000 (3,000) | 900 (400) | $0.04 | 42 s |
| verifier | zai-glm-5-3 | high | 4 | 9 | 12,000 (3,000) | 900 (400) | $0.03 | 80 s |
| formatter | mistral-large-4 | low | 5 | 7 | 12,000 (3,000) | 900 (400) | $0.01 | 42 s |

Estimated cost: $0.19 (list prices) · duration 312 s

Ensemble: 9 draft findings from 3 explorers merged into 7 candidates

Verifier: 3 confirmed, 3 not confirmed, 1 refuted and dropped

</details>

<sub>Automated review; findings can be wrong. Open findings lists everything still outstanding on this pull request. To dismiss a finding, resolve its thread or comment `/msreview dismiss <id> <reason>` (anyone with write access).</sub>
