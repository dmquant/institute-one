from app import db
from app.institute import evidence


def test_extract_sources_canonicalizes_and_keeps_markdown_title():
    text = (
        "参考 [SEC filing](https://www.sec.gov/ixviewer/doc/action?doc=foo&utm_source=x) "
        "以及 https://example.com/report?a=1&utm_campaign=noise。"
    )

    out = evidence.extract_sources(text)

    assert len(out) == 2
    assert out[0].title == "SEC filing"
    assert "utm_source" not in out[0].canonical_url
    assert out[0].host == "www.sec.gov"
    assert out[1].canonical_url == "https://example.com/report?a=1"


async def test_ingest_text_and_topic_context():
    text = (
        "中国BD授权讨论引用 [公告](https://www.example.com/bd-deal?utm_medium=x)。"
        "该公告影响 milestone 兑现率判断。"
    )

    inserted = await evidence.ingest_text(
        text,
        artifact_kind="whiteboard_card",
        artifact_id="card001",
        artifact_path="Whiteboard/x.md#card-01",
        topic="中国BD授权泡沫检验",
        analyst_id="healthcare-analyst",
        work_date="2026-06-12",
    )

    assert inserted == 1
    source = await db.query_one("SELECT * FROM evidence_sources")
    assert source is not None
    assert source["canonical_url"] == "https://www.example.com/bd-deal"
    link = await db.query_one("SELECT * FROM claim_evidence_links")
    assert link is not None
    assert link["topic"] == "中国BD授权泡沫检验"
    assert "milestone" in link["context_text"]

    block = await evidence.evidence_context("中国BD授权")
    assert "既有证据账本" in block
    assert "https://www.example.com/bd-deal" in block
