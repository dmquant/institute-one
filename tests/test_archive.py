"""Archive: workspace snapshot, FTS search, traversal rejection."""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.institute import archive, sessions


async def _seed_session() -> dict:
    session = await sessions.create_session("archive test", kind="research")
    ws = sessions.workspace_path(session)
    (ws / "report.md").write_text(
        "# 深度报告\n\n## 核心结论\nzebrafish42 是关键标记词。\n", encoding="utf-8"
    )
    (ws / "notes.txt").write_text("plain text note with zebrafish42", encoding="utf-8")
    (ws / "data.bin").write_bytes(b"\x00\x01binary, not archived")
    (ws / ".hidden.md").write_text("dotfiles are skipped", encoding="utf-8")
    return session


async def test_snapshot_session_archives_text_files():
    session = await _seed_session()
    archived = await archive.snapshot_session(session["id"], "research", "item1")

    assert sorted(archived) == ["research/item1/notes.txt", "research/item1/report.md"]
    base = get_settings().archive_dir
    for rel in archived:
        assert (base / rel).is_file()
    assert not (base / "research/item1/data.bin").exists()
    assert not (base / "research/item1/.hidden.md").exists()

    rows = await archive.list_files(ref_kind="research", ref_id="item1")
    assert {r["path"] for r in rows} == set(archived)

    # unchanged files are skipped on a re-snapshot
    assert await archive.snapshot_session(session["id"], "research", "item1") == []


async def test_search_finds_token():
    session = await _seed_session()
    await archive.snapshot_session(session["id"], "research", "item2")

    hits = await archive.search("zebrafish42")
    assert hits, "FTS search found nothing"
    assert all("zebrafish42" in h["snippet"].lower().replace("<b>", "").replace("</b>", "")
               for h in hits)
    assert any(h["path"] == "research/item2/report.md" for h in hits)

    # garbage queries return empty instead of raising
    assert await archive.search("") == []
    assert await archive.search('"unbalanced AND (') == []


async def test_read_file_rejects_traversal():
    session = await _seed_session()
    await archive.snapshot_session(session["id"], "research", "item3")

    text = await archive.read_file("research/item3/report.md")
    assert "zebrafish42" in text

    with pytest.raises(ValueError):
        await archive.read_file("../outside.md")
    with pytest.raises(ValueError):
        await archive.read_file("research/item3/../../../etc/passwd")
    with pytest.raises(FileNotFoundError):
        await archive.read_file("research/item3/nope.md")
