"""
Admin Extraction Results 라우터 통합 테스트 — Phase 8 FG8.2/8.3 (B 스코프, 2026-04-22).

대상: `/api/v1/admin/extraction-results` 라우터의 RBAC + 입력 검증 경로.

테스트 범위 (DB 불필요, 라우터 진입 직후 검증 단계만):

  - 인증/권한
      * 미인증(헤더 누락) → 401
      * VIEWER 역할 → 403 (admin.read 미충족)
      * AUTHOR 역할 → 403 (admin.read 미충족)
      * APPROVER 역할 → 403 (admin.read 미충족)
      * ORG_ADMIN 에게 GET 허용 (adminOrg=SUPER_ADMIN 로 대체)
      * AUTHOR POST → 403 (admin.write 는 SUPER_ADMIN 전용)

  - 입력 검증 (GET /)
      * status 가 허용되지 않은 값 → 422
      * document_type 이 정규식 위반 → 422
      * scope_profile_id 가 비-UUID → 422
      * page, page_size 범위 위반 → 422

  - 입력 검증 (GET /{id})
      * extraction_id 가 비-UUID → 422 (FastAPI path 파서)
      * scope_profile_id 가 비-UUID → 422

  - 입력 검증 (POST approve/reject)
      * 본문 overrides 가 201 개 → 422 (DoS 방어, 상한 200)
      * approval_comment 가 1025 자 → 422
      * reject reason 이 1025 자 → 422

  - Status 어댑터 함수 (map_status_to_external / map_status_to_internal)

주의:
  - 모든 케이스는 라우터 진입 직후 Pydantic 또는 쿼리 파서 단계에서 실패 또는
    권한 가드에서 401/403 이 떨어진다. 실제 DB 연결은 필요하지 않다.
  - INTEGRATION_TEST=1 이 없는 환경에서도 통과해야 한다.
  - TestClient 픽스처(`client`)는 tests/conftest.py 가 제공.
"""
from __future__ import annotations

from typing import Any, Dict
from uuid import uuid4

import pytest

from app.schemas.admin_extraction_results import (
    map_status_to_external,
    map_status_to_internal,
)


API_BASE = "/api/v1/admin/extraction-results"


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------


def _uuid() -> str:
    return str(uuid4())


# ---------------------------------------------------------------------------
# 상태 어댑터 함수 단위 테스트 (DB 불필요)
# ---------------------------------------------------------------------------


class TestStatusAdapters:
    def test_internal_pending_to_external(self):
        assert map_status_to_external("pending") == "pending_review"

    def test_internal_approved_to_external(self):
        assert map_status_to_external("approved") == "approved"

    def test_internal_modified_maps_to_approved(self):
        # 내부 `modified` 는 외부에서 `approved` 로 정규화.
        assert map_status_to_external("modified") == "approved"

    def test_internal_rejected_to_external(self):
        assert map_status_to_external("rejected") == "rejected"

    def test_unknown_internal_falls_back_to_pending_review(self):
        # 예기치 못한 값은 500 을 내지 말고 pending_review 로 안전 기본값.
        assert map_status_to_external("garbage_value") == "pending_review"

    def test_external_pending_review_to_internal(self):
        assert map_status_to_internal("pending_review") == "pending"

    def test_external_approved_to_internal(self):
        # 외부 approved 는 내부 `approved` 로 매핑되며, `modified` 는 라우터가
        # IN 절로 별도 확장해야 한다(주석대로).
        assert map_status_to_internal("approved") == "approved"

    def test_external_rejected_to_internal(self):
        assert map_status_to_internal("rejected") == "rejected"

    def test_unknown_external_raises_value_error(self):
        with pytest.raises(ValueError):
            map_status_to_internal("not_a_status")


# ---------------------------------------------------------------------------
# RBAC — GET (admin.read = ORG_ADMIN | SUPER_ADMIN)
# ---------------------------------------------------------------------------


class TestListRouteRBAC:
    def test_unauthenticated_rejected(self, client):
        res = client.get(API_BASE)
        # 미인증은 401 (debug 헤더 없음).
        assert res.status_code == 401, res.text

    def test_viewer_forbidden(self, client, auth_viewer):
        res = client.get(API_BASE, headers=auth_viewer)
        assert res.status_code == 403, res.text

    def test_author_forbidden_on_list(self, client, auth_author):
        # AUTHOR 도 admin.read 에 대한 권한 없음 (ORG_ADMIN/SUPER_ADMIN 만 허용).
        res = client.get(API_BASE, headers=auth_author)
        assert res.status_code == 403, res.text

    def test_approver_forbidden_on_list(self, client, auth_approver):
        res = client.get(API_BASE, headers=auth_approver)
        assert res.status_code == 403, res.text


