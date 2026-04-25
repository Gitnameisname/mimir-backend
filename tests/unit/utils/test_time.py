"""Unit tests for :mod:`app.utils.time`.

Covers: aware 여부, ISO 포맷, mock patch 가능성, 단조 증가.
Docs: ``docs/함수도서관/backend.md`` §1.1 B1.
"""
from __future__ import annotations

import re
import time as _time
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.utils.time import utcnow, utcnow_iso


# --- utcnow --------------------------------------------------------------


def test_utcnow_returns_datetime() -> None:
    assert isinstance(utcnow(), datetime)


def test_utcnow_is_timezone_aware() -> None:
    ts = utcnow()
    assert ts.tzinfo is not None, "utcnow() 는 timezone-aware 여야 한다"


def test_utcnow_uses_utc_timezone() -> None:
    ts = utcnow()
    # tzinfo 인스턴스 동일성 대신 utcoffset 으로 비교 (미래 tz 변경 대응)
    assert ts.utcoffset().total_seconds() == 0


def test_utcnow_is_monotonic_over_sleep() -> None:
    t1 = utcnow()
    _time.sleep(0.001)
    t2 = utcnow()
    assert t2 >= t1


def test_utcnow_matches_reference_within_slack() -> None:
    """실제 `datetime.now(timezone.utc)` 대비 2초 이내 오차여야 한다."""
    ref = datetime.now(timezone.utc)
    got = utcnow()
    delta = abs((got - ref).total_seconds())
    assert delta < 2.0, f"utcnow() 오차가 과대: {delta}s"


def test_utcnow_is_patchable() -> None:
    """테스트에서 mock 주입이 가능해야 한다."""
    fixed = datetime(2026, 4, 25, 9, 41, 0, tzinfo=timezone.utc)
    with patch("app.utils.time.utcnow", return_value=fixed):
        from app.utils.time import utcnow as patched_utcnow

        assert patched_utcnow() == fixed


# --- utcnow_iso ----------------------------------------------------------


_ISO_AWARE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:\+00:00|Z)$"
)


def test_utcnow_iso_returns_str() -> None:
    assert isinstance(utcnow_iso(), str)


def test_utcnow_iso_matches_expected_format() -> None:
    s = utcnow_iso()
    assert _ISO_AWARE_RE.match(s), f"예상 포맷 미일치: {s}"


def test_utcnow_iso_parses_back_to_aware_datetime() -> None:
    s = utcnow_iso()
    parsed = datetime.fromisoformat(s)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0


def test_utcnow_iso_is_consistent_with_utcnow() -> None:
    """두 함수의 시각은 수 밀리초 이내여야 한다."""
    before = utcnow()
    iso = utcnow_iso()
    after = utcnow()
    parsed = datetime.fromisoformat(iso)
    assert before <= parsed <= after


# --- regression guard ---------------------------------------------------


def test_module_has_no_side_effects_on_import() -> None:
    """순수 함수 모듈이어야 한다. __all__ 만 노출."""
    import app.utils.time as mod

    assert set(mod.__all__) == {"utcnow", "utcnow_iso"}
    # 외부 I/O 진입점이 노출되지 않음을 확인
    assert not hasattr(mod, "requests")
    assert not hasattr(mod, "httpx")


@pytest.mark.parametrize("_", range(3))
def test_utcnow_stability_across_calls(_: int) -> None:
    ts = utcnow()
    assert ts.tzinfo is not None
