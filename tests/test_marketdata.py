from __future__ import annotations

from types import SimpleNamespace

from app.institute import marketdata


def test_normalize_ticker():
    assert marketdata.normalize_ticker("$aapl") == "AAPL"
    assert marketdata.normalize_ticker("BRK/B") == "BRK-B"
    assert marketdata.normalize_ticker("not a ticker") is None


async def test_quote_without_fmp_key_is_cached():
    row = await marketdata.get_quote("AAPL", refresh=True)
    assert row["provider"] == "none"
    assert row["payload"]["available"] is False
    assert "price provider disabled" in row["payload"]["reason"]

    cached = await marketdata.get_quote("AAPL")
    assert cached["payload"]["available"] is False


def test_ibkr_bars_to_quote_marks_approximate_history():
    bars = [
        SimpleNamespace(date="2026-06-10", open=190, high=195, low=189, close=192, volume=1000),
        SimpleNamespace(date="2026-06-11", open=192, high=198, low=191, close=196, volume=1200),
    ]
    quote = marketdata._ibkr_bars_to_quote("AAPL", bars)
    assert quote["available"] is True
    assert quote["mode"] == "historical_bar"
    assert quote["approximate"] is True
    assert quote["price"] == 196
    assert quote["change"] == 4
    assert round(quote["changesPercentage"], 4) == 2.0833


def test_financial_snapshot_extracts_latest_facts():
    companyfacts = {
        "facts": {
            "us-gaap": {
                "Revenues": {"units": {"USD": [
                    {"val": 100, "fy": 2024, "fp": "FY", "form": "10-K", "end": "2024-09-30", "filed": "2024-10-30"},
                    {"val": 120, "fy": 2025, "fp": "FY", "form": "10-K", "end": "2025-09-30", "filed": "2025-10-30"},
                ]}},
                "NetIncomeLoss": {"units": {"USD": [
                    {"val": 25, "fy": 2025, "fp": "FY", "form": "10-K", "end": "2025-09-30", "filed": "2025-10-30"},
                ]}},
            }
        }
    }
    snap = marketdata._financial_snapshot(companyfacts)
    assert snap["revenue"]["value"] == 120
    assert snap["revenue"]["fy"] == 2025
    assert snap["net_income"]["value"] == 25
