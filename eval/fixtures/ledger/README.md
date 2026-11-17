# ledger

A deliberately small account ledger: users own accounts, accounts hold
transactions in integer cents, and a thin handler layer turns request
dictionaries into `(status, body)` responses. Storage is SQLite from the
standard library, so the project has no runtime dependencies.

```
ledger/
  money.py       parsing, formatting and proration of cent amounts
  models.py      plain dataclasses
  db.py          SQLite storage
  pagination.py  page parameters and page envelopes
  auth.py        who may see or change an account
  service.py     business rules on top of the storage layer
  handlers.py    request handlers
```

Run the tests with `python -m pytest`.
