"""Prompt overrides (ROADMAP Phase 2): resolve priority, byte-identical
defaults, lifecycle conditional claims, API roundtrip.

The byte-identity tests hardcode the FORMER inline prompt strings (they do not
import the constants they guard), so a paraphrase in prompts.py fails here —
this is the evidence that lets prompt iteration move to data while keeping
CLAUDE.md rule 4 honest for the no-override path.
"""
from __future__ import annotations

import asyncio
import logging

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app import db
from app.api import prompt_overrides as po_api
from app.institute import prompt_overrides as po
from app.institute import prompts
from app.institute.analysts import get_analyst

# the former inline strings, restated literally (NOT imported from prompts.py)
OLD_CITATION = (
    "【引用规范】所有事实性论断必须给出来源（链接、报告名或数据出处）。无法核实的内容必须明确标注「未经核实」。\n"
    "区分事实与观点：观点用「我认为/判断」开头。数字给出时间点。禁止编造数据。"
)
OLD_DELIVERABLE = (
    "【交付规范】把完整成果写入工作目录下的文件 {filename}（Markdown，中文为主）。"
    "写完后只回复一行：DONE: {filename}"
)
OLD_ANCHOR_PREFIX = "【时间锚点】今天是 "
OLD_ANCHOR_SUFFIX = "（新加坡时间）。所有「最近/目前/今年」均以此为准。"


@pytest.fixture(autouse=True)
def fresh_override_cache():
    """Module-level cache/warn state must not leak across tests (the DB is
    wiped per test by conftest, but module globals are not)."""
    po._cache = None
    po._warned = False
    yield
    po._cache = None
    po._warned = False


def _expected_prompt(analyst, task: str, filename: str) -> str:
    """The pre-override sandwich below the (time-varying) date anchor."""
    return (
        f"你是 {analyst.name}（{analyst.name_en}），AI 研究所的{analyst.focus}。\n{analyst.persona}"
        + f"\n\n## 任务\n{task}"
        + "\n\n" + OLD_CITATION
        + "\n\n" + OLD_DELIVERABLE.format(filename=filename)
    )


def _assert_prompt_is_default(task: str = "任务正文T", filename: str = "out.md") -> None:
    analyst = get_analyst("macro-analyst")
    prompt = prompts.build_analyst_prompt(analyst, task, output_file=filename)
    anchor, sep, rest = prompt.partition("\n\n")
    assert sep == "\n\n"
    assert anchor.startswith(OLD_ANCHOR_PREFIX) and anchor.endswith(OLD_ANCHOR_SUFFIX)
    assert prompts.now_sgt().strftime("%Y-%m-%d") in anchor
    assert "\n" not in anchor
    # ENTIRE remainder byte-identical — nothing inserted anywhere
    assert rest == _expected_prompt(analyst, task, filename)


# ---- resolve priority + byte-identical defaults -------------------------------

async def test_prompt_byte_identical_with_cold_cache(caplog):
    """Never-loaded cache (fresh process, no boot pre-warm): code defaults,
    byte-identical output, exactly ONE warning."""
    assert po._cache is None
    with caplog.at_level(logging.WARNING, logger="institute.prompt_overrides"):
        _assert_prompt_is_default()
        _assert_prompt_is_default()  # second build: no second warning
    warned = [r for r in caplog.records if "never loaded" in r.getMessage()]
    assert len(warned) == 1


async def test_prompt_byte_identical_with_loaded_empty_cache():
    loaded = await po.refresh_cache()
    assert loaded == {}
    _assert_prompt_is_default()


async def test_shadow_recorded_but_never_effective():
    row = await po.create("prompts.citation_mandate", "【替换引用块】测试内容。", note="试运行")
    assert row["status"] == "shadow"
    assert row["created_at"] and row["activated_at"] is None and row["retired_at"] is None
    await po.refresh_cache()
    assert po.resolve("prompts.citation_mandate", "DEFAULT") == "DEFAULT"
    _assert_prompt_is_default()  # the prompt path never sees shadows


async def test_active_override_wins_and_retire_restores_default():
    row = await po.create("prompts.citation_mandate", "【替换引用块】测试内容。")
    active = await po.activate(row["id"])
    assert active["status"] == "active" and active["activated_at"]

    analyst = get_analyst("macro-analyst")
    prompt = prompts.build_analyst_prompt(analyst, "任务正文T", output_file="out.md")
    assert "【替换引用块】测试内容。" in prompt
    assert OLD_CITATION not in prompt

    retired = await po.retire(row["id"])
    assert retired["status"] == "retired" and retired["retired_at"]
    _assert_prompt_is_default()  # back to byte-identical defaults


async def test_templated_override_renders_fields():
    row = await po.create(
        "prompts.file_deliverable", "【新交付】写入 {filename}，回 OK: {filename}",
    )
    await po.activate(row["id"])
    analyst = get_analyst("macro-analyst")
    prompt = prompts.build_analyst_prompt(analyst, "任务", output_file="x.md")
    assert prompt.endswith("【新交付】写入 x.md，回 OK: x.md")


