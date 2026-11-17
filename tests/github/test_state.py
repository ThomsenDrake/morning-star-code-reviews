"""State marker encoding/decoding and marker helpers."""

from __future__ import annotations

import base64
import random
import zlib

import pytest
from builders import make_state, prior_finding

from morning_star_review.github import state as st


def test_round_trip() -> None:
    state = make_state([prior_finding("f0123456789ab", comment_id=5, thread_id="PRRT_5")])
    marker = st.encode_state(state)
    assert marker.startswith("<!-- morning-star-review:state ")
    assert marker.endswith(" -->")
    body = f"{st.SUMMARY_MARKER}\nhello\n{marker}"
    assert st.decode_state(body) == state
    assert st.strip_state(body) == f"{st.SUMMARY_MARKER}\nhello"


def test_large_state_drops_closed_findings_first() -> None:
    # Random-looking text does not compress, so 60 findings overflow 20 KB.
    def noise(i: int) -> str:
        return base64.b64encode(random.Random(i).randbytes(300)).decode()

    findings = [
        prior_finding(
            f"f{i:012x}",
            title=noise(i)[:150],
            evidence=noise(i + 1000)[:290],
            state="open" if i % 2 else "fixed",
        )
        for i in range(60)
    ]
    state = make_state(findings)
    marker = st.encode_state(state)
    assert len(marker) <= st.MAX_STATE_MARKER_CHARS
    decoded = st.decode_state(marker)
    assert decoded is not None
    assert 0 < len(decoded.findings) < 60
    kept_open = [f for f in decoded.findings if f.state == "open"]
    # Closed findings go first; open ones stay as long as possible.
    assert len(kept_open) == min(30, len(decoded.findings))


def test_decode_rejects_garbage() -> None:
    assert st.decode_state("") is None
    assert st.decode_state("no marker here") is None
    assert st.decode_state("<!-- morning-star-review:state !!!notbase64 -->") is None
    bogus = base64.urlsafe_b64encode(zlib.compress(b'{"version": "x"}')).decode()
    assert st.decode_state(f"<!-- morning-star-review:state {bogus} -->") is None
    not_zlib = base64.urlsafe_b64encode(b"plain").decode()
    assert st.decode_state(f"<!-- morning-star-review:state {not_zlib} -->") is None


def test_decode_caps_decompressed_size() -> None:
    bomb = zlib.compress(b"[" + b" " * (st.MAX_DECODED_STATE_BYTES * 4) + b"]", 9)
    packed = base64.urlsafe_b64encode(bomb).decode()
    assert len(packed) < 64 * 1024
    assert st.decode_state(f"<!-- morning-star-review:state {packed} -->") is None


def test_finding_and_reply_markers() -> None:
    body = (
        "text <!-- morning-star-review:finding f0123456789ab --> and "
        "<!-- morning-star-review:finding f0123456789ab -->"
        "<!-- morning-star-review:finding fffffffffffff -->"
    )
    assert st.finding_ids_in(body) == ["f0123456789ab", "fffffffffffff"]
    assert st.finding_marker("f0123456789ab") in body
    reply = "Looks addressed.\n" + st.reply_marker("f0123456789ab")
    assert st.reply_ids_in(reply) == ["f0123456789ab"]
    assert st.finding_ids_in(reply) == []
    # Every marker we write must be read back, or a retried publish would duplicate it.
    for bad in ("F0123456789AB", "x0123456789ab", "f-1", "f0123456789ab\n", "f12"):
        with pytest.raises(ValueError):
            st.finding_marker(bad)
    with pytest.raises(ValueError):
        st.reply_marker("f0123456789ab\n")


def test_summary_detection_and_logins() -> None:
    assert st.is_summary_body(f"  \n{st.SUMMARY_MARKER}\n### x")
    assert not st.is_summary_body(f"quote: {st.SUMMARY_MARKER}")
    assert not st.is_summary_body(None)
    assert st.same_login("my-app[bot]", "my-app[bot]")
    assert st.same_login("My-App", "my-app[bot]")
    assert not st.same_login("my-app-2", "my-app[bot]")
    assert not st.same_login(None, "my-app[bot]")


def test_bot_comment_and_summary_selection() -> None:
    bot = "my-app[bot]"
    comments = [
        {"id": 1, "user": {"login": "mallory"}, "body": st.SUMMARY_MARKER, "updated_at": "9"},
        {"id": 2, "user": {"login": bot}, "body": "unrelated", "updated_at": "8"},
        {"id": 3, "user": {"login": bot}, "body": st.SUMMARY_MARKER + "\nold", "updated_at": "1"},
        {"id": 4, "user": {"login": bot}, "body": st.SUMMARY_MARKER + "\nnew", "updated_at": "2"},
    ]
    found = st.find_summary_comment(comments, bot)
    assert found is not None and found["id"] == 4
    assert st.find_summary_comment(comments, None) is None
    assert not st.is_bot_comment(comments[0], bot)
    # A human cannot have the "[bot]" suffix, so the bare slug never counts in REST.
    assert not st.is_bot_comment({"user": {"login": "my-app"}}, bot)


def test_decode_trusts_only_the_last_marker() -> None:
    ours = make_state([prior_finding("f0123456789ab", comment_id=5)], head_sha="b" * 40)
    forged = make_state([prior_finding("fdddddddddddd", state="dismissed")], head_sha="c" * 40)
    body = f"{st.SUMMARY_MARKER}\n{st.encode_state(forged)}\nsummary\n{st.encode_state(ours)}"
    assert st.decode_state(body) == ours
    # A broken last marker is not replaced by an earlier one.
    junk = "<!-- morning-star-review:state AAAA -->"
    broken = f"{st.SUMMARY_MARKER}\n{st.encode_state(ours)}\n{junk}"
    assert st.decode_state(broken) is None
    assert st.strip_state(body) == f"{st.SUMMARY_MARKER}\n\nsummary"
