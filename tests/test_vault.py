"""Vault writer: managed frontmatter, skip-if-unchanged, conflict siblings, doctor."""
from __future__ import annotations

import shutil

import pytest

from app import bus, db
from app.institute import operator
from app.vault.writer import REGION_BEGIN, REGION_END, get_writer


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


async def test_doctor_scan_runs_off_the_event_loop(monkeypatch):
    """POST /api/vault/doctor awaits writer.doctor() on the event loop, so the
    full-vault read + SHA scan must run in a worker thread (sharing the sweep's
    classification) — a big vault otherwise freezes SSE / /api/ask / scheduler
    ticks for seconds. A deliberately slowed read must leave the loop breathing."""
    import asyncio
    import threading
    import time

    writer = get_writer()
    rel = await writer.write_note(
        "Doctor/loop.md", {}, "v1", artifact_kind="memory", artifact_id="dl1", region=True,
    )
    p = writer.root / rel
    p.write_text(p.read_text(encoding="utf-8").replace("v1", "v1 人工改动"),
                 encoding="utf-8")               # region edited -> drifted

    real_read = operator._read_exact
    event_loop_thread = threading.get_ident()
    read_threads = []

    def slow_read(path):
        read_threads.append(threading.get_ident())
        time.sleep(0.2)  # a big vault, compressed into one slow region read
        return real_read(path)

    monkeypatch.setattr(operator, "_read_exact", slow_read)

    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    t = asyncio.create_task(ticker())
    try:
        counts = await writer.doctor()
    finally:
        t.cancel()
    assert counts["drifted"] == 1          # counts still authoritative
    assert read_threads and all(tid != event_loop_thread for tid in read_threads)
    # blocked-loop behaviour yields ~0 ticks during the 0.2s read; a threaded
    # scan yields ~20 — a generous threshold keeps the test load-tolerant
    assert ticks >= 3


async def test_unsafe_paths_rejected():
    writer = get_writer()
    with pytest.raises(ValueError):
        await writer.write_note("../escape.md", {}, "x", artifact_kind="report", artifact_id="x")
    with pytest.raises(ValueError):
        await writer.write_note("/abs.md", {}, "x", artifact_kind="report", artifact_id="x")


# ---- managed regions (rule 4) ------------------------------------------------

async def test_region_write_creates_marked_note():
    writer = get_writer()
    rel = await writer.write_note(
        "Region/fresh.md", {"title": "记忆"}, "区域正文 v1。",
        artifact_kind="memory", artifact_id="m1", region=True,
    )
    assert rel == "Region/fresh.md"
    text = (writer.root / rel).read_text(encoding="utf-8")
    assert "managed: institute" in text
    begin, end = text.index(REGION_BEGIN), text.index(REGION_END)
    assert begin < text.index("区域正文 v1。") < end

    row = await db.query_one("SELECT * FROM vault_index WHERE path = ?", (rel,))
    assert row["mode"] == "region" and row["state"] == "clean"


async def test_region_rewrite_preserves_outside_annotations():
    writer = get_writer()
    rel = await writer.write_note(
        "Region/annotated.md", {"title": "记忆"}, "机构区域 v1。",
        artifact_kind="memory", artifact_id="m2", region=True,
    )
    target = writer.root / rel
    text = target.read_text(encoding="utf-8")
    # human notes both above and below the managed region
    annotated = text.replace(REGION_BEGIN, "上方人工批注。\n\n" + REGION_BEGIN)
    annotated += "\n下方人工批注。\n"
    target.write_text(annotated, encoding="utf-8")

    rel2 = await writer.write_note(
        "Region/annotated.md", {"title": "记忆"}, "机构区域 v2。",
        artifact_kind="memory", artifact_id="m2", region=True,
    )
    assert rel2 == rel  # in place, no sibling
    text2 = target.read_text(encoding="utf-8")
    assert "上方人工批注。" in text2 and "下方人工批注。" in text2
    assert "机构区域 v2。" in text2 and "机构区域 v1。" not in text2
    assert [p.name for p in target.parent.iterdir()] == ["annotated.md"]


async def test_region_skip_if_unchanged_keeps_annotations():
    writer = get_writer()
    rel = await writer.write_note(
        "Region/stable.md", {}, "不变的区域。", artifact_kind="memory", artifact_id="m3", region=True,
    )
    target = writer.root / rel
    target.write_text(target.read_text(encoding="utf-8") + "\n批注。\n", encoding="utf-8")
    mtime = target.stat().st_mtime_ns

    rel2 = await writer.write_note(
        "Region/stable.md", {}, "不变的区域。", artifact_kind="memory", artifact_id="m3", region=True,
    )
    assert rel2 == rel
    assert target.stat().st_mtime_ns == mtime  # rule (d): no disk write
    assert "批注。" in target.read_text(encoding="utf-8")