async def test_render_falls_back_on_broken_active_content():
    """Validation bypassed via a manual DB edit: the prompt path must degrade
    to the exact default rendering instead of raising."""
    row = await po.create("prompts.file_deliverable", "【新交付】{filename}")
    await po.activate(row["id"])
    await db.execute(
        "UPDATE prompt_overrides SET content = '{bogus}' WHERE id = ?", (row["id"],)
    )
    await po.refresh_cache()
    out = po.render("prompts.file_deliverable", prompts.FILE_DELIVERABLE, filename="f.md")
    assert out == prompts.FILE_DELIVERABLE.format(filename="f.md")


async def test_invalidate_cache_serves_defaults_until_refresh():
    row = await po.create("prompts.citation_mandate", "【替换引用块】")
    await po.activate(row["id"])
    assert po.resolve("prompts.citation_mandate", "DEFAULT") == "【替换引用块】"
    po.invalidate_cache()
    assert po.resolve("prompts.citation_mandate", "DEFAULT") == "DEFAULT"
    await po.refresh_cache()
    assert po.resolve("prompts.citation_mandate", "DEFAULT") == "【替换引用块】"


# ---- validation -----------------------------------------------------------------

async def test_create_validation_rejects_bad_input():
    with pytest.raises(ValueError, match="unknown scope"):
        await po.create("analyst_daily.bogus", "x")
    with pytest.raises(ValueError, match="must not be empty"):
        await po.create("prompts.citation_mandate", "   ")
    with pytest.raises(ValueError, match="unknown placeholders"):
        await po.create("prompts.file_deliverable", "写入 {bogus}")
    with pytest.raises(ValueError, match="invalid format template"):
        await po.create("prompts.file_deliverable", "写入 {filename")
    with pytest.raises(ValueError, match="note exceeds"):
        await po.create("prompts.citation_mandate", "ok", note="n" * (po.MAX_NOTE_LEN + 1))
    # zero-field scopes are literal blocks: braces stay literal, no validation
    row = await po.create("prompts.citation_mandate", "字面 {braces} 不做格式化")
    await po.activate(row["id"])
    assert po.resolve("prompts.citation_mandate", "D") == "字面 {braces} 不做格式化"
    assert po.render("prompts.citation_mandate", "D") == "字面 {braces} 不做格式化"


# ---- lifecycle conditional claims -------------------------------------------------

async def test_activate_atomically_retires_previous_active():
    a = await po.create("prompts.citation_mandate", "版本A")
    b = await po.create("prompts.citation_mandate", "版本B")
    await po.activate(a["id"])
    await po.activate(b["id"])

    a2, b2 = await po.get(a["id"]), await po.get(b["id"])
    assert a2["status"] == "retired" and a2["retired_at"]
    assert b2["status"] == "active"
    actives = await db.query(
        "SELECT id FROM prompt_overrides WHERE scope = ? AND status = 'active'",
        ("prompts.citation_mandate",),
    )
    assert [r["id"] for r in actives] == [b["id"]]
    assert po.resolve("prompts.citation_mandate", "D") == "版本B"


async def test_lifecycle_transitions_are_one_shot():
    row = await po.create("prompts.citation_mandate", "版本A")
    await po.activate(row["id"])
    with pytest.raises(po.OverrideConflict):  # active, not shadow
        await po.activate(row["id"])
    await po.retire(row["id"])
    with pytest.raises(po.OverrideConflict):  # already retired
        await po.retire(row["id"])
    with pytest.raises(po.OverrideConflict):  # retired rows never re-activate
        await po.activate(row["id"])

    draft = await po.create("prompts.citation_mandate", "版本B")
    with pytest.raises(po.OverrideConflict):  # shadow is not active
        await po.retire(draft["id"])
    with pytest.raises(LookupError):
        await po.activate(999999)
    with pytest.raises(LookupError):
        await po.retire(999999)


async def test_concurrent_activate_same_row_single_winner():
    row = await po.create("prompts.citation_mandate", "版本A")
    results = await asyncio.gather(
        po.activate(row["id"]), po.activate(row["id"]), return_exceptions=True,
    )
    ok = [r for r in results if isinstance(r, dict)]
    conflicts = [r for r in results if isinstance(r, po.OverrideConflict)]
    assert len(ok) == 1 and len(conflicts) == 1
    assert (await po.get(row["id"]))["status"] == "active"


async def test_concurrent_activate_two_drafts_leaves_exactly_one_active():
    a = await po.create("prompts.citation_mandate", "版本A")
    b = await po.create("prompts.citation_mandate", "版本B")
    results = await asyncio.gather(
        po.activate(a["id"]), po.activate(b["id"]), return_exceptions=True,
    )
    assert all(isinstance(r, dict) for r in results)  # both claims were valid
    rows = await db.query(
        "SELECT status FROM prompt_overrides WHERE scope = ?",
        ("prompts.citation_mandate",),
    )
    statuses = sorted(r["status"] for r in rows)
    assert statuses == ["active", "retired"]  # the invariant, whoever won


