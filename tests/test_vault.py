"""Vault writer: managed frontmatter, skip-if-unchanged, conflict siblings, doctor."""
from __future__ import annotations

import shutil

import pytest

from app import bus, db
from app.vault.writer import get_writer


@pytest.fixture(autouse=True)
def clean_vault_dir():
    """The vault tmp dir outlives the per-test DB wipe; keep disk and ledger in sync."""
    writer = get_writer()
    assert writer.enabled and writer.root is not None
    shutil.rmtree(writer.root, ignore_errors=True)
    writer.root.mkdir(parents=True, exist_ok=True)
    yield


async def test_write_note_creates_managed_note():
    writer = get_writer()
    rel = await writer.write_note(
        "Reports/test-note.md", {"title": "测试报告", "tags": ["alpha"]},
        "# 正文\n\n核心观点。", artifact_kind="report", artifact_id="r1",
    )
    assert rel == "Reports/test-note.md"
    path = writer.root / rel
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "managed: institute" in text
    assert "type: report" in text
    assert "institute/report" in text  # own tag merged in
    assert "核心观点。" in text

    row = await db.query_one("SELECT * FROM vault_index WHERE path = ?", (rel,))
    assert row["state"] == "clean"
    assert row["artifact_kind"] == "report"


async def test_unchanged_rewrite_is_noop():
    writer = get_writer()
    rel = await writer.write_note(
        "Reports/stable.md", {"title": "稳定"}, "不变的内容。",
        artifact_kind="report", artifact_id="r2",
    )
    row_before = await db.query_one("SELECT sha256, written_at FROM vault_index WHERE path = ?", (rel,))
    mtime_before = (writer.root / rel).stat().st_mtime_ns

    rel2 = await writer.write_note(
        "Reports/stable.md", {"title": "稳定"}, "不变的内容。",
        artifact_kind="report", artifact_id="r2",
    )
    assert rel2 == rel
    assert (writer.root / rel).stat().st_mtime_ns == mtime_before  # no disk write
    row_after = await db.query_one("SELECT sha256, written_at FROM vault_index WHERE path = ?", (rel,))
    assert row_after == row_before  # no ledger churn


async def test_human_edit_yields_conflict_sibling():
    writer = get_writer()
    rel = await writer.write_note(
        "Reports/edited.md", {"title": "原稿"}, "机构版本 v1。",
        artifact_kind="report", artifact_id="r3",
    )
    target = writer.root / rel
    human_text = target.read_text(encoding="utf-8") + "\n人工补充的一段。\n"
    target.write_text(human_text, encoding="utf-8")

    new_rel = await writer.write_note(
        "Reports/edited.md", {"title": "原稿"}, "机构版本 v2。",
        artifact_kind="report", artifact_id="r3",
    )
    assert new_rel != rel
    assert "(institute update " in new_rel
    # the human-edited original was never overwritten
    assert target.read_text(encoding="utf-8") == human_text
    sibling = writer.root / new_rel
    assert sibling.is_file()
    assert "机构版本 v2。" in sibling.read_text(encoding="utf-8")

    orig_row = await db.query_one("SELECT state FROM vault_index WHERE path = ?", (rel,))
    assert orig_row["state"] == "conflict"
    new_row = await db.query_one("SELECT state FROM vault_index WHERE path = ?", (new_rel,))
    assert new_row["state"] == "clean"

    events = await bus.replay(0, types=["vault.conflict"])
    assert any(e.ref_id == rel for e in events)


async def test_doctor_counts():
    writer = get_writer()
    clean_rel = await writer.write_note(
        "Doctor/clean.md", {}, "干净。", artifact_kind="report", artifact_id="d1"
    )
    assert clean_rel

    conflict_rel = await writer.write_note(
        "Doctor/conflict.md", {}, "v1", artifact_kind="report", artifact_id="d2"
    )
    (writer.root / conflict_rel).write_text("human edit", encoding="utf-8")
    sibling_rel = await writer.write_note(
        "Doctor/conflict.md", {}, "v2", artifact_kind="report", artifact_id="d2"
    )

    missing_rel = await writer.write_note(
        "Doctor/missing.md", {}, "将被删除。", artifact_kind="report", artifact_id="d3"
    )
    (writer.root / missing_rel).unlink()

    counts = await writer.doctor()
    assert counts["total"] == 4  # clean + conflict-original + sibling + missing
    assert counts["clean"] == 2  # clean.md + the conflict sibling
    assert counts["conflict"] == 1
    assert counts["missing"] == 1
    assert counts["drifted"] == 0
    assert sibling_rel != conflict_rel


async def test_unsafe_paths_rejected():
    writer = get_writer()
    with pytest.raises(ValueError):
        await writer.write_note("../escape.md", {}, "x", artifact_kind="report", artifact_id="x")
    with pytest.raises(ValueError):
        await writer.write_note("/abs.md", {}, "x", artifact_kind="report", artifact_id="x")
