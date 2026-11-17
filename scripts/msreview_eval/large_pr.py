"""Deterministic file generator for the large-PR case (E6).

The large PR adds about 80 files to the ledger fixture: one lock file, files
with a generated-code marker, a vendored package, many similar report modules,
tests, docs and translation catalogs. The one planted bug lives in a
hand-written report module that is added by a patch on top of these files. It
has fewer changed lines than a report module, so it ranks below them, and a
larger diff, so it cannot fill the gap they leave in the inline diff budget:
the reviewer only sees it if it opens the file on demand.

Everything returned here is plain text built from fixed tables: the same input
always yields byte-identical files, which keeps the PR branch reproducible.
"""

from __future__ import annotations

import hashlib
import json

# (module name, title, predicate expression over ``tx``, one-line predicate doc)
REPORTS: tuple[tuple[str, str, str, str], ...] = (
    ("credits", "Credits", "tx.amount_cents > 0", "Money coming in."),
    ("debits", "Debits", "tx.amount_cents < 0", "Money going out."),
    ("deposits", "Deposits", 'tx.memo.startswith("deposit")', "Recorded deposits."),
    ("withdrawals", "Withdrawals", 'tx.memo.startswith("withdrawal")', "Recorded withdrawals."),
    ("transfers", "Transfers", 'tx.memo.startswith("transfer ")', "Both legs of transfers."),
    ("plan_fees", "Plan fees", 'tx.memo.startswith("plan change to ")', "Plan change fees."),
    (
        "large_credits",
        "Large credits",
        "tx.amount_cents >= 100_000",
        "Credits of 1,000.00 or more.",
    ),
    ("large_debits", "Large debits", "tx.amount_cents <= -100_000", "Debits of 1,000.00 or more."),
    ("small_amounts", "Small amounts", "abs(tx.amount_cents) < 1_000", "Below 10.00 either way."),
    ("cent_amounts", "Cent amounts", "abs(tx.amount_cents) < 100", "Below one unit either way."),
    ("round_amounts", "Round amounts", "tx.amount_cents % 100 == 0", "Whole units without cents."),
    ("morning", "Morning activity", 'tx.created_at[11:13] < "12"', "Before noon UTC."),
    ("evening", "Evening activity", 'tx.created_at[11:13] >= "18"', "From 18:00 UTC on."),
    ("night", "Night activity", 'tx.created_at[11:13] < "06"', "Before 06:00 UTC."),
    (
        "month_start",
        "Start of month",
        'tx.created_at[8:10] <= "07"',
        "First seven days of a month.",
    ),
    ("month_end", "End of month", 'tx.created_at[8:10] >= "24"', "From the 24th of a month on."),
    (
        "first_quarter",
        "First quarter",
        'tx.created_at[5:7] in ("01", "02", "03")',
        "January-March.",
    ),
    ("second_quarter", "Second quarter", 'tx.created_at[5:7] in ("04", "05", "06")', "April-June."),
    (
        "third_quarter",
        "Third quarter",
        'tx.created_at[5:7] in ("07", "08", "09")',
        "July-September.",
    ),
    (
        "fourth_quarter",
        "Fourth quarter",
        'tx.created_at[5:7] in ("10", "11", "12")',
        "October-December.",
    ),
    ("without_memo", "Without memo", "not tx.memo.strip()", "Transactions with an empty memo."),
    ("tagged", "Tagged", '"#" in tx.memo', "Memos that carry a #tag."),
    ("refund_like", "Refund-like", '"refund" in tx.memo.lower()', "Memos that mention a refund."),
    (
        "salary_like",
        "Salary-like",
        '"salary" in tx.memo.lower() and tx.amount_cents > 0',
        "Salary credits.",
    ),
    ("rent_like", "Rent-like", '"rent" in tx.memo.lower() and tx.amount_cents < 0', "Rent debits."),
)