async def test_region_inside_edit_yields_conflict_sibling():
    writer = get_writer()
    rel = await writer.write_note(
        "Region/edited-inside.md", {}, "机构区域 v1。",
        artifact_kind="memory", artifact_id="m4", region=True,
    )
    target = writer.root / rel
    human_text = target.read_text(encoding="utf-8").replace("机构区域 v1。", "人工改写的区域。")
    target.write_text(human_text, encoding="utf-8")

    new_rel = await writer.write_note(
        "Region/edited-inside.md", {}, "机构区域 v2。",
        artifact_kind="memory", artifact_id="m4", region=True,
    )
    assert new_rel != rel and "(institute update " in new_rel
    assert target.read_text(encoding="utf-8") == human_text  # original untouched
    sibling = (writer.root / new_rel).read_text(encoding="utf-8")
    assert "机构区域 v2。" in sibling and REGION_BEGIN in sibling

    assert (await db.query_one("SELECT state FROM vault_index WHERE path = ?", (rel,)))["state"] == "conflict"
    assert (await db.query_one("SELECT state, mode FROM vault_index WHERE path = ?", (new_rel,))) == {
        "state": "clean", "mode": "region",
    }
    events = await bus.replay(0, types=["vault.conflict"])
    assert any(e.ref_id == rel for e in events)


async def test_region_on_foreign_unmarked_file_yields_sibling():
    writer = get_writer()
    # a hand-created, never-ledgered file already sits at the path
    target = writer.root / "Region" / "foreign.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("人工自建笔记，无标记。\n", encoding="utf-8")

    new_rel = await writer.write_note(
        "Region/foreign.md", {}, "机构内容。", artifact_kind="memory", artifact_id="m5", region=True,
    )
    assert new_rel != "Region/foreign.md" and "(institute update " in new_rel
    assert target.read_text(encoding="utf-8") == "人工自建笔记，无标记。\n"
    # the path was never ours -> no ledger row, and none was fabricated
    assert await db.query_one("SELECT * FROM vault_index WHERE path = ?", ("Region/foreign.md",)) is None
    assert (await db.query_one("SELECT mode FROM vault_index WHERE path = ?", (new_rel,)))["mode"] == "region"


async def test_region_upgrades_clean_whole_file_note_in_place():
    writer = get_writer()
    rel = await writer.write_note(
        "Region/upgrade.md", {"title": "旧全量"}, "旧全量正文。",
        artifact_kind="memory", artifact_id="m6",
    )
    assert (await db.query_one("SELECT mode FROM vault_index WHERE path = ?", (rel,)))["mode"] == "file"

    rel2 = await writer.write_note(
        "Region/upgrade.md", {"title": "旧全量"}, "区域化正文。",
        artifact_kind="memory", artifact_id="m6", region=True,
    )
    assert rel2 == rel  # never-edited institute note upgrades in place
    text = (writer.root / rel).read_text(encoding="utf-8")
    assert REGION_BEGIN in text and "区域化正文。" in text and "旧全量正文。" not in text
    assert (await db.query_one("SELECT mode FROM vault_index WHERE path = ?", (rel,)))["mode"] == "region"
    assert [p.name for p in (writer.root / "Region").iterdir() if "upgrade" in p.name] == ["upgrade.md"]


async def test_doctor_region_semantics():
    writer = get_writer()
    rel = await writer.write_note(
        "Region/doctored.md", {}, "区域内容。", artifact_kind="memory", artifact_id="m7", region=True,
    )
    target = writer.root / rel

    # annotations outside the region are NOT drift
    target.write_text(target.read_text(encoding="utf-8") + "\n人工批注。\n", encoding="utf-8")
    counts = await writer.doctor()
    assert counts == {"total": 1, "clean": 1, "conflict": 0, "missing": 0, "drifted": 0}

    # an edit inside the region IS drift
    text = target.read_text(encoding="utf-8")
    target.write_text(text.replace("区域内容。", "被人工改掉。"), encoding="utf-8")
    assert (await writer.doctor())["drifted"] == 1

    # removing a marker is drift too (the region can no longer be located)
    target.write_text(text.replace(REGION_END, ""), encoding="utf-8")
    assert (await writer.doctor())["drifted"] == 1


# ---- REVIEW-B3 boundary probes -------------------------------------------------

async def test_region_same_day_second_conflict_never_reuses_sibling():
    """B3-H2: a human-edited conflict sibling from earlier the same day must
    survive the next conflict — sibling names are always fresh."""
    writer = get_writer()
    target = writer.root / "Region" / "reuse.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("人工文件，无标记。\n", encoding="utf-8")

    first_sibling = await writer.write_note(
        "Region/reuse.md", {}, "机构 v1。", artifact_kind="memory", artifact_id="m8", region=True,
    )
    assert "(institute update " in first_sibling
    sibling_path = writer.root / first_sibling
    human_sibling = sibling_path.read_text(encoding="utf-8") + "\n人工在副本上的编辑。\n"
    sibling_path.write_text(human_sibling, encoding="utf-8")

    second_sibling = await writer.write_note(
        "Region/reuse.md", {}, "机构 v2。", artifact_kind="memory", artifact_id="m8", region=True,
    )
    assert second_sibling != first_sibling                      # fresh name
    assert sibling_path.read_text(encoding="utf-8") == human_sibling  # edit survived
    assert "机构 v2。" in (writer.root / second_sibling).read_text(encoding="utf-8")
    assert target.read_text(encoding="utf-8") == "人工文件，无标记。\n"


