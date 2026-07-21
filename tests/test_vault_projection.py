"""Vault projection completion (ROADMAP Phase 4, line 142 leftovers).

Two pins, both driven the tests/test_exporter_handlers.py way (synthetic
``bus.Event`` → handler call → note content assertions):

1. **Dataview inline typed relations** on ``Chain/<entity>.md``: every
   recommended relation role renders as a Dataview inline field
   ``role:: [[target-slug]]`` (the field NAME is the relation role verbatim —
   the shipped vocabulary is snake_case ASCII, already a legal Dataview key,
   so the role→field conversion rule is identity; the test enforces that
   shape so a future role with whitespace/``::`` fails loudly and forces a
   sanitization decision instead of silently breaking Dataview).
2. **``## Entities`` footer coverage** for the artifact kinds the exporter
   handlers gained this round: factcheck disputes digest, paper-book journal,
   research tree, bilingual twin. Footers must be idempotent: handlers
   re-render bodies from rows/tasks on every export (never re-read the note),
   so a re-fired event yields exactly one footer.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from app import bus, db
from app.config import get_settings
from app.institute import chain
from app.vault import exporter
from app.vault.writer import get_writer

ANALYST = "chief-strategist"  # first roster entry; stable in catalog/analysts.json


@pytest.fixture(autouse=True)
def clean_vault(app_runtime):
    """Vault files persist across tests (one tmp tree per pytest run) while the
    ledger db is wiped per test — start each test with an empty vault so
    region-mode writes cannot see a ledger-less leftover file (conflict path)."""
    vault = get_settings().vault_dir
    assert vault is not None
    shutil.rmtree(vault, ignore_errors=True)
    vault.mkdir(parents=True, exist_ok=True)
    yield


def _event(type_: str, ref_id: str, payload: dict) -> bus.Event:
    return bus.Event(id=0, type=type_, ref_kind="test", ref_id=ref_id,
                     payload=payload, created_at=bus.now_iso())


def _vault() -> Path:
    root = get_writer().root
    assert root is not None
    return root


def _read_note(rel: str) -> str:
    path = _vault() / rel
    assert path.is_file(), f"expected vault note {rel} (have: {[str(p.relative_to(_vault())) for p in _vault().rglob('*.md')]})"
    return path.read_text(encoding="utf-8")


# ---- gap 1: Dataview inline typed relations on entity notes -------------------

async def test_entity_note_renders_every_vocabulary_role_as_dataview_field():
    """One `role:: [[dst-slug]]` inline field per outgoing edge, for the whole
    recommended vocabulary; the destination note keeps the human-readable
    incoming arrow (queryability comes from the source-side fields:
    `FROM "Chain" WHERE contains(supplier_of, [[X]])`)."""
    writer = get_writer()
    src = await chain.create_node("宁德时代", "company")
    dsts: dict[str, dict] = {}
    for i, role in enumerate(chain.RELATION_VOCABULARY):
        dst = await chain.create_node(f"目标实体{i}号", "company")
        await chain.add_edge(src["id"], dst["id"], role)
        dsts[role] = dst

    rel = await chain.export_entity_note(src["id"])
    assert rel == "Chain/宁德时代.md"
    text = (writer.root / rel).read_text(encoding="utf-8")
    assert "## 关系" in text
    for role, dst in dsts.items():
        assert f"{role}:: [[{dst['slug']}]]" in text

    # incoming side: human-readable arrow on the destination note
    dst = dsts["supplier_of"]
    rel_dst = await chain.export_entity_note(dst["id"])
    dst_text = (writer.root / rel_dst).read_text(encoding="utf-8")
    assert "[[宁德时代]] —supplier_of→ 本实体" in dst_text


def test_vocabulary_roles_are_legal_dataview_field_names_verbatim():
    """The role→Dataview-field conversion rule is IDENTITY, valid because every
    shipped role is snake_case ASCII (no whitespace, no `::`, no newline —
    nothing Dataview or the managed-region parser would choke on). A new role
    breaking this shape must fail here and get an explicit conversion or
    domain-boundary rejection (REVIEW-C2 S5) before it ships."""
    for role in chain.RELATION_VOCABULARY:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", role), (
            f"relation role {role!r} is not a safe Dataview field name as-is; "
            "decide a conversion rule before adding it to the vocabulary"
        )


# ---- gap 2: ## Entities footer coverage on the remaining artifact kinds -------

async def test_disputed_digest_gains_entity_footer_idempotently():
    await chain.create_node("宁德时代", "company")
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO fact_cards (id, source_kind, source_ref, analyst_id, claim, category, status, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("fc-vp-1", "whiteboard_card", "card-vp-9", ANALYST,
         "宁德时代 2025 年出货量翻三倍", "numerical", "disputed", now),
    )
    event = _event("factcheck.disputed", "fc-vp-1", {
        "kind": "disputed", "claim": "宁德时代 2025 年出货量翻三倍", "category": "numerical",
        "analyst_id": ANALYST, "source_kind": "whiteboard_card", "source_ref": "card-vp-9",
    })
    await exporter._on_factcheck_disputed(event)

    text = _read_note("Inbox/Disputed Claims.md")
    assert "宁德时代 2025 年出货量翻三倍" in text
    assert "## Entities\n[[宁德时代]]" in text
    assert text.endswith("[[宁德时代]]\n")                     # footer appended LAST

    await exporter._on_factcheck_disputed(event)               # re-fire: rebuilt from rows
    text2 = _read_note("Inbox/Disputed Claims.md")
    assert text2.count("## Entities") == 1


async def test_paper_book_journal_gains_entity_footer_idempotently():
    from app.institute.paper_book import BENCHMARK_ID
    from app.institute.prompts import work_date

    await chain.create_node(BENCHMARK_ID, "other")             # appears in the NAV line
    wd = work_date()
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO nav_history (work_date, nav, gross_exposure, n_open, n_unpriced, realized_pnl_cum, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (wd, 1.042, 2.0, 2, 0, 0.042, now, now),
    )
    event = _event("paper_book.marked", wd, {"work_date": wd, "nav": 1.042, "n_open": 2, "closed": []})
    await exporter._on_paper_book(event)

    rel = f"Book/journal/{wd}.md"
    text = _read_note(rel)
    assert f"纸面交易日志 · {wd}" in text
    assert f"## Entities\n[[{BENCHMARK_ID}]]" in text

    await exporter._on_paper_book(event)
    assert _read_note(rel).count("## Entities") == 1


async def test_research_tree_note_gains_entity_footer_idempotently():
    await chain.create_node("宁德时代", "company")
    now = bus.now_iso()
    await db.execute(
        "INSERT INTO research_trees (id, root_topic, status, max_depth, max_nodes, created_at, finished_at, announced_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("tree-vp-1", "动力电池格局", "completed", 2, 12, now, now, now),
    )
    await db.execute(
        "INSERT INTO research_tree_nodes (id, tree_id, parent_id, depth, topic, question, status, summary, created_at, finished_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("tnode-vp-1", "tree-vp-1", None, 0, "动力电池格局", "", "completed",
         "宁德时代份额继续领先。", now, now),
    )
    event = _event("tree.completed", "tree-vp-1", {})
    await exporter._on_research_tree_completed(event)

    rel = "Research/动力电池格局/tree.md"
    text = _read_note(rel)
    assert "宁德时代份额继续领先" in text
    assert "## Entities\n[[宁德时代]]" in text

    await exporter._on_research_tree_completed(event)
    assert _read_note(rel).count("## Entities") == 1


async def test_bilingual_twin_gains_entity_footer_idempotently():
    await chain.create_node("宁德时代", "company", aliases=["CATL"])
    await db.execute(
        "INSERT INTO tasks (id, requested_hand, prompt, status, source, output, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("task-twin-vp1", "echo", "translate", "completed", "bilingual",
         "CATL kept gaining battery share in Q2.", bus.now_iso()),
    )
    # real emit shape: app/institute/bilingual.py _payload_from_state (BY REFERENCE)
    event = _event("bilingual.twin_ready", "run-vp-9", {
        "run_id": "run-vp-9", "workflow_id": "briefing", "locale": "en",
        "work_date": "2026-07-19", "task_id": "task-twin-vp1",
        "summary": "CATL kept gaining", "text_bytes": 38,
    })
    await exporter._on_twin_ready(event)

    rel = "Briefing/2026-07-19 晨会简报_en.md"
    text = _read_note(rel)
    assert "CATL kept gaining battery share" in text
    assert "## Entities\n[[宁德时代]]" in text                 # alias hit links the node

    await exporter._on_twin_ready(event)                       # same task output → same body
    assert _read_note(rel).count("## Entities") == 1
