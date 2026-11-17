"""Make model-written text safe to post on GitHub (DESIGN 8.5).

Model text is untrusted: a prompt-injected model could try to hide markers, ping
people, cross-reference issues, link to phishing pages, or inject suggestion blocks.
``sanitize_md`` keeps the Markdown readable while removing those effects:

- ``<!--``, ``-->`` and ``morning-star-review:`` marker text are removed everywhere;
- HTML is neutralised by escaping ``<`` before tag-like text (``Optional<User>`` stays
  readable, ``<img>``/``<details>`` show as text) and by escaping character references;
- ``@mentions``, ``#123`` / ``owner/repo#123`` / ``GH-123`` references get a U+200D after
  ``@`` / ``#`` / ``GH``;
- links and URLs to hosts other than github.com become inline code (the link text
  keeps escaped brackets); images become links (then follow the same rule); bare
  github.com URLs become ``<autolinks>``; every other bracket in prose is escaped, so
  no link or reference definition can be assembled from pieces;
- ATX and setext headings are flattened; more than two blank lines collapse;
- fenced code is kept verbatim (only markers are removed) and its fence is moved to
  column 0, unclosed fences are closed, and fences that GitHub renders specially
  (``suggestion``, ``mermaid``...) become plain;
- code spans are matched per line (and per table cell), as GitHub does;
- the result is capped at ``limit`` characters.

The function is idempotent: ``sanitize_md(sanitize_md(x, n), n) == sanitize_md(x, n)``.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Callable
from urllib.parse import urlsplit

__all__ = [
    "ALLOWED_LINK_HOSTS",
    "ELLIPSIS",
    "MARKER_PREFIX",
    "ZWJ",
    "code_span",
    "sanitize_inline",
    "sanitize_md",
    "strip_markers",
    "suggestion_hazards",
]

ZWJ = "\u200d"  # zero-width joiner: breaks @/# autolinking, invisible
MARKER_PREFIX = "morning-star-review:"
ALLOWED_LINK_HOSTS = frozenset({"github.com", "www.github.com"})
ELLIPSIS = "…"

_MARKERS = re.compile(r"<!--|-->|morning-star-review\s*:", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_FENCE_OPEN = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")
# CommonMark closing fence: up to 3 spaces, the fence run, then only spaces or tabs.
_FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")
_SAFE_LANG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_+#.-]{0,40}$")
_SPECIAL_FENCES = frozenset({"suggestion", "mermaid", "geojson", "topojson", "stl"})

# Line-level patterns (prose only).
# Any indentation: inside a list item a heading may be indented 4 or more columns.
_ATX = re.compile(r"^([ \t]*(?:(?:>|[-*+]|\d{1,9}[.)])[ \t]*)*)(#{1,6})(?=[ \t]|$)", re.M)
_SETEXT = re.compile(r"^([ \t]*(?:>[ \t]*)*)(=+|-+)([ \t]*)$", re.M)
# A tilde fence after container markers or deep indentation is a fence inside a list
# item or quote on GitHub (possibly a ```suggestion```); escaped, it stays text.
_TILDE_FENCE = re.compile(r"^([ \t]*(?:(?:>|[-*+]|\d{1,9}[.)])[ \t]*)*)(~{3,})", re.M)
_REF_DEF = re.compile(r"^((?: {0,3}>)*[ \t]{0,3})\[(?=[^\]\n]+\]:)", re.M)
_BLANKS = re.compile(r"^(?:[ \t]*\n){3,}", re.M)  # 3+ blank lines; starts at line starts only
# Inline patterns (prose outside code spans). Markdown links and <autolinks> are
# matched before bare URLs: the inline parser builds links before GitHub autolinks
# bare URLs, so a bare URL match must never swallow a link.
# URLs and destinations never include "|": in a table GitHub splits the cell there
# first, which would cut a generated code span in two and expose the URL.
_LINK = re.compile(
    r"(?P<md>(?<!\\)!?\[(?P<text>[^\[\]\n]{0,1000})\]"  # "\[" is no link opener
    r"\(\s*(?P<dest><[^<>\n|]*>|[^\s()|]*(?:\([^\s()|]*\)[^\s()|]*)*)"
    r"(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^()\n]*\)))?\s*\))"
    r"|(?P<auto><(?P<autourl>[A-Za-z][A-Za-z0-9+.-]{1,31}:[^\s<>|]*)>)"
)
# What GitHub autolinks without markup: http(s)://, ftp:// and www. (any case).
_BARE_URL = re.compile(r"(?:(?i:https?|ftp)://|(?i:www)\.)[^\s<>`\\|]+")
_IMAGE = re.compile(r"!\[")
_ENTITY = re.compile(r"&(?=#?[A-Za-z0-9]{1,32};)(?!(?:amp|lt|gt|quot|apos);)")
_TAG_START = re.compile(r"<(?=[A-Za-z/!?])")
# Only a letter or digit right before "@" rules out a mention: a backtick is a literal
# one in prose, and "_" or "*" may close emphasis, leaving "@" at the start of a node.
_MENTION = re.compile(r"(?<![A-Za-z0-9])@(?=[A-Za-z0-9])")
_ISSUE_REF = re.compile(r"#(?=\d)")
_GH_REF = re.compile(r"\b(GH)-(?=\d)", re.IGNORECASE)
_TRAILING_PUNCT = ".,:;!?*_~'\""
_PIPE = re.compile(r"(?<!\\)((?:\\\\)*)\|")  # a pipe after an even number of backslashes
_BACKTICK_RUN = re.compile(r"`+")
_LITERAL_BACKTICK = re.compile(r"(?<!\\)((?:\\\\)*)`")
_BRACKET = re.compile(r"(?<!\\)((?:\\\\)*)([\[\]])")
# A line that could be a GFM table delimiter row (also inside quotes and list items).
_TABLE_DELIM = re.compile(r"^[ \t>*+\-\d.):|]*$")
_BLOCK_START = frozenset("#~>+*=-")
_ORDERED_START = re.compile(r"^(\d{1,9})([.)])")


def strip_markers(text: str) -> str:
    """Remove ``<!--``, ``-->`` and ``morning-star-review:`` until none is left.

    Repeats because removing one occurrence can join the pieces of another.
    """
    previous = None
    while previous != text:
        previous = text
        text = _MARKERS.sub("", text)
    return text


def code_span(text: str) -> str:
    """``text`` as an inline code span whose fence is longer than any backtick run in it."""
    flat = " ".join(text.split())
    longest = max((len(run) for run in re.findall(r"`+", flat)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if flat.startswith("`") or flat.endswith("`") or not flat else ""
    return f"{fence}{pad}{flat}{pad}{fence}"


def _allowed_url(url: str) -> bool:
    if any(ch.isspace() or ch == "\\" for ch in url):
        return False
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return False
    if parts.scheme.lower() not in {"http", "https"} or "@" in parts.netloc:
        return False
    return host is not None and host.lower() in ALLOWED_LINK_HOSTS


def _plain(text: str) -> str:
    # Backticks left in prose are unmatched (literal); escaping them keeps them from
    # joining a code span we generate next to them.
    text = _LITERAL_BACKTICK.sub(r"\1\\`", text)
    # Brackets left in prose are never part of a link we approved. Escaping them stops
    # GitHub from building a link, image or reference definition out of pieces this
    # module treats separately (for example "[`x`](//host)" or "[a]: //host").
    text = _BRACKET.sub(r"\1\\\2", text)
    text = _ENTITY.sub("&amp;", text)
    text = _TAG_START.sub("&lt;", text)
    text = _MENTION.sub("@" + ZWJ, text)
    text = _ISSUE_REF.sub("#" + ZWJ, text)
    return _GH_REF.sub(lambda m: m.group(1) + ZWJ + "-", text)


def _split_url_tail(url: str) -> tuple[str, str]:
    """Split trailing punctuation (and unbalanced ``)``) off a bare URL, like GFM does."""
    opens, closes = url.count("("), url.count(")")
    end = len(url)
    while end > 0:
        ch = url[end - 1]
        if ch in _TRAILING_PUNCT:
            end -= 1
        elif ch == ")" and closes > opens:
            closes -= 1
            end -= 1
        else:
            break
    return url[:end], url[end:]


def _link_replacement(m: re.Match[str]) -> str:
    if m.group("md") is not None:
        # The text may end up as plain prose (link removed), where GitHub would
        # autolink a bare URL, so it gets the bare-URL treatment too.
        text = _bare_urls(m.group("text"))
        dest = m.group("dest").strip()
        if dest.startswith("<") and dest.endswith(">"):
            dest = dest[1:-1]
        if _allowed_url(dest):
            return f"[{text}]({dest})"
        # The brackets stay (escaped): bare link text could land at a line start and
        # open a heading, a fence or a table row there.
        label = f"\\[{text}\\]" if text.strip() else ""
        if not dest:
            return label
        return f"{label} ({code_span(dest)})" if label else code_span(dest)
    url = m.group("autourl")
    return f"<{url}>" if _allowed_url(url) else code_span(url)


def _bare_replacement(m: re.Match[str]) -> str:
    url, tail = _split_url_tail(m.group(0))
    if "://" in url and _allowed_url(url) and not url.endswith("--"):  # "-->" is a marker
        # Kept as an explicit <autolink>. A bare URL would be autolinked by GitHub up
        # to the next space, swallowing a backtick that opens a code span here and
        # turning the rest of that span into live Markdown.
        return f"<{url}>" + _plain(tail)
    return code_span(url) + _plain(tail)


def _append(out: list[str], piece: str) -> None:
    """Append ``piece``; keep a generated code span from being escaped or merged."""
    if out and piece.startswith(("`", "<")):
        prev = out[-1]
        if prev.endswith("\\") or _ends_with_live_backtick(prev):
            out.append(" ")
    if piece:
        out.append(piece)


def _sub_parts(
    pattern: re.Pattern[str],
    text: str,
    on_match: Callable[[re.Match[str]], str],
    on_gap: Callable[[str], str],
) -> str:
    out: list[str] = []
    pos = 0
    for m in pattern.finditer(text):
        _append(out, on_gap(text[pos : m.start()]))
        _append(out, on_match(m))
        pos = m.end()
    _append(out, on_gap(text[pos:]))
    return "".join(out)


def _inline(text: str) -> str:
    """Inline transforms on prose text that contains no code spans."""
    text = _IMAGE.sub("[", text)
    return _sub_parts(
        _LINK,
        text,
        _link_replacement,
        _bare_urls,
    )


def _bare_urls(text: str) -> str:
    return _sub_parts(_BARE_URL, text, _bare_replacement, _plain)


def _escaped(text: str, pos: int) -> bool:
    """True when ``text[pos]`` is preceded by an odd number of backslashes."""
    count = 0
    while pos - count > 0 and text[pos - count - 1] == "\\":
        count += 1
    return count % 2 == 1


def _split_code_spans(text: str, *, pipes: bool) -> list[tuple[str, bool]]:
    """Split prose into (chunk, is_code_span) parts using CommonMark backtick matching.

    A backslash-escaped backtick cannot open a span; inside a span backslashes are
    literal, so a span closes at the next whole backtick run of the same length.

    Spans never cross a line break (or a ``|`` when ``pipes``). CommonMark lets a span
    run over lines of one paragraph, but a blank line, a list item or a table cell ends
    it; matching per line can only miss a span (its backticks are then escaped), never
    keep text verbatim that GitHub renders as prose.
    """
    if "\n" in text or (pipes and "|" in text):
        split: list[tuple[str, bool]] = []
        for piece in re.split(r"(\n|\|)" if pipes else r"(\n)", text):
            if piece in ("\n", "|"):
                split.append((piece, False))
            elif piece:
                split.extend(_split_code_spans(piece, pipes=False))
        return _merge_prose(split)
    runs: dict[int, list[int]] = {}  # run length -> start offsets, ascending
    for m in _BACKTICK_RUN.finditer(text):
        runs.setdefault(m.end() - m.start(), []).append(m.start())
    parts: list[tuple[str, bool]] = []
    pos = 0
    start = 0
    n = len(text)
    while pos < n:
        if text[pos] != "`":
            pos += 1
            continue
        if _escaped(text, pos):
            pos += 1
            continue
        run_end = pos
        while run_end < n and text[run_end] == "`":
            run_end += 1
        run = run_end - pos
        candidates = runs.get(run, [])
        i = bisect.bisect_left(candidates, run_end)
        if i == len(candidates):  # an unmatched run is literal text
            pos = run_end
            continue
        close_end = candidates[i] + run
        if pos > start:
            parts.append((text[start:pos], False))
        parts.append((text[pos:close_end], True))
        pos = start = close_end
    if start < n:
        parts.append((text[start:], False))
    return parts


def _merge_prose(parts: list[tuple[str, bool]]) -> list[tuple[str, bool]]:
    """Join neighbouring prose parts, so links and URLs are matched across them."""
    out: list[tuple[str, bool]] = []
    prose: list[str] = []  # joined once, so many short lines stay linear
    for chunk, is_code in parts:
        if not is_code:
            prose.append(chunk)
            continue
        if prose:
            out.append(("".join(prose), False))
            prose = []
        out.append((chunk, True))
    if prose:
        out.append(("".join(prose), False))
    return out


def _table_capable(text: str) -> bool:
    """True when some line could be a GFM table delimiter row (``|---|``, or ``:-`` under
    a one-column header)."""
    return any(
        "-" in line and ("|" in line or ":" in line) and _TABLE_DELIM.match(line)
        for line in text.split("\n")
    )


def _ends_with_live_backtick(text: str) -> bool:
    return text.endswith("`") and not _escaped(text, len(text) - 1)


def _prose(text: str, *, blocks: bool) -> str:
    if blocks:
        text = _ATX.sub(lambda m: m.group(1) + "\\" + m.group(2), text)
        text = _SETEXT.sub(lambda m: m.group(1) + "\\" + m.group(2) + m.group(3), text)
        text = _REF_DEF.sub(lambda m: m.group(1) + "\\[", text)
        text = _TILDE_FENCE.sub(lambda m: m.group(1) + "\\" + m.group(2), text)
        text = _BLANKS.sub("\n\n", text)
    pieces: list[str] = []
    # GFM splits table rows into cells before it parses code spans.
    pipes = blocks and _table_capable(text)
    for chunk, is_code in _split_code_spans(text, pipes=pipes):
        piece = chunk if is_code else _inline(chunk)
        # Two adjacent spans would merge into one longer backtick run and pair wrongly.
        if pieces and piece.startswith("`") and _ends_with_live_backtick(pieces[-1]):
            pieces.append(" ")
        pieces.append(piece)
    return "".join(pieces)


def _fence_line(fence: str, info: str) -> str:
    """The opening fence line with only a plain language word kept.

    GitHub takes the language from the first word (split at spaces and tabs) after
    entity and backslash decoding, so anything but a plain word is dropped, and
    languages it renders specially (``suggestion``...) become ``text``.
    """
    words = info.split()
    if not words:
        return fence
    lang = words[0]
    if lang.lower() in _SPECIAL_FENCES or not _SAFE_LANG.match(lang):
        lang = "text"
    return f"{fence} {lang}" if lang == "text" else f"{fence}{lang}"


def _dedent(line: str, width: int) -> str:
    """Remove up to ``width`` leading spaces (what CommonMark does inside an indented fence)."""
    stripped = len(line) - len(line.lstrip(" "))
    return line[min(stripped, width) :]


def _blocks(text: str) -> str:
    """Apply prose rules outside fenced code blocks; keep code verbatim; close open fences."""
    out: list[str] = []
    prose: list[str] = []
    fence: str | None = None  # the opening fence while inside a code block
    indent = 0  # indentation of the opening fence in the input
    for line in text.split("\n"):
        if fence is None:
            m = _FENCE_OPEN.match(line)
            if m and not (m.group(2)[0] == "`" and "`" in m.group(3)):
                if prose:
                    out.append(_prose("\n".join(prose), blocks=True))
                    prose = []
                fence, indent = m.group(2), len(m.group(1))
                # The fence is written at column 0, the code dedented to match. An
                # indented fence can belong to a list item on GitHub and end with it,
                # which would turn the rest of the "code" into live prose; at column 0
                # it is a top-level block both here and on GitHub.
                out.append(_fence_line(fence, m.group(3)))
            else:
                prose.append(line)
            continue
        body = _dedent(line, indent)
        out.append(body)
        # Decide on the line as written, because that is what GitHub parses.
        close = _FENCE_CLOSE.match(body)
        if close and close.group(1)[0] == fence[0] and len(close.group(1)) >= len(fence):
            fence = None
    if prose:
        out.append(_prose("\n".join(prose), blocks=True))
    if fence is not None:
        out.append(fence)
    return "\n".join(out)


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return strip_markers(_CONTROL.sub("", text))


def _fit(text: str, limit: int, transform: Callable[[str], str]) -> str:
    """``transform(text)``, or if that is longer than ``limit``, the transform of the longest
    raw prefix (plus an ellipsis) that fits.

    The raw input is cut, never the transformed output, so a cut through a code span or
    a URL is sanitised again instead of exposing its content as live Markdown.
    """
    if limit <= 0:
        return ""
    out = transform(text)
    if len(out) <= limit:
        return out
    best = ""
    lo, hi = 0, len(text) - 1
    while lo <= hi:  # binary search; transform is close to monotonic in the prefix length
        mid = (lo + hi) // 2
        candidate = transform(text[:mid].rstrip() + ELLIPSIS)
        if len(candidate) <= limit:
            best, lo = candidate, mid + 1
        else:
            hi = mid - 1
    return best


def sanitize_md(text: str, limit: int) -> str:
    """Sanitise untrusted Markdown for a GitHub comment; at most ``limit`` characters."""
    return _fit(text, limit, lambda t: _blocks(_clean(t)))


def sanitize_inline(text: str, limit: int) -> str:
    """Sanitise untrusted text for one line (a title or a table cell).

    Newlines and runs of whitespace become one space, no block syntax is recognised
    (fences, headings), and ``|`` is escaped so the text cannot split a table cell.
    """

    def transform(t: str) -> str:
        flat = " ".join(_clean(t).split())
        # The text may be placed at a line start: escape what would open a block there
        # (heading, fence, quote, list item, setext underline).
        if flat[:1] in _BLOCK_START:
            flat = "\\" + flat
        flat = _ORDERED_START.sub(r"\1\\\2", flat)
        # Escape pipes first: GFM splits table cells before any inline parsing, even
        # inside code spans, and an escaped pipe also ends a bare URL for our matcher.
        return _prose(_PIPE.sub(r"\1\\|", flat), blocks=False)

    return _fit(text, limit, transform)


_LINE_BREAK = re.compile(r"\r\n|\r|\n")  # CommonMark line endings


def _closes_fence(text: str, fence: str) -> bool:
    """Some line of ``text`` would end a code block opened with ``fence``.

    A closing fence (CommonMark 4.5) is at most three spaces of indentation, a run of
    the fence character at least as long as the opening fence, then only blanks.
    """
    if len(fence) < 3 or set(fence) not in ({"`"}, {"~"}):
        raise ValueError(f"not a code fence: {fence!r}")
    closer = re.compile(rf"^ {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*$")
    return any(closer.match(line) for line in _LINE_BREAK.split(text))


def suggestion_hazards(text: str, *, fence: str | None = None) -> list[str]:
    """Reasons why replacement code must not be posted as a suggestion block.

    ADDENDUM E rule 5, relaxed for code: the text is posted verbatim inside a fenced
    block, where GitHub links no ``@mention``, ``#123`` reference or URL. So
    ``@decorators``, ``#`` comments and URLs are fine; the hazards are

    - a marker (``<!--``, ``-->``, ``morning-star-review:``), which could forge or
      break the markers this tool finds its comments by;
    - a line that would close ``fence``, the fence the block is posted in: the rest
      of the text would then render as live Markdown. With ``fence=None`` the caller
      uses a fence longer than any backtick run in ``text`` (``review.suggest
      .fence_for``), which no line can close.

    The prose rewrites of ``sanitize_md`` are not applied: inside a fenced block they
    would only corrupt valid code. Empty list = no hazard.
    """
    reasons: list[str] = []
    if strip_markers(text) != text:
        reasons.append("contains a marker")
    if fence is not None and _closes_fence(text, fence):
        reasons.append("contains a line that would close the suggestion fence")
    return reasons
