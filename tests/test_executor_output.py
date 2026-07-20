"""Byte-aware tasks.output truncation (executor.truncate_output) + compact_error."""
from __future__ import annotations

from app.config import get_settings
from app.router import executor
from app.router.executor import TRUNCATION_MARKER, compact_error, truncate_output


def test_truncate_noop_under_cap():
    assert truncate_output("short", 100) == "short"
    assert truncate_output("", 100) == ""


def test_truncate_counts_bytes_not_chars():
    # 40 CJK chars = 120 UTF-8 bytes; the old char slice would keep all 40
    text = "研" * 40
    out = truncate_output(text, 60)
    assert out.endswith(TRUNCATION_MARKER)
    assert len(out.encode("utf-8")) <= 60
    # never splits a code point: the kept prefix is intact CJK
    kept = out.removesuffix(TRUNCATION_MARKER)
    assert set(kept) == {"研"}


def test_truncate_exact_cap_is_noop():
    text = "x" * 50
    assert truncate_output(text, 50) == text


def test_truncate_tiny_cap_never_exceeds_cap():
    """cap smaller than the marker: degrade to a bare head slice, still <= cap."""
    marker_bytes = len(TRUNCATION_MARKER.encode("utf-8"))
    text = "深度研究" * 10
    for cap in (0, 1, 2, 3, marker_bytes - 1, marker_bytes):
        out = truncate_output(text, cap)
        assert len(out.encode("utf-8")) <= cap, f"cap={cap} overflowed"
        assert TRUNCATION_MARKER not in out
    # ASCII head survives when it fits the tiny cap
    assert truncate_output("abcdef" * 100, 3) == "abc"
    # a multi-byte code point split by the cut is dropped, not mangled
    assert truncate_output("研" * 100, 2) == ""

    # one byte above the marker: room for exactly one ASCII char + marker
    out = truncate_output("a" * 100, marker_bytes + 1)
    assert out == "a" + TRUNCATION_MARKER
    assert len(out.encode("utf-8")) == marker_bytes + 1


async def test_submit_caps_output_bytewise_with_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "output_cap_bytes", 64)
    task = await executor.submit("echo", "深度研究" * 200, source="test", workspace=tmp_path)
    assert task.status == "completed"
    assert task.output.endswith(TRUNCATION_MARKER)
    assert len(task.output.encode("utf-8")) <= 64


def test_compact_error_keeps_first_and_last_lines():
    text = "FIRST line marker\n" + ("x" * 2000) + "\nLAST line marker"
    out = compact_error(text, cap=200)
    assert len(out) <= 200
    assert out.startswith("FIRST line marker")
    assert out.rstrip().endswith("LAST line marker")
    assert "…" in out

    # short text passes through untouched
    assert compact_error("short", cap=200) == "short"

    # a single overlong line still yields head + tail, no duplication
    single = "y" * 3000
    out2 = compact_error(single, cap=100)
    assert len(out2) <= 100
    assert "…" in out2
