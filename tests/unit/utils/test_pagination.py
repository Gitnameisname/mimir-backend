"""Unit tests for :mod:`app.repositories.pagination`.

Covers: None 기본값 / 음수 보호 / 상한 cap / 정상 / 경계.
Docs: ``docs/함수도서관/backend.md`` §1.9 BE-G5.
"""
from __future__ import annotations

import pytest

from app.repositories.pagination import clamp_pagination, paginate_page


class TestClampPagination:
    def test_both_none_uses_defaults(self):
        assert clamp_pagination(None, None) == (50, 0)

    def test_custom_default_limit(self):
        assert clamp_pagination(None, None, default_limit=20) == (20, 0)

    def test_normal_values_pass_through(self):
        assert clamp_pagination(50, 100) == (50, 100)

    def test_zero_limit_clamped_to_one(self):
        assert clamp_pagination(0, 0) == (1, 0)

    def test_negative_limit_clamped_to_one(self):
        assert clamp_pagination(-10, 0) == (1, 0)

    def test_limit_above_max_capped(self):
        assert clamp_pagination(99999, 0, max_limit=100) == (100, 0)

    def test_default_max_limit_200(self):
        assert clamp_pagination(500, 0) == (200, 0)

    def test_negative_offset_clamped_to_zero(self):
        assert clamp_pagination(50, -5) == (50, 0)

    def test_offset_none_uses_zero(self):
        assert clamp_pagination(50, None) == (50, 0)

    def test_limit_exact_max_kept(self):
        assert clamp_pagination(200, 0, max_limit=200) == (200, 0)

    def test_limit_one_kept(self):
        assert clamp_pagination(1, 0) == (1, 0)

    def test_offset_zero_kept(self):
        assert clamp_pagination(50, 0) == (50, 0)

    def test_large_offset_passes_through(self):
        # offset 상한은 없음 (호출자가 도메인별로 결정)
        assert clamp_pagination(50, 1_000_000) == (50, 1_000_000)

    @pytest.mark.parametrize(
        ("limit", "offset", "max_limit", "default_limit", "expected"),
        [
            (None, None, 100, 20, (20, 0)),
            (10, 0, 100, 20, (10, 0)),
            (-1, -1, 100, 20, (1, 0)),
            (200, 50, 100, 20, (100, 50)),
        ],
    )
    def test_parametrized(self, limit, offset, max_limit, default_limit, expected):
        assert (
            clamp_pagination(limit, offset, max_limit=max_limit, default_limit=default_limit)
            == expected
        )


# ===========================================================================
# paginate_page (R1, 2026-04-25)
# ===========================================================================


class TestPaginatePage:
    def test_normal(self):
        assert paginate_page(1, 50) == (1, 50, 0)

    def test_offset_calculation(self):
        assert paginate_page(3, 20) == (3, 20, 40)

    def test_both_none_uses_defaults(self):
        assert paginate_page(None, None) == (1, 50, 0)

    def test_zero_page_clamped_to_one(self):
        assert paginate_page(0, 50) == (1, 50, 0)

    def test_negative_page_clamped_to_one(self):
        assert paginate_page(-3, 50) == (1, 50, 0)

    def test_negative_page_size_clamped_to_one(self):
        assert paginate_page(2, -10) == (2, 1, 1)

    def test_page_size_above_max_capped(self):
        assert paginate_page(5, 99999, max_page_size=100) == (5, 100, 400)

    def test_default_page_size(self):
        assert paginate_page(2, None, default_page_size=20) == (2, 20, 20)

    def test_first_page_always_offset_zero(self):
        for size in [1, 10, 100, 200]:
            assert paginate_page(1, size)[2] == 0

    def test_offset_consistent_with_clamp_pagination(self):
        """paginate_page 의 page_size clamp 가 clamp_pagination 와 동일 결과."""
        page, page_size, _offset = paginate_page(1, 99999, max_page_size=100)
        clamped_limit, _ = clamp_pagination(99999, 0, max_limit=100, default_limit=50)
        assert page_size == clamped_limit
