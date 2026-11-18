### Morning Star Code Review report
**Verdict: 5 open findings (4 critical, 1 medium)** · 2 possible issues (not confirmed) · `aaaaaaa` · full review

| Severity | Finding | Location | Status |
|---|---|---|---|
| Critical | Page slice off by one | [`ledger/service.py:14`](https://github.com/octo/ledger/blob/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/ledger/service.py#L14) | new |
| Critical | Ownership check is inverted | [`ledger/service.py:18`](https://github.com/octo/ledger/blob/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/ledger/service.py#L18) | new |
| Critical | prorate arguments are swapped | [`ledger/service.py:27`](https://github.com/octo/ledger/blob/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/ledger/service.py#L27) | new |
| Critical | SQL built by string concatenation | [`ledger/service.py:23`](https://github.com/octo/ledger/blob/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/ledger/service.py#L23) | [still open](https://github.com/octo/ledger/pull/7#discussion_r103) |
| Medium | Total ignores refunds | [`ledger/service.py:30`](https://github.com/octo/ledger/blob/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/ledger/service.py#L30) | [still open (resolved without a fix by drive-by)](https://github.com/octo/ledger/pull/7#discussion_r105) |

#### Possible issues (not confirmed)

The verifier could neither confirm nor rule these out. Check them yourself.

| Severity | Finding | Location | Reported by |
|---|---|---|---|
| Medium | prorate has no guard for zero total days | `ledger/money.py` | 1 explorer |
| Low | Description is hard-coded | [`ledger/service.py:33`](https://github.com/octo/ledger/blob/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/ledger/service.py#L33) | 1 explorer |

#### Resolved since the last review

- Old null check (`ledger/service.py`): code changed

#### Findings in detail

##### 1. Critical · correctness: Page slice off by one

`ledger/service.py:14` · on a changed line · reported by zai-glm-5-3, mistral-large-4, deepseek-v4-1-flash-1017 · confirmed by the verifier

Each page has size + 1 items, and consecutive pages overlap by one item.

**Suggested fix:** Drop the + 1.

Suggested replacement for line 14:

```python
        end = start + size
```

Code at head:

```python
12      def page(self, items, page, size):
13          start = page * size
14          end = start + size + 1
15          return items[start:end]
16
```

##### 2. Critical · security: Ownership check is inverted

`ledger/service.py:18` · on a changed line · reported by zai-glm-5-3 · confirmed by the verifier

Anyone except the owner may edit the entry; the owner may not.

**Suggested fix:** Compare with `==`.

Suggested replacement for line 18:

```python
        if user.id == entry.owner_id:
```

Code at head:

```python
16
17      def can_edit(self, user, entry):
18          if user.id != entry.owner_id:
19              return True
20          return False
```

##### 3. Critical · correctness: prorate arguments are swapped

`ledger/service.py:27` · on a changed line · reported by mistral-large-4 · confirmed by the verifier

`prorate(amount, days, total)` is defined in ledger/money.py; days and amount are passed in the wrong order.

**Suggested fix:** Call `prorate(amount, days, total_days)`.

Code at head:

```python
25
26      def split(self, amount, days, total_days):
27          return prorate(days, amount, total_days)
28
29      def total(self, entries):
```

##### 4. Critical · security: SQL built by string concatenation (unconfirmed)

`ledger/service.py:23` · on a changed line · reported by zai-glm-5-3 · carried from the previous review · not confirmed by the verifier

`name` is pasted into the SQL text; a crafted name can read any row.

**Suggested fix:** Pass `name` as a query parameter.

Code at head:

```python
21
22      def find(self, name):
23          query = "SELECT * FROM entries WHERE name = '" + name + "'"
24          return self.db.execute(query)
25
```

##### 5. Medium · Total ignores refunds

`ledger/service.py:30` · on a changed line · carried from the previous review

Code at head:

```python
28
29      def total(self, entries):
30          return sum((e.amount for e in entries), Decimal(0))
31
32      def describe(self):
```

##### 6. Medium · reliability: prorate has no guard for zero total days (unconfirmed)

`ledger/money.py` · outside this PR's changed files · reported by deepseek-v4-1-flash-1017 · not confirmed by the verifier

Division by zero when total is 0.

**Suggested fix:** Drop the + 1.

##### 7. Low · other: Description is hard-coded (unconfirmed)

`ledger/service.py:33` · in a changed file, not on a changed line · reported by mistral-large-4 · not confirmed by the verifier

Minor: consider a constant.

Code at head:

```python
31
32      def describe(self):
33          return 'ledger'
34
35
```

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
