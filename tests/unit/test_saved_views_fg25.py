"""
S3 Phase 2 FG 2-5 — saved_views Pydantic schema + service 단위 회귀.

task2-5.md §6 성공 기준:
  - extra="forbid" 화이트리스트 강제
  - tag 정규화 (소문자)
  - 상한 50 강제 (409)
  - UNIQUE (owner_id, name) 위반 시 409
  - PATCH/DELETE 는 WHERE 절에 owner_id 포함 — 다른 사용자가 view_id 만 알아도 수정 불가
  - SavedViewResponse 가 owner_id 미포함 (마스킹)
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("INTERNAL_SERVICE_SECRET", "test-internal")


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

class TestSavedViewFilter:
    def test_empty_filter_ok(self):
        from app.schemas.saved_views import SavedViewFilter
        f = SavedViewFilter()
        assert f.q is None and f.tag is None

    def test_unknown_field_rejected(self):
        from app.schemas.saved_views import SavedViewFilter
        from pydantic import ValidationError
        with pytest.raises(ValidationError) as ei:
            SavedViewFilter(scope_profile_ids=["s1"])  # type: ignore[call-arg]
        assert "extra" in str(ei.value).lower() or "forbid" in str(ei.value).lower()

    def test_tag_normalized_lowercase(self):
        from app.schemas.saved_views import SavedViewFilter
        f = SavedViewFilter(tag=["AI", "  ML/DL  ", "한글"])
        assert f.tag == ["ai", "ml/dl", "한글"]

    def test_tag_empty_strings_dropped(self):
        from app.schemas.saved_views import SavedViewFilter
        f = SavedViewFilter(tag=["ai", "", "  ", "ml"])
        assert f.tag == ["ai", "ml"]

    def test_q_too_long_rejected(self):
        from app.schemas.saved_views import SavedViewFilter
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            SavedViewFilter(q="x" * 201)


class TestSavedViewSort:
    def test_valid_sort(self):
        from app.schemas.saved_views import SavedViewSortItem
        s = SavedViewSortItem(field="updated_at", direction="desc")
        assert s.field == "updated_at"

    def test_invalid_field_rejected(self):
        from app.schemas.saved_views import SavedViewSortItem
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            SavedViewSortItem(field="random_col", direction="asc")  # type: ignore[arg-type]

    def test_invalid_direction_rejected(self):
        from app.schemas.saved_views import SavedViewSortItem
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            SavedViewSortItem(field="title", direction="ascending")  # type: ignore[arg-type]


class TestCreateRequest:
    def test_layout_whitelist(self):
        from app.schemas.saved_views import SavedViewCreateRequest
        r = SavedViewCreateRequest(name="My", layout="graph")
        assert r.layout == "graph"

    def test_invalid_layout_rejected(self):
        from app.schemas.saved_views import SavedViewCreateRequest
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            SavedViewCreateRequest(name="My", layout="kanban")  # type: ignore[arg-type]

    def test_extra_field_rejected(self):
        from app.schemas.saved_views import SavedViewCreateRequest
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            SavedViewCreateRequest(  # type: ignore[call-arg]
                name="My", layout="list", malicious_key="x",
            )

    def test_default_filter_and_sort(self):
        from app.schemas.saved_views import SavedViewCreateRequest
        r = SavedViewCreateRequest(name="My", layout="list")
        assert r.filter.q is None
        assert r.sort == []
        assert r.include_tag_nodes is False


class TestSavedViewResponse:
    def test_owner_id_not_in_response(self):
        """task2-5.md §7 R-02 — owner_id 마스킹."""
        from app.schemas.saved_views import SavedViewResponse
        r = SavedViewResponse(
            id="v1",
            name="My",
            filter={},  # type: ignore[arg-type]
            sort=[],
            layout="list",
            include_tag_nodes=False,
            created_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
            updated_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
        )
        dumped = r.model_dump()
        assert "owner_id" not in dumped


# ---------------------------------------------------------------------------
# Service — 상한 / UNIQUE / owner 검증
# ---------------------------------------------------------------------------

class TestServiceLimits:
    def test_create_above_limit_raises_409(self, monkeypatch):
        from app.services import saved_views_service as svc_module
        from app.api.errors.exceptions import ApiConflictError
        from app.schemas.saved_views import SavedViewCreateRequest

        # repository.count_by_owner → MAX 반환
        monkeypatch.setattr(
            svc_module.saved_views_repository,
            "count_by_owner",
            lambda conn, owner_id: svc_module.MAX_VIEWS_PER_USER,
        )

        with pytest.raises(ApiConflictError):
            svc_module.saved_views_service.create(
                conn=MagicMock(),
                owner_id="u1",
                request=SavedViewCreateRequest(name="N", layout="list"),
            )

    def test_create_under_limit_proceeds(self, monkeypatch):
        from app.services import saved_views_service as svc_module
        from app.models.saved_view import SavedView
        from app.schemas.saved_views import SavedViewCreateRequest

        created = SavedView(
            id="v1", owner_id="u1", name="N", filter={}, sort=[], layout="list",
            created_at=datetime(2026, 5, 10), updated_at=datetime(2026, 5, 10),
        )
        monkeypatch.setattr(
            svc_module.saved_views_repository,
            "count_by_owner",
            lambda conn, owner_id: 5,
        )
        monkeypatch.setattr(
            svc_module.saved_views_repository,
            "create",
            lambda conn, **kw: created,
        )
        result = svc_module.saved_views_service.create(
            conn=MagicMock(),
            owner_id="u1",
            request=SavedViewCreateRequest(name="N", layout="list"),
        )
        assert result.id == "v1"


class TestServiceOwnerEnforcement:
    def test_update_other_owner_returns_not_found(self, monkeypatch):
        """다른 owner 의 view 를 PATCH 시 404 (존재 유출 차단)."""
        from app.services import saved_views_service as svc_module
        from app.api.errors.exceptions import ApiNotFoundError
        from app.schemas.saved_views import SavedViewUpdateRequest

        # repository.update WHERE 절에 owner_id 가 안 맞으면 None 반환
        monkeypatch.setattr(
            svc_module.saved_views_repository,
            "update",
            lambda conn, **kw: None,
        )

        with pytest.raises(ApiNotFoundError):
            svc_module.saved_views_service.update(
                conn=MagicMock(),
                view_id="v-other",
                owner_id="attacker-u",
                request=SavedViewUpdateRequest(name="hijacked"),
            )

    def test_delete_other_owner_returns_not_found(self, monkeypatch):
        from app.services import saved_views_service as svc_module
        from app.api.errors.exceptions import ApiNotFoundError

        monkeypatch.setattr(
            svc_module.saved_views_repository,
            "delete",
            lambda conn, **kw: False,
        )
        with pytest.raises(ApiNotFoundError):
            svc_module.saved_views_service.delete(
                conn=MagicMock(),
                view_id="v-other",
                owner_id="attacker-u",
            )


class TestServiceShareAccess:
    def test_get_for_share_does_not_check_owner(self, monkeypatch):
        """공유 URL — 인증된 viewer 누구나 정의 읽기 가능."""
        from app.services import saved_views_service as svc_module
        from app.models.saved_view import SavedView

        view = SavedView(
            id="v1", owner_id="other-u", name="Shared", filter={}, sort=[],
            layout="list",
            created_at=datetime(2026, 5, 10), updated_at=datetime(2026, 5, 10),
        )
        monkeypatch.setattr(
            svc_module.saved_views_repository,
            "get_by_id",
            lambda conn, view_id: view,
        )
        result = svc_module.saved_views_service.get_for_share(
            conn=MagicMock(), view_id="v1",
        )
        # owner_id 가 다른 사용자라도 정의 읽기 성공
        assert result.id == "v1"
        assert result.name == "Shared"
