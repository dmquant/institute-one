from __future__ import annotations

from app.institute.quality import evidence_warnings, quality_callout


def test_evidence_warnings_detect_weak_sources():
    text = (
        "事实：测试（来源：MarketWatch）。\n"
        "另见 file://scratch/report.md 和 https://example.com?q=test\n"
    )
    warnings = evidence_warnings(text, require_followups=True)

    assert any("file://" in w for w in warnings)
    assert any("source label" in w for w in warnings)
    assert any("search-result" in w for w in warnings)
    assert any("follow-up JSON" in w for w in warnings)


def test_evidence_warnings_detect_unparseable_followup_json():
    warnings = evidence_warnings('```json\n{"whiteboard_topics": [{"topic": "bad "quote""}]}\n```', require_followups=True)
    assert any("not parseable" in w for w in warnings)


def test_quality_callout_is_obsidian_warning():
    callout = quality_callout(["no traceable http(s) source URLs found"])
    assert callout.startswith("> [!warning]")
