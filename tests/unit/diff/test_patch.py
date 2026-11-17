"""Patch parsing: GitHub patch strings and local git diffs."""

from __future__ import annotations

from morning_star_review.diff import patch as patch_mod
from morning_star_review.diff.patch import changed_line_count, local_patches, parse_patch

MULTI = """@@ -10,4 +10,5 @@ def handler(request):
     a = 1
-    b = 2
+    b = 3
+    c = 4
     d = 5
     e = 6
@@ -40,3 +41,2 @@ class Thing:
     x = 1
-    y = 2
     z = 3"""


def test_multi_hunk_numbers_head_lines() -> None:
    fp = parse_patch("m.py", MULTI, "modified", None)
    assert len(fp.hunks) == 2
    h1, h2 = fp.hunks
    assert (h1.old_start, h1.old_len, h1.new_start, h1.new_len) == (10, 4, 10, 5)
    assert h1.header == "def handler(request):"
    assert h1.lines == [
        (" ", 10, "    a = 1"),
        ("-", None, "    b = 2"),
        ("+", 11, "    b = 3"),
        ("+", 12, "    c = 4"),
        (" ", 13, "    d = 5"),
        (" ", 14, "    e = 6"),
    ]
    assert h2.lines == [(" ", 41, "    x = 1"), ("-", None, "    y = 2"), (" ", 42, "    z = 3")]
    assert fp.added == 2 and fp.removed == 2 and changed_line_count(fp) == 4
    assert not fp.binary and not fp.no_newline_flags


def test_missing_patch_gives_no_hunks() -> None:
    fp = parse_patch("big.json", None, "modified", None)
    assert fp.hunks == [] and not fp.binary


def test_binary_marker_from_git() -> None:
    patch = "diff --git a/x.png b/x.png\nindex 1..2 100644\nBinary files a/x.png and b/x.png differ"
    fp = parse_patch("x.png", patch, "modified", None)
    assert fp.binary and fp.hunks == []


def test_no_newline_marker() -> None:
    patch = "@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file"
    fp = parse_patch("v", patch, "modified", None)
    assert fp.no_newline_flags
    assert fp.hunks[0].lines == [("-", None, "old"), ("+", 1, "new")]


def test_added_and_deleted_files() -> None:
    added = parse_patch("n.py", "@@ -0,0 +1,2 @@\n+one\n+two", "added", None)
    assert [ln[1] for ln in added.hunks[0].lines] == [1, 2]
    removed = parse_patch("o.py", "@@ -1,2 +0,0 @@\n-one\n-two", "removed", None)
    assert removed.hunks[0].new_len == 0
    assert [ln[1] for ln in removed.hunks[0].lines] == [None, None]


def test_rename_keeps_previous_path() -> None:
    fp = parse_patch("new.py", "@@ -1 +1 @@\n-a\n+b", "renamed", "old.py")
    assert fp.previous_path == "old.py" and fp.status == "renamed"


def test_crlf_lines_are_stripped_of_cr() -> None:
    patch = "@@ -1,2 +1,2 @@\r\n line one\r\n-line two\r\n+line two changed\r"
    fp = parse_patch("n.txt", patch, "modified", None)
    assert fp.hunks[0].lines == [
        (" ", 1, "line one"),
        ("-", None, "line two"),
        ("+", 2, "line two changed"),
    ]


def test_empty_context_line_without_space() -> None:
    # Some producers strip the single space of an empty context line.
    patch = "@@ -1,3 +1,3 @@\n a\n\n-b\n+c"
    fp = parse_patch("e.py", patch, "modified", None)
    assert fp.hunks[0].lines == [(" ", 1, "a"), (" ", 2, ""), ("-", None, "b"), ("+", 3, "c")]


def test_truncated_patch_does_not_invent_lines() -> None:
    patch = "@@ -1,2 +1,2 @@\n a\n+b\n+c\n+d"
    fp = parse_patch("t.py", patch, "modified", None)
    nums = [n for _, n, _ in fp.hunks[0].lines if n is not None]
    assert nums == [1, 2]


def test_headers_before_first_hunk_are_skipped() -> None:
    patch = "diff --git a/x b/x\nindex 1..2 100644\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"
    fp = parse_patch("x", patch, "modified", None)
    assert fp.hunks[0].lines == [("-", None, "a"), ("+", 1, "b")]


def test_local_patches_match_github_patches(make_repo, h) -> None:
    base, head = h.e1_trees()
    repo, (b, hd) = make_repo(base, head)
    files = h.e1_pr_files()
    paths = [f["path"] for f in files]
    local = local_patches(str(repo), b, hd, paths)
    github = h.e1_patches()
    for path, gp in github.items():
        if path == "notes.txt":
            continue  # the fixture's CRLF patch is hand-made; compared below
        assert path in local, path
        assert local[path].hunks == gp.hunks, path
        assert local[path].status == gp.status, path
    assert local["ledger/util.py"].previous_path == "ledger/old_util.py"
    assert local["ledger/util.py"].status == "renamed"
    assert local["ledger/legacy.py"].status == "removed"
    notes = local["notes.txt"].hunks[0].lines
    assert notes[-1] == ("+", 2, "line two changed")


def test_local_patches_filters_and_validates(make_repo) -> None:
    repo, (b, hd) = make_repo({"a.py": "x\n", "b.py": "y\n"}, {"a.py": "x2\n", "b.py": "y2\n"})
    out = local_patches(str(repo), b, hd, ["a.py"])
    assert list(out) == ["a.py"]
    assert local_patches(str(repo), b, hd, []) == {}
    try:
        local_patches(str(repo), "--output=/tmp/x", hd, ["a.py"])
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("option-like revision accepted")


def test_local_patches_binary(make_repo) -> None:
    repo, (b, hd) = make_repo({"i.bin": b"\x00\x01"}, {"i.bin": b"\x00\x02"})
    out = local_patches(str(repo), b, hd, ["i.bin"])
    assert out["i.bin"].binary and out["i.bin"].hunks == []


def test_local_patches_type_change_keeps_one_diff_run(make_repo, h, monkeypatch) -> None:
    # `git diff -p` prints a file -> symlink change as two sections; the pairing with
    # the --raw entries must still line up (and not fall back to one diff per file).
    repo, (base,) = make_repo({"a.py": "x = 1\n", "f.txt": "a\n", "z.py": "y = 1\n"})
    (repo / "f.txt").unlink()
    (repo / "f.txt").symlink_to("a.py")
    (repo / "a.py").write_text("x = 2\n")
    (repo / "z.py").write_text("y = 2\n")
    h.git(repo, "add", "-A")
    h.git(repo, "commit", "-q", "-m", "type change")
    head = h.git(repo, "rev-parse", "HEAD").strip()
    calls: list[list[str]] = []
    real = patch_mod._git_diff

    def counting(repo_dir: str, args: list[str]) -> str:
        calls.append(args)
        return real(repo_dir, args)

    monkeypatch.setattr(patch_mod, "_git_diff", counting)
    out = local_patches(str(repo), base, head, ["a.py", "f.txt", "z.py"])
    assert len(calls) == 2  # --raw and -p, no per-file fallback
    assert out["a.py"].hunks[0].lines[-1] == ("+", 1, "x = 2")
    assert out["z.py"].hunks[0].lines[-1] == ("+", 1, "y = 2")
    tc = out["f.txt"]
    assert tc.status == "changed"
    assert [ln for hk in tc.hunks for ln in hk.lines] == [("-", None, "a"), ("+", 1, "a.py")]