async def test_drafts_mutable_history_immutable():
    row = await po.create("prompts.citation_mandate", "草稿一", note="v1")
    edited = await po.update_draft(row["id"], content="草稿二", note="v2")
    assert edited["content"] == "草稿二" and edited["note"] == "v2"

    await po.activate(row["id"])
    with pytest.raises(po.OverrideConflict):
        await po.update_draft(row["id"], content="改历史")
    with pytest.raises(po.OverrideConflict):
        await po.delete_draft(row["id"])

    draft = await po.create("prompts.citation_mandate", "可删草稿")
    await po.delete_draft(draft["id"])
    assert await po.get(draft["id"]) is None
    with pytest.raises(LookupError):
        await po.delete_draft(draft["id"])


# ---- API roundtrip ------------------------------------------------------------------

def _client() -> AsyncClient:
    app = FastAPI()
    app.include_router(po_api.router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_api_roundtrip_crud_lifecycle_diff():
    async with _client() as client:
        # scopes overview lists every registered mount point with its default
        r = await client.get("/api/prompt-overrides/scopes")
        assert r.status_code == 200
        scopes = {s["scope"]: s for s in r.json()}
        assert set(po.SCOPES) <= set(scopes)
        assert scopes["prompts.citation_mandate"]["default"] == OLD_CITATION
        assert scopes["prompts.citation_mandate"]["active_id"] is None

        # create a shadow draft
        r = await client.post("/api/prompt-overrides", json={
            "scope": "prompts.citation_mandate", "content": "【API引用块】", "note": "试",
        })
        assert r.status_code == 201
        oid = r.json()["id"]
        assert r.json()["status"] == "shadow"

        # diff preview vs the code default
        r = await client.get(f"/api/prompt-overrides/{oid}/diff")
        assert r.status_code == 200
        body = r.json()
        assert body["changed"] is True
        assert body["default"] == OLD_CITATION
        assert "+【API引用块】" in body["diff"]

        # edit the draft
        r = await client.put(f"/api/prompt-overrides/{oid}", json={"content": "【API引用块v2】"})
        assert r.status_code == 200 and r.json()["content"] == "【API引用块v2】"

        # activate → live in prompts; scope query shows it
        r = await client.post(f"/api/prompt-overrides/{oid}/activate")
        assert r.status_code == 200 and r.json()["status"] == "active"
        prompt = prompts.build_analyst_prompt(get_analyst("macro-analyst"), "任务")
        assert "【API引用块v2】" in prompt
        r = await client.get(
            "/api/prompt-overrides",
            params={"scope": "prompts.citation_mandate", "status": "active"},
        )
        assert [row["id"] for row in r.json()] == [oid]
        r = await client.get("/api/prompt-overrides/scopes")
        assert {s["scope"]: s for s in r.json()}["prompts.citation_mandate"]["active_id"] == oid

        # active rows are immutable and undeletable
        assert (await client.put(f"/api/prompt-overrides/{oid}", json={"content": "x"})).status_code == 409
        assert (await client.delete(f"/api/prompt-overrides/{oid}")).status_code == 409

        # retire → defaults restored
        r = await client.post(f"/api/prompt-overrides/{oid}/retire")
        assert r.status_code == 200 and r.json()["status"] == "retired"
        _assert_prompt_is_default()

        # lifecycle conflicts surface as 409, unknown ids as 404
        assert (await client.post(f"/api/prompt-overrides/{oid}/activate")).status_code == 409
        assert (await client.post(f"/api/prompt-overrides/{oid}/retire")).status_code == 409
        assert (await client.get("/api/prompt-overrides/999999")).status_code == 404
        assert (await client.post("/api/prompt-overrides/999999/activate")).status_code == 404

        # validation face: unknown scope / bad placeholder / bad status filter
        r = await client.post("/api/prompt-overrides", json={"scope": "nope.x", "content": "y"})
        assert r.status_code == 400
        r = await client.post("/api/prompt-overrides", json={
            "scope": "prompts.file_deliverable", "content": "{bogus}",
        })
        assert r.status_code == 400
        assert (await client.get("/api/prompt-overrides", params={"status": "live"})).status_code == 400


async def test_api_delete_draft_and_list_lazy_heals_cache():
    async with _client() as client:
        r = await client.post("/api/prompt-overrides", json={
            "scope": "prompts.persona_block", "content": "你是 {name}（{name_en}）：{focus}\n{persona}",
        })
        oid = r.json()["id"]
        assert (await client.delete(f"/api/prompt-overrides/{oid}")).status_code == 204
        assert (await client.get(f"/api/prompt-overrides/{oid}")).status_code == 404
        assert (await client.delete(f"/api/prompt-overrides/{oid}")).status_code == 404

        # a cold cache heals on the first list read (the hand-weights idiom)
        po.invalidate_cache()
        assert (await client.get("/api/prompt-overrides")).status_code == 200
        assert po._cache is not None