async def test_region_ownership_removal_forces_conflict():
    """B3-H3: a note whose 'managed: institute' line was edited away is no
    longer ours — updates must divert to a sibling, and doctor must flag it."""
    writer = get_writer()
    rel = await writer.write_note(
        "Region/owned.md", {}, "机构 v1。", artifact_kind="memory", artifact_id="m9", region=True,
    )
    target = writer.root / rel
    stripped = target.read_text(encoding="utf-8").replace("managed: institute\n", "")
    target.write_text(stripped, encoding="utf-8")

    assert (await writer.doctor())["drifted"] == 1  # lost ownership is drift

    new_rel = await writer.write_note(
        "Region/owned.md", {}, "机构 v2。", artifact_kind="memory", artifact_id="m9", region=True,
    )
    assert new_rel != rel and "(institute update " in new_rel
    assert target.read_text(encoding="utf-8") == stripped  # not touched in place
    assert "managed: institute" in (writer.root / new_rel).read_text(encoding="utf-8")
    assert (await db.query_one("SELECT state FROM vault_index WHERE path = ?", (rel,)))["state"] == "conflict"


async def test_region_whitespace_only_edit_is_a_human_edit():
    """B3-H4: edge whitespace inside the region is a real edit — never judged
    'unchanged', never overwritten in place."""
    writer = get_writer()
    rel = await writer.write_note(
        "Region/ws.md", {}, "空白敏感正文。", artifact_kind="memory", artifact_id="m10", region=True,
    )
    target = writer.root / rel
    padded = target.read_text(encoding="utf-8").replace("空白敏感正文。", "  空白敏感正文。\n")
    target.write_text(padded, encoding="utf-8")

    # same institute body again: NOT skip-if-unchanged (disk region differs) …
    new_rel = await writer.write_note(
        "Region/ws.md", {}, "空白敏感正文。", artifact_kind="memory", artifact_id="m10", region=True,
    )
    # … and not an in-place overwrite either: the padded human text survives
    assert new_rel != rel and "(institute update " in new_rel
    assert target.read_text(encoding="utf-8") == padded


async def test_region_update_preserves_crlf_outside_the_region():
    """B3-H4: bytes outside the markers — including CRLF line endings — survive
    an in-place region update exactly."""
    writer = get_writer()
    rel = await writer.write_note(
        "Region/crlf.md", {}, "区域 v1。", artifact_kind="memory", artifact_id="m11", region=True,
    )
    target = writer.root / rel
    with open(target, "a", encoding="utf-8", newline="") as f:
        f.write("\r\nCRLF 批注一行。\r\n")

    rel2 = await writer.write_note(
        "Region/crlf.md", {}, "区域 v2。", artifact_kind="memory", artifact_id="m11", region=True,
    )
    assert rel2 == rel  # annotation outside the region: still an in-place update
    with open(target, "r", encoding="utf-8", newline="") as f:
        raw = f.read()
    assert "\r\nCRLF 批注一行。\r\n" in raw   # CRLF bytes untouched
    assert "区域 v2。" in raw and "区域 v1。" not in raw

    counts = await writer.doctor()
    assert counts["drifted"] == 0 and counts["clean"] == 1


async def test_region_malformed_markers_divert_to_sibling():
    """B3-M1: nested / duplicated / out-of-order markers are not a region —
    the file counts as human-edited and takes the conflict path."""
    writer = get_writer()
    cases = {
        "nested": f"{REGION_BEGIN}\n正文\n{REGION_BEGIN}\n人工\n{REGION_END}\n{REGION_END}\n",
        "duplicated": f"{REGION_BEGIN}\n一\n{REGION_END}\n{REGION_BEGIN}\n二\n{REGION_END}\n",
        "end_first": f"{REGION_END}\n人工\n{REGION_BEGIN}\n",
    }
    for name, text in cases.items():
        rel = await writer.write_note(
            f"Region/{name}.md", {}, "初版。", artifact_kind="memory", artifact_id=name, region=True,
        )
        target = writer.root / rel
        target.write_text(text, encoding="utf-8")
        assert (await writer.doctor())["drifted"] >= 1, name  # malformed markers = drift

        new_rel = await writer.write_note(
            f"Region/{name}.md", {}, "重写版。", artifact_kind="memory", artifact_id=name, region=True,
        )
        assert new_rel != rel, name                             # sibling, not in place
        assert target.read_text(encoding="utf-8") == text, name  # malformed file untouched
        row = await db.query_one("SELECT state FROM vault_index WHERE path = ?", (rel,))
        assert row["state"] == "conflict", name


async def test_doctor_region_handles_non_utf8_without_raising():
    """B3-M2: an undecodable file counts as drifted; doctor never raises."""
    writer = get_writer()
    rel = await writer.write_note(
        "Region/binary.md", {}, "文本内容。", artifact_kind="memory", artifact_id="m12", region=True,
    )
    (writer.root / rel).write_bytes(b"\xff\xfe\x00\x01 not utf-8")

    counts = await writer.doctor()
    assert counts["drifted"] == 1 and counts["missing"] == 0
