**Critical · correctness: Page slice off by one**

Each page has size + 1 items, and consecutive pages overlap by one item.

**Suggested fix:** Drop the + 1.

```suggestion
        end = start + size
```

<sub>Morning Star Code Review · zai-glm-5-3, mistral-large-4, deepseek-v4-1-flash-1017 · `aaaaaaa` · confirmed by the verifier · id `f96addfc28589`</sub>
<!-- morning-star-review:finding f96addfc28589 -->

---

**Critical · security: Ownership check is inverted**

Anyone except the owner may edit the entry; the owner may not.

**Suggested fix:** Compare with `==`.

```suggestion
        if user.id == entry.owner_id:
```

<sub>Morning Star Code Review · zai-glm-5-3 · `aaaaaaa` · confirmed by the verifier · id `f93ebfca948b4`</sub>
<!-- morning-star-review:finding f93ebfca948b4 -->

---

**Critical · correctness: prorate arguments are swapped**

`prorate(amount, days, total)` is defined in ledger/money.py; days and amount are passed in the wrong order.

**Suggested fix:** Call `prorate(amount, days, total_days)`.

<sub>Morning Star Code Review · mistral-large-4 · `aaaaaaa` · confirmed by the verifier · id `f09980c2acb07`</sub>
<!-- morning-star-review:finding f09980c2acb07 -->