TESTED_REPORTS = 14
DOCUMENTED_REPORTS = 10
LOCALES: dict[str, dict[str, str]] = {
    "de": {"total": "Summe", "average": "Durchschnitt", "largest": "Größter Betrag", "day": "Tag"},
    "en": {"total": "Total", "average": "Average", "largest": "Largest", "day": "Day"},
    "es": {"total": "Total", "average": "Promedio", "largest": "Mayor importe", "day": "Día"},
    "fr": {"total": "Total", "average": "Moyenne", "largest": "Montant maximal", "day": "Jour"},
    "it": {"total": "Totale", "average": "Media", "largest": "Importo massimo", "day": "Giorno"},
    "nl": {"total": "Totaal", "average": "Gemiddelde", "largest": "Grootste bedrag", "day": "Dag"},
    "pl": {"total": "Suma", "average": "Średnia", "largest": "Największa kwota", "day": "Dzień"},
    "pt": {"total": "Total", "average": "Média", "largest": "Maior valor", "day": "Dia"},
}
GENERATED_GROUPS = (
    "africa",
    "americas_north",
    "americas_south",
    "asia_east",
    "asia_south",
    "asia_west",
    "europe_east",
    "europe_north",
    "europe_south",
    "europe_west",
    "oceania",
)
LOCK_PACKAGES = 40