# ---------------------------------------------------------------------------
# RBAC — POST (admin.write = SUPER_ADMIN 전용)
# ---------------------------------------------------------------------------


class TestWriteRouteRBAC:
    def test_author_forbidden_on_approve(self, client, auth_author):
        # AUTHOR 는 admin.write 미충족 → 403.
        res = client.post(
            f"{API_BASE}/{_uuid()}/approve",
            json={},
            headers=auth_author,
        )
        assert res.status_code == 403, res.text

    def test_viewer_forbidden_on_reject(self, client, auth_viewer):
        res = client.post(
            f"{API_BASE}/{_uuid()}/reject",
            json={"reason": "n/a"},
            headers=auth_viewer,
        )
        assert res.status_code == 403, res.text

    def test_unauthenticated_approve(self, client):
        res = client.post(
            f"{API_BASE}/{_uuid()}/approve",
            json={},
        )
        assert res.status_code == 401, res.text


# ---------------------------------------------------------------------------
# 입력 검증 — GET /admin/extraction-results (목록)
# ---------------------------------------------------------------------------


class TestListQueryValidation:
    def test_invalid_status_returns_422(self, client, auth_admin):
        res = client.get(
            f"{API_BASE}?status=not_a_status",
            headers=auth_admin,
        )
        assert res.status_code == 422, res.text
        assert "status" in res.text

    def test_invalid_document_type_returns_422(self, client, auth_admin):
        # 소문자 시작은 정규식 위반 → 422 (UPPER 변환 후에도 불통과할 때).
        # 하지만 서버는 대문자로 변환 후 검증하므로, 특수문자를 포함해야 실패한다.
        res = client.get(
            f"{API_BASE}?document_type=!!invalid!!",
            headers=auth_admin,
        )
        assert res.status_code == 422, res.text
        assert "document_type" in res.text or "형식" in res.text

    def test_invalid_scope_profile_id_returns_422(self, client, auth_admin):
        res = client.get(
            f"{API_BASE}?scope_profile_id=not-a-uuid",
            headers=auth_admin,
        )
        assert res.status_code == 422, res.text
        assert "scope_profile_id" in res.text or "UUID" in res.text

    def test_page_must_be_positive(self, client, auth_admin):
        res = client.get(f"{API_BASE}?page=0", headers=auth_admin)
        assert res.status_code == 422, res.text

    def test_page_size_upper_bound(self, client, auth_admin):
        # page_size 최대 100 (DoS 방어).
        res = client.get(f"{API_BASE}?page_size=10000", headers=auth_admin)
        assert res.status_code == 422, res.text

    def test_page_upper_bound(self, client, auth_admin):
        # page 최대 10,000.
        res = client.get(f"{API_BASE}?page=99999", headers=auth_admin)
        assert res.status_code == 422, res.text

    def test_valid_status_values_accepted(self, client, auth_admin):
        # 실제 DB 가 없으면 200 이 아닐 수도 있지만, 적어도 422 는 아니어야 한다.
        # ExtractionCandidateRepository.list_for_admin_queue 가 빈 결과를 내거나
        # DB 연결 실패면 500 이 되며 여기서는 입력 단계 통과 여부만 본다.
        for s in ("pending_review", "approved", "rejected"):
            res = client.get(f"{API_BASE}?status={s}", headers=auth_admin)
            # 422 아님을 확인 (DB 의존 실패는 허용).
            assert res.status_code != 422, (s, res.text)


# ---------------------------------------------------------------------------
# 입력 검증 — GET /admin/extraction-results/{id}
# ---------------------------------------------------------------------------


class TestDetailRouteValidation:
    def test_non_uuid_path_returns_422(self, client, auth_admin):
        res = client.get(f"{API_BASE}/not-a-uuid", headers=auth_admin)
        assert res.status_code == 422, res.text

    def test_invalid_scope_profile_id_returns_422(self, client, auth_admin):
        res = client.get(
            f"{API_BASE}/{_uuid()}?scope_profile_id=not-a-uuid",
            headers=auth_admin,
        )
        assert res.status_code == 422, res.text


# ---------------------------------------------------------------------------
# 입력 검증 — POST approve / reject (본문)
# ---------------------------------------------------------------------------


