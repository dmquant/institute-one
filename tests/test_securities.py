"""Security master: canonical ids, market normalization, aliases, thesis edges."""
from __future__ import annotations

import pytest

from app.institute import securities, theses


# ---- canonical ids + normalization ------------------------------------------

def test_canonical_ids_carry_market_suffix():
    assert securities.canonical_id("600519", "CN_A") == "600519.CN_A"
    assert securities.canonical_id("0700", "HK") == "0700.HK"
    assert securities.canonical_id("nvda", "US") == "NVDA.US"
    with pytest.raises(securities.SecurityError, match="unknown market"):
        securities.canonical_id("AAPL", "NASDAQ")
    with pytest.raises(securities.SecurityError, match="ticker"):
        securities.canonical_id("", "US")


def test_market_thesis_data_labels_normalize():
    assert securities.normalize_market("US") == ("US", "stock")
    assert securities.normalize_market("US ETF") == ("US", "etf")
    assert securities.normalize_market("US ADR") == ("US", "adr")
    assert securities.normalize_market("A-share") == ("CN_A", "stock")
    assert securities.normalize_market("A-share ETF") == ("CN_A", "etf")
    assert securities.normalize_market("HK") == ("HK", "stock")
    assert securities.normalize_market("HK ETF") == ("HK", "etf")
    assert securities.normalize_market("Korea") == ("KR", "stock")
    assert securities.normalize_market("Japan") == ("JP", "stock")
    assert securities.normalize_market(" cn_a ") == ("CN_A", "stock")  # already-local label
    with pytest.raises(securities.SecurityError, match="unknown market label"):
        securities.normalize_market("Mars")


# ---- upsert + aliases ---------------------------------------------------------

async def test_upsert_is_idempotent_and_aliases_resolve():
    sec = await securities.upsert_security(
        "600519", "CN_A", name="贵州茅台", meta={"sector": "白酒"}, source="import"
    )
    assert sec["id"] == "600519.CN_A"
    assert sec["meta"] == {"sector": "白酒"}

    again = await securities.upsert_security("600519", "CN_A", name="贵州茅台")
    assert again["id"] == sec["id"]
    assert len(await securities.list_securities()) == 1

    # update-in-place on new name_en
    updated = await securities.upsert_security("600519", "CN_A", name="贵州茅台", name_en="Kweichow Moutai")
    assert updated["name_en"] == "Kweichow Moutai"

    await securities.add_alias(sec["id"], "贵州茅台", kind="name_zh")
    await securities.add_alias(sec["id"], "600519", kind="ticker")
    await securities.add_alias(sec["id"], "600519", kind="ticker")  # dupe ignored
    got = await securities.get_security(sec["id"])
    assert len(got["aliases"]) == 2

    assert (await securities.find_security("600519.CN_A"))["id"] == sec["id"]
    assert (await securities.find_security("贵州茅台"))["id"] == sec["id"]
    assert (await securities.find_security("600519"))["id"] == sec["id"]
    assert await securities.find_security("no-such-thing") is None
    assert await securities.add_alias("MISSING.US", "x") is None

    with pytest.raises(securities.SecurityError, match="unknown alias kind"):
        await securities.add_alias(sec["id"], "x", kind="nickname")
    with pytest.raises(securities.SecurityError, match="unknown instrument_type"):
        await securities.upsert_security("SPY", "US", instrument_type="fund")


# ---- thesis edges --------------------------------------------------------------

async def test_edges_store_role_exposure_confidence_rationale():
    thesis = await theses.create_thesis({"title": "AI 服务器放量", "slug": "ai-servers"})
    sec = await securities.upsert_security("NVDA", "US", name_en="NVIDIA")

    edge = await securities.upsert_edge(
        thesis["id"], sec["id"],
        role="core", exposure="direct", confidence=0.8, rationale="加速卡龙头", source="import",
    )
    assert edge["role"] == "core"
    assert edge["confidence"] == 0.8

    # same (thesis, security, role) upserts in place
    edge2 = await securities.upsert_edge(
        thesis["id"], sec["id"], role="core", exposure="direct", confidence=0.9, rationale="加速卡龙头",
    )
    assert edge2["id"] == edge["id"]
    assert edge2["confidence"] == 0.9

    # edges surface on the thesis detail
    got = await theses.get_thesis(thesis["id"])
    assert len(got["securities"]) == 1
    assert got["securities"][0]["ticker"] == "NVDA"
    assert got["securities"][0]["role"] == "core"

    with pytest.raises(securities.SecurityError, match="confidence"):
        await securities.upsert_edge(thesis["id"], sec["id"], confidence=1.5)
    with pytest.raises(securities.SecurityError, match="unknown thesis"):
        await securities.upsert_edge("nope", sec["id"])
    with pytest.raises(securities.SecurityError, match="unknown security"):
        await securities.upsert_edge(thesis["id"], "nope")

    assert await securities.remove_edge(thesis["id"], sec["id"], "core") == 1
    assert (await theses.get_thesis(thesis["id"]))["securities"] == []
