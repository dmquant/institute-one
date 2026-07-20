"""Analyst roster CRUD persists to catalog/analysts.json and survives reloads."""
from __future__ import annotations

import json
import os

import pytest

from app.config import get_settings
from app.institute import analysts


@pytest.fixture(autouse=True)
def restore_catalog():
    """CRUD writes the real catalog file — snapshot and restore it around each test."""
    path = get_settings().catalog_path
    original = path.read_text(encoding="utf-8")
    yield
    path.write_text(original, encoding="utf-8")
    analysts.reload()


def _payload(analyst_id: str = "quant-analyst") -> dict:
    return {
        "id": analyst_id,
        "name": "量化分析师",
        "name_en": "Quant Analyst",
        "category": "equity",
        "emoji": "📐",
        "focus": "量化与因子分析师",
        "persona": "你以统计证据说话，给出回测窗口与显著性。",
    }


def test_create_update_delete_roundtrip():
    before = len(analysts.roster())

    created = analysts.create_analyst(_payload())
    assert created.id == "quant-analyst"
    assert analysts.get_analyst("quant-analyst") is not None
    assert len(analysts.roster()) == before + 1
    # persisted to disk
    on_disk = json.loads(get_settings().catalog_path.read_text(encoding="utf-8"))
    assert any(a["id"] == "quant-analyst" for a in on_disk["analysts"])

    updated = analysts.update_analyst("quant-analyst", {**_payload(), "name": "量化研究员"})
    assert updated.name == "量化研究员"
    assert analysts.get_analyst("quant-analyst").name == "量化研究员"

    assert analysts.delete_analyst("quant-analyst") is True
    assert analysts.get_analyst("quant-analyst") is None
    assert len(analysts.roster()) == before


def test_manual_edit_reloads_via_mtime():
    """A manual catalog edit (mtime change) is picked up without reload()/restart."""
    path = get_settings().catalog_path
    first = analysts.roster()[0]  # prime the cache

    data = json.loads(path.read_text(encoding="utf-8"))
    data["analysts"][0]["name"] = first.name + "·手工改"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    st = path.stat()  # force the mtime forward in case the write landed in the same tick
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

    assert analysts.roster()[0].name == first.name + "·手工改"
    assert analysts.get_analyst(first.id).name == first.name + "·手工改"


def test_unchanged_mtime_serves_cache():
    """Same mtime -> no re-parse: the cached roster object is returned."""
    analysts.reload()
    a = analysts._load()
    b = analysts._load()
    assert a is b


def test_validation_and_conflicts():
    with pytest.raises(KeyError):
        analysts.create_analyst(_payload("chief-strategist"))  # duplicate id
    with pytest.raises(ValueError):
        analysts.create_analyst({**_payload(), "id": "Bad ID!"})  # not a slug
    with pytest.raises(ValueError):
        analysts.create_analyst({**_payload("x-analyst"), "persona": ""})  # missing field
    with pytest.raises(LookupError):
        analysts.update_analyst("nobody", _payload("nobody"))
    assert analysts.delete_analyst("nobody") is False