def _camel(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


_REPORT_TEMPLATE = '''"""__TITLE__ report.

Summarises the transactions selected by ``selected`` into per-day totals and
an overall summary. Amounts are integer cents throughout.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from ledger.models import Transaction
from ledger.money import format_amount

REPORT_NAME = "__NAME__"
TITLE = "__TITLE__"


@dataclass(frozen=True)
class __CLS__Summary:
    count: int
    total_cents: int
    largest_cents: int
    smallest_cents: int

    @property
    def average_cents(self) -> int:
        """Mean amount rounded down; zero when nothing was selected."""
        if self.count == 0:
            return 0
        return self.total_cents // self.count


EMPTY = __CLS__Summary(count=0, total_cents=0, largest_cents=0, smallest_cents=0)


def selected(tx: Transaction) -> bool:
    """__PREDDOC__"""
    return __PRED__


def select(transactions: Iterable[Transaction]) -> list[Transaction]:
    """The transactions this report covers, in their original order."""
    return [tx for tx in transactions if selected(tx)]


def summarise(transactions: Iterable[Transaction]) -> __CLS__Summary:
    """Count, total and extremes of the selected transactions."""
    amounts = [tx.amount_cents for tx in select(transactions)]
    if not amounts:
        return EMPTY
    return __CLS__Summary(
        count=len(amounts),
        total_cents=sum(amounts),
        largest_cents=max(amounts),
        smallest_cents=min(amounts),
    )


def per_day(transactions: Iterable[Transaction]) -> dict[str, int]:
    """Total cents per calendar day (``YYYY-MM-DD``), days in ascending order."""
    totals: dict[str, int] = {}
    for tx in select(transactions):
        day = tx.created_at[:10]
        totals[day] = totals.get(day, 0) + tx.amount_cents
    return dict(sorted(totals.items()))


def busiest_days(transactions: Iterable[Transaction], limit: int = 3) -> list[tuple[str, int]]:
    """Days with the most selected transactions, most first; ties by date."""
    counts = Counter(tx.created_at[:10] for tx in select(transactions))
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return ranked[:limit]


def memo_words(transactions: Iterable[Transaction], limit: int = 5) -> list[str]:
    """Most frequent lower-cased memo words (three letters or more)."""
    counts: Counter[str] = Counter()
    for tx in select(transactions):
        counts.update(word for word in tx.memo.lower().split() if len(word) > 2)
    return [word for word, _ in counts.most_common(limit)]


def compare(previous: Iterable[Transaction], current: Iterable[Transaction]) -> dict[str, int]:
    """Change of count and total between two periods (current minus previous)."""
    before = summarise(previous)
    after = summarise(current)
    return {
        "count": after.count - before.count,
        "total_cents": after.total_cents - before.total_cents,
    }


def render(transactions: Iterable[Transaction], currency: str) -> list[str]:
    """Plain-text lines for the __TITLE_LOWER__ section of a monthly statement."""
    rows = list(transactions)
    summary = summarise(rows)
    lines = [f"{TITLE} ({summary.count} transactions)"]
    lines.append(f"  total:   {format_amount(summary.total_cents, currency)}")
    lines.append(f"  average: {format_amount(summary.average_cents, currency)}")
    lines.append(f"  largest: {format_amount(summary.largest_cents, currency)}")
    for day, total in per_day(rows).items():
        lines.append(f"  {day}  {format_amount(total, currency)}")
    return lines
'''


def report_module(name: str, title: str, predicate: str, doc: str) -> str:
    return (
        _REPORT_TEMPLATE.replace("__NAME__", name)
        .replace("__TITLE_LOWER__", title.lower())
        .replace("__TITLE__", title)
        .replace("__CLS__", _camel(name))
        .replace("__PREDDOC__", doc)
        .replace("__PRED__", predicate)
    )


def report_test(name: str) -> str:
    return f"""from __future__ import annotations

from ledger.models import Transaction
from ledger.reports import {name}


def _tx(tx_id: int, cents: int, memo: str = "") -> Transaction:
    when = "2026-03-02T10:00:00Z"
    return Transaction(id=tx_id, account_id=1, amount_cents=cents, memo=memo, created_at=when)


def test_empty_input_gives_empty_summary() -> None:
    assert {name}.summarise([]) == {name}.EMPTY
    assert {name}.per_day([]) == {{}}


def test_render_starts_with_title() -> None:
    assert {name}.render([], "EUR")[0].startswith({name}.TITLE)


def test_selected_rows_keep_their_order() -> None:
    rows = [_tx(1, 500, "deposit"), _tx(2, -300, "withdrawal"), _tx(3, 700, "deposit")]
    picked = {name}.select(rows)
    assert [tx.id for tx in picked] == sorted(tx.id for tx in picked)
"""


def report_doc(name: str, title: str, doc: str) -> str:
    return f"""# {title} report

Module: `ledger/reports/{name}.py`

Selects: {doc}

The report prints the number of selected transactions, their total, the
average and the largest amount, followed by one line per calendar day.
`compare(previous, current)` returns the change in count and total between two
periods, for example two consecutive months.
"""


_VENDOR_FILES: dict[str, str] = {
    "__init__.py": '''"""tinyfmt: small text formatting helpers (vendored copy, see VERSION)."""

from .align import center, ljust, rjust
from .numbers import group_digits
from .table import render_table
from .wrap import wrap_words

__all__ = ["center", "group_digits", "ljust", "render_table", "rjust", "wrap_words"]
''',
    "align.py": '''"""Padding helpers that never truncate their input."""


def ljust(text: str, width: int, fill: str = " ") -> str:
    return text + fill * max(0, width - len(text))


def rjust(text: str, width: int, fill: str = " ") -> str:
    return fill * max(0, width - len(text)) + text


def center(text: str, width: int, fill: str = " ") -> str:
    missing = max(0, width - len(text))
    left = missing // 2
    return fill * left + text + fill * (missing - left)
''',
    "numbers.py": '''"""Digit grouping for integers."""


def group_digits(value: int, separator: str = ",") -> str:
    sign = "-" if value < 0 else ""
    digits = str(abs(value))
    groups = []
    while digits:
        groups.append(digits[-3:])
        digits = digits[:-3]
    return sign + separator.join(reversed(groups))
''',
    "table.py": '''"""Fixed-width plain-text tables."""

from .align import ljust, rjust


def render_table(header: list[str], rows: list[list[str]], numeric: set[int] | None = None) -> str:
    numeric = numeric or set()
    widths = [len(cell) for cell in header]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = []
    for row in [header, *rows]:
        cells = []
        for index, cell in enumerate(row):
            pad = rjust if index in numeric else ljust
            cells.append(pad(cell, widths[index]))
        lines.append("  ".join(cells).rstrip())
    return "\\n".join(lines)
''',
    "wrap.py": '''"""Greedy word wrapping."""


def wrap_words(text: str, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}" if current else word
        if len(candidate) <= width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines
''',
    "py.typed": "",
    "README.md": """# tinyfmt (vendored)

A small set of text formatting helpers copied into this repository so the
reports have no third-party runtime dependency. Do not edit these files by
hand; update the copy as a whole and bump `VERSION`.
""",
    "VERSION": "0.4.1\n",
}


def vendor_files() -> dict[str, str]:
    return {f"vendor/tinyfmt/{name}": text for name, text in _VENDOR_FILES.items()}


def generated_files() -> dict[str, str]:
    files: dict[str, str] = {}
    header = "# Code generated by tools/gen_codes.py. DO NOT EDIT.\n"
    for group in GENERATED_GROUPS:
        lines = [header, f'"""Bank routing prefixes for the {group} region."""\n', "PREFIXES = {"]
        for index in range(30):
            digest = hashlib.sha256(f"{group}:{index}".encode()).hexdigest()
            code = digest[:6].upper()
            lines.append(f'    "{group[:2].upper()}{index:03d}": "{code}",')
        lines.append("}\n")
        files[f"ledger/generated/codes_{group}.py"] = "\n".join(lines)
    names = ", ".join(f'"codes_{group}"' for group in GENERATED_GROUPS)
    files["ledger/generated/__init__.py"] = header + f"__all__ = [{names}]\n"
    return files


def lock_file() -> str:
    parts = ["version = 1", 'requires-python = ">=3.11"', ""]
    for index in range(LOCK_PACKAGES):
        name = f"fixture-pkg-{index:02d}"
        version = f"{1 + index % 4}.{index % 7}.{index % 3}"
        digest = hashlib.sha256(f"{name}=={version}".encode()).hexdigest()
        parts += [
            "[[package]]",
            f'name = "{name}"',
            f'version = "{version}"',
            'source = { registry = "https://pypi.org/simple" }',
            f'sdist = {{ url = "https://files.example.org/{name}-{version}.tar.gz",'
            f' hash = "sha256:{digest}" }}',
            "",
        ]
    return "\n".join(parts)


def locale_files() -> dict[str, str]:
    files: dict[str, str] = {}
    for lang, labels in LOCALES.items():
        catalog = {f"report.label.{key}": value for key, value in labels.items()}
        for name, title, _pred, _doc in REPORTS:
            catalog[f"report.{name}.title"] = title
        files[f"locales/{lang}.json"] = json.dumps(catalog, indent=2, ensure_ascii=False) + "\n"
    return files


def large_pr_files() -> dict[str, str]:
    """Every generated file of the large PR, keyed by repository path."""
    files: dict[str, str] = {"uv.lock": lock_file()}
    files.update(generated_files())
    files.update(vendor_files())
    files.update(locale_files())
    entries = "".join(f'    "{name}",\n' for name in sorted(name for name, *_ in REPORTS))
    files["ledger/reports/__init__.py"] = (
        '"""Statement reports. Each module exposes ``summarise`` and ``render``."""\n\n'
        f"__all__ = [\n{entries}]\n"
    )
    for name, title, predicate, doc in REPORTS:
        files[f"ledger/reports/{name}.py"] = report_module(name, title, predicate, doc)
    for name, *_ in REPORTS[:TESTED_REPORTS]:
        files[f"tests/reports/test_{name}.py"] = report_test(name)
    for name, title, _pred, doc in REPORTS[:DOCUMENTED_REPORTS]:
        files[f"docs/reports/{name}.md"] = report_doc(name, title, doc)
    return files
