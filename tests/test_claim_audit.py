from __future__ import annotations

from app import db
from app.institute import claim_audit


def test_claim_audit_classifies_source_attached_and_unsupported():
    text = (
        "公司收入同比增长 25%，达到 100 亿元，来源：[年报](https://example.com/reports/annual-2025)。"
        "管理层宣布 2026 年继续扩产 30%。"
    )

    report = claim_audit.audit_text(text)
    counts = report.counts()

    assert report.total >= 2
    assert counts["source_attached"] >= 1
    assert counts["unsupported"] >= 1
    assert any(c.category == "financial" for c in report.claims)


def test_claim_audit_flags_weak_and_declared_unverified():
    text = (
        "据 https://example.com ，公司股价上涨 12%。\n\n"
        "未经核实：监管机构已经批准该交易。"
    )

    report = claim_audit.audit_text(text)
    counts = report.counts()

    assert counts["weak_source"] >= 1
    assert counts["declared_unverified"] >= 1
    callout = claim_audit.claim_audit_callout(report)
    assert "source_attached" in callout
    assert "weak_source" in callout


async def test_claim_audit_store_replaces_artifact_rows():
    first = "公司收入同比增长 25%，达到 100 亿元，来源：https://example.com/reports/annual-2025。"
    second = "管理层宣布 2026 年继续扩产 30%。"

    report1 = await claim_audit.audit_and_store_text(
        first,
        artifact_kind="analyst-daily",
        artifact_id="a1:2026-06-12",
        artifact_path="Analysts/a1/2026-06-12 日报.md",
        topic="测试分析师",
        analyst_id="a1",
        work_date="2026-06-12",
    )
    assert report1.total >= 1
    rows1 = await db.query("SELECT * FROM fact_cards")
    assert len(rows1) == report1.total
    assert rows1[0]["verdict"] == "source_attached"

    report2 = await claim_audit.audit_and_store_text(
        second,
        artifact_kind="analyst-daily",
        artifact_id="a1:2026-06-12",
        artifact_path="Analysts/a1/2026-06-12 日报.md",
        topic="测试分析师",
        analyst_id="a1",
        work_date="2026-06-12",
    )
    rows2 = await db.query("SELECT * FROM fact_cards")
    assert len(rows2) == report2.total
    assert rows2[0]["verdict"] == "unsupported"