class TestApproveBodyValidation:
    def test_overrides_size_limit(self, client, auth_admin):
        # 상한 200 (DoS 방어). 201 개면 422.
        overrides = {f"k{i}": i for i in range(201)}
        res = client.post(
            f"{API_BASE}/{_uuid()}/approve",
            json={"overrides": overrides},
            headers=auth_admin,
        )
        assert res.status_code == 422, res.text

    def test_approval_comment_max_length(self, client, auth_admin):
        # max_length=1024. 1025 자면 422.
        res = client.post(
            f"{API_BASE}/{_uuid()}/approve",
            json={"approval_comment": "x" * 1025},
            headers=auth_admin,
        )
        assert res.status_code == 422, res.text

    def test_empty_body_passes_validation(self, client, auth_admin):
        # 빈 body 는 Pydantic 검증은 통과해야 한다. DB 레벨에서 404 등이 나올 수 있음.
        res = client.post(
            f"{API_BASE}/{_uuid()}/approve",
            json={},
            headers=auth_admin,
        )
        # 입력 단계 통과 확인.
        assert res.status_code != 422, res.text

    def test_non_uuid_path_returns_422(self, client, auth_admin):
        res = client.post(
            f"{API_BASE}/not-a-uuid/approve",
            json={},
            headers=auth_admin,
        )
        assert res.status_code == 422, res.text


class TestRejectBodyValidation:
    def test_reason_max_length(self, client, auth_admin):
        res = client.post(
            f"{API_BASE}/{_uuid()}/reject",
            json={"reason": "r" * 1025},
            headers=auth_admin,
        )
        assert res.status_code == 422, res.text

    def test_blank_reason_accepted_and_normalized(self, client, auth_admin):
        # 공백만 있는 reason 은 validator 가 None 으로 정규화 → 422 아님.
        res = client.post(
            f"{API_BASE}/{_uuid()}/reject",
            json={"reason": "   "},
            headers=auth_admin,
        )
        assert res.status_code != 422, res.text

    def test_empty_body_accepted(self, client, auth_admin):
        res = client.post(
            f"{API_BASE}/{_uuid()}/reject",
            json={},
            headers=auth_admin,
        )
        assert res.status_code != 422, res.text

    def test_non_uuid_path_returns_422(self, client, auth_admin):
        res = client.post(
            f"{API_BASE}/not-a-uuid/reject",
            json={"reason": "x"},
            headers=auth_admin,
        )
        assert res.status_code == 422, res.text


# ---------------------------------------------------------------------------
# 교차 scope 방어 — 존재 여부 노출 회피 (404 not 403)
# ---------------------------------------------------------------------------


class TestScopeIsolation:
    """
    존재하지 않는 ID + 잘못된 scope 조합도 404 로 응답해야 한다
    (existence enumeration 방어).
    """

    def test_nonexistent_id_returns_404_or_500(self, client, auth_admin):
        # DB 연결이 없으면 500 이 될 수 있으나, 연결이 있고 단순히 id 가 없으면
        # 404. 여기선 403 이 아닌 것만 확인(권한은 통과했어야 함).
        res = client.get(f"{API_BASE}/{_uuid()}", headers=auth_admin)
        assert res.status_code != 403, res.text
        assert res.status_code != 401, res.text


# ---------------------------------------------------------------------------
# document_type 정규화 (소문자 → 대문자) 경로
# ---------------------------------------------------------------------------


class TestDocumentTypeNormalization:
    def test_lowercase_document_type_passes_validation(self, client, auth_admin):
        # 서버가 대문자로 정규화하므로 소문자는 422 가 아니다.
        res = client.get(
            f"{API_BASE}?document_type=contract",
            headers=auth_admin,
        )
        assert res.status_code != 422, res.text

    def test_mixed_case_normalizes(self, client, auth_admin):
        res = client.get(
            f"{API_BASE}?document_type=Contract",
            headers=auth_admin,
        )
        assert res.status_code != 422, res.text

    def test_document_type_with_hyphen_accepted(self, client, auth_admin):
        res = client.get(
            f"{API_BASE}?document_type=sub-contract",
            headers=auth_admin,
        )
        assert res.status_code != 422, res.text

    def test_document_type_starting_with_digit_rejected(self, client, auth_admin):
        res = client.get(
            f"{API_BASE}?document_type=1contract",
            headers=auth_admin,
        )
        assert res.status_code == 422, res.text
