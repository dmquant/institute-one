from __future__ import annotations

import json

from app import bus, db
from app.institute import judgement_bridge


async def test_judgement_bridge_writes_candidate_queue(tmp_path):
    await db.execute(
        """
        INSERT INTO whiteboard_boards
          (id, topic, question, status, max_cards, work_date, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            "board1",
            "SpaceX 估值补充",
            "太空 AI 算力买方是否非价格敏感？",
            "completed",
            5,
            "2026-06-12",
            bus.now_iso(),
            bus.now_iso(),
        ),
    )
    await db.execute(
        """
        INSERT INTO vault_index
          (path, artifact_kind, artifact_id, sha256, state, written_at)
        VALUES (?,?,?,?,?,?)
        """,
        (
            "Whiteboard/2026-06-12 SpaceX 估值补充.md",
            "whiteboard",
            "board1",
            "abc",
            "clean",
            bus.now_iso(),
        ),
    )
    await db.execute(
        """
        INSERT INTO fact_cards
          (artifact_kind, artifact_id, artifact_path, topic, analyst_id, work_date,
           claim_text, category, verdict, confidence, rationale, source_urls,
           context_text, claim_hash, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "whiteboard",
            "board1",
            "Whiteboard/2026-06-12 SpaceX 估值补充.md",
            "SpaceX 估值补充",
            "",
            "2026-06-12",
            "太空 AI 算力买方不会太在意价格。",
            "financial",
            "unsupported",
            0.1,
            "claim 附近没有可追溯 http(s) 来源",
            json.dumps([]),
            "太空 AI 算力买方不会太在意价格。",
            "hash1",
            bus.now_iso(),
            bus.now_iso(),
        ),
    )

    result = await judgement_bridge.build_review_queue(
        date="2026-06-12",
        output_dir=tmp_path,
    )

    assert result.claims == 1
    assert result.topics == 1
    assert result.verdict_counts["unsupported"] == 1
    text = result.path.read_text(encoding="utf-8")
    assert "candidate_only_do_not_auto_promote" in text
    assert "SpaceX 估值补充" in text
    assert "check_existing_or_possible_conflict" in text
