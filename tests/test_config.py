"""Settings field guards: load-time validation of dangerous config values.

max_concurrent / default_timeout_s / output_cap_bytes have minimum bounds, and
timezone is pre-checked against the IANA database, so a bad .env fails at
config load with a clear message instead of misbehaving silently at runtime.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


# ---- max_concurrent --------------------------------------------------------

def test_max_concurrent_zero_rejected_with_maintenance_hint():
    with pytest.raises(ValidationError, match="maintenance"):
        Settings(max_concurrent=0)


def test_max_concurrent_negative_rejected():
    with pytest.raises(ValidationError, match="max_concurrent"):
        Settings(max_concurrent=-2)


def test_max_concurrent_env_zero_rejected(monkeypatch):
    monkeypatch.setenv("INSTITUTE_MAX_CONCURRENT", "0")
    with pytest.raises(ValidationError, match="maintenance"):
        Settings()


def test_max_concurrent_valid_values_load():
    assert Settings(max_concurrent=1).max_concurrent == 1
    assert Settings().max_concurrent >= 1  # built-in default unaffected


# ---- default_timeout_s -----------------------------------------------------

@pytest.mark.parametrize("value", [0, -1])
def test_default_timeout_s_must_be_positive(value):
    with pytest.raises(ValidationError, match="default_timeout_s"):
        Settings(default_timeout_s=value)


def test_default_timeout_s_valid_values_load():
    assert Settings(default_timeout_s=1).default_timeout_s == 1
    assert Settings().default_timeout_s > 0


# ---- output_cap_bytes ------------------------------------------------------

@pytest.mark.parametrize("value", [0, -100])
def test_output_cap_bytes_must_be_positive(value):
    with pytest.raises(ValidationError, match="output_cap_bytes"):
        Settings(output_cap_bytes=value)


def test_output_cap_bytes_valid_values_load():
    assert Settings(output_cap_bytes=1).output_cap_bytes == 1
    assert Settings().output_cap_bytes > 0


# ---- timezone --------------------------------------------------------------

def test_timezone_invalid_rejected_at_load():
    with pytest.raises(ValidationError, match="unknown IANA timezone"):
        Settings(timezone="Mars/Olympus_Mons")


def test_timezone_invalid_rejected_via_env(monkeypatch):
    monkeypatch.setenv("INSTITUTE_TIMEZONE", "Not/AZone")
    with pytest.raises(ValidationError, match="unknown IANA timezone"):
        Settings()


def test_timezone_valid_values_load():
    assert Settings(timezone="UTC").timezone == "UTC"
    assert Settings().timezone  # built-in default unaffected
