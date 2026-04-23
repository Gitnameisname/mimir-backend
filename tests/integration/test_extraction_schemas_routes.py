"""
Extraction Schema 라우터 통합 테스트 — Phase 8 FG8.1 P3-E

대상: /api/v1/extraction-schemas 라우터의 입력 검증 경로

테스트 범위 (DB 불필요, Pydantic/쿼리 파서 단계까지만 검증):
- POST /extraction-schemas
    * 최상위 fields 개수 상한(MAX_FIELDS_COUNT+1) 초과 → 422 (P3-C)
    * nested_schema 깊이 상한(MAX_NESTED_DEPTH+1) 초과 → 422 (P3-C)
    * doc_type_code 정규식 위반 → 422 (P2-C)
    * scope_profile_id 비-UUID → 422 (P2-C)
- GET /extraction-schemas?scope_profile_id=not-a-uuid → 422 (P2-C)
- GET /extraction-schemas/{doc_type}?scope_profile_id=not-a-uuid → 422 (P2-C)
- GET /extraction-schemas/{doc_type}/versions?scope_profile_id=not-a-uuid → 422 (P2-D)
- PATCH /extraction-schemas/{doc_type}/deprecate
    * 공백-only reason → 422 (P2-C)
    * 제어문자 포함 reason → 422 (P2-C)
- PUT /extraction-schemas/{doc_type}
    * 빈 fields → 422 (P2-C)
    * 제어문자 포함 change_summary → 422 (P2-C)

주의:
- 모든 케이스는 라우터 진입 직후 Pydantic 또는 쿼리 파라미터 파싱 단계에서 실패하므로
  실제 DB 연결이 필요하지 않음. 즉 INTEGRATION_TEST=1 이 설정되지 않은
  일반 pytest 실행 환경에서도 통과해야 함.
- TestClient 픽스처(`client`) 는 `tests/conftest.py` 가 제공.
"""
from __future__ import annotations

from typing import Any, Dict

import pytest

from app.schemas.extraction import MAX_FIELDS_COUNT, MAX_NESTED_DEPTH


API_BASE = "/api/v1/extraction-schemas"


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------


def _string_field(name: str = "x", *, description: str = "desc") -> Dict[str, Any]:
    return {
        "field_name": name,
        "field_type": "string",
        "required": True,
        "description": description,
    }


def _nested_chain(depth: int) -> Dict[str, Any]:
    """depth 단계 object→object→...→leaf 체인.

    depth=0 → leaf string. depth=N → object field with nested_schema={inner: chain(N-1)}.
    """
    if depth <= 0:
        return _string_field("leaf")
    return {
        "field_name": "obj",
        "field_type": "object",
        "required": False,
        "description": "nested",
        "nested_schema": {"inner": _nested_chain(depth - 1)},
    }


def _base_create_payload(**overrides) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "doc_type_code": "contract",
        "fields": {"x": _string_field("x")},
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# POST /extraction-schemas — 바디 검증 (Pydantic 레이어)
# ---------------------------------------------------------------------------


class TestCreateRouteValidation:
    def test_rejects_over_max_fields_count(self, client, auth_author):
        n = MAX_FIELDS_COUNT + 1
        fields = {f"f_{i}": _string_field(f"f_{i}") for i in range(n)}
        res = client.post(
            API_BASE,
            json=_base_create_payload(fields=fields),
            headers=auth_author,
        )
        assert res.status_code == 422, res.text
        # 상한이 에러 메시지에 노출돼 있어야 함
        assert str(MAX_FIELDS_COUNT) in res.text

    def test_rejects_over_max_nested_depth(self, client, auth_author):
        root = _nested_chain(MAX_NESTED_DEPTH + 1)
        res = client.post(
            API_BASE,
            json=_base_create_payload(fields={"root": root}),
            headers=auth_author,
        )
        assert res.status_code == 422, res.text
        body = res.text
        assert "깊이" in body or "depth" in body.lower()

    def test_rejects_invalid_doc_type_code(self, client, auth_author):
        res = client.post(
            API_BASE,
            json=_base_create_payload(doc_type_code="1contract"),
            headers=auth_author,
        )
        assert res.status_code == 422, res.text

    def test_rejects_non_uuid_scope_profile_id(self, client, auth_author):
        res = client.post(
            API_BASE,
            json=_base_create_payload(scope_profile_id="not-a-uuid"),
            headers=auth_author,
        )
        assert res.status_code == 422, res.text


# ---------------------------------------------------------------------------
# GET — scope_profile_id 쿼리 파라미터 파싱
# ---------------------------------------------------------------------------


class TestScopeProfileIdQueryValidation:
    def test_list_rejects_non_uuid_scope(self, client, auth_viewer):
        res = client.get(
            API_BASE,
            params={"scope_profile_id": "not-a-uuid"},
            headers=auth_viewer,
        )
        assert res.status_code == 422, res.text

    def test_detail_rejects_non_uuid_scope(self, client, auth_viewer):
        res = client.get(
            f"{API_BASE}/contract",
            params={"scope_profile_id": "not-a-uuid"},
            headers=auth_viewer,
        )
        assert res.status_code == 422, res.text

    def test_versions_rejects_non_uuid_scope(self, client, auth_viewer):
        res = client.get(
            f"{API_BASE}/contract/versions",
            params={"scope_profile_id": "not-a-uuid"},
            headers=auth_viewer,
        )
        assert res.status_code == 422, res.text

    def test_versions_rejects_negative_offset(self, client, auth_viewer):
        res = client.get(
            f"{API_BASE}/contract/versions",
            params={"offset": -1},
            headers=auth_viewer,
        )
        assert res.status_code == 422, res.text

    def test_versions_rejects_zero_limit(self, client, auth_viewer):
        res = client.get(
            f"{API_BASE}/contract/versions",
            params={"limit": 0},
            headers=auth_viewer,
        )
        assert res.status_code == 422, res.text


# ---------------------------------------------------------------------------
# PUT /extraction-schemas/{doc_type} — 업데이트 검증
# ---------------------------------------------------------------------------


class TestUpdateRouteValidation:
    def test_rejects_empty_fields(self, client, auth_author):
        res = client.put(
            f"{API_BASE}/contract",
            json={"fields": {}},
            headers=auth_author,
        )
        assert res.status_code == 422, res.text

    def test_rejects_control_char_in_change_summary(self, client, auth_author):
        res = client.put(
            f"{API_BASE}/contract",
            json={
                "fields": {"x": _string_field("x")},
                "change_summary": "hello\x00there",
            },
            headers=auth_author,
        )
        assert res.status_code == 422, res.text

    def test_rejects_over_max_fields_count(self, client, auth_author):
        n = MAX_FIELDS_COUNT + 1
        fields = {f"f_{i}": _string_field(f"f_{i}") for i in range(n)}
        res = client.put(
            f"{API_BASE}/contract",
            json={"fields": fields},
            headers=auth_author,
        )
        assert res.status_code == 422, res.text
        assert str(MAX_FIELDS_COUNT) in res.text


# ---------------------------------------------------------------------------
# PATCH /extraction-schemas/{doc_type}/deprecate — reason 검증
# ---------------------------------------------------------------------------


class TestDeprecateRouteValidation:
    def test_rejects_whitespace_only_reason(self, client, auth_author):
        res = client.patch(
            f"{API_BASE}/contract/deprecate",
            json={"reason": "     "},
            headers=auth_author,
        )
        assert res.status_code == 422, res.text

    def test_rejects_control_char_in_reason(self, client, auth_author):
        res = client.patch(
            f"{API_BASE}/contract/deprecate",
            json={"reason": "stop\x1bnow"},
            headers=auth_author,
        )
        assert res.status_code == 422, res.text

    def test_rejects_missing_reason(self, client, auth_author):
        res = client.patch(
            f"{API_BASE}/contract/deprecate",
            json={},
            headers=auth_author,
        )
        assert res.status_code == 422, res.text


# ---------------------------------------------------------------------------
# GET /extraction-schemas/{doc_type}/versions/diff (P4-A)
# ---------------------------------------------------------------------------


class TestDiffVersionsRouteValidation:
    def test_rejects_missing_base_version(self, client, auth_viewer):
        res = client.get(
            f"{API_BASE}/contract/versions/diff",
            params={"target_version": 2},
            headers=auth_viewer,
        )
        assert res.status_code == 422, res.text

    def test_rejects_missing_target_version(self, client, auth_viewer):
        res = client.get(
            f"{API_BASE}/contract/versions/diff",
            params={"base_version": 1},
            headers=auth_viewer,
        )
        assert res.status_code == 422, res.text

    def test_rejects_zero_base_version(self, client, auth_viewer):
        res = client.get(
            f"{API_BASE}/contract/versions/diff",
            params={"base_version": 0, "target_version": 2},
            headers=auth_viewer,
        )
        assert res.status_code == 422, res.text

    def test_rejects_negative_target_version(self, client, auth_viewer):
        res = client.get(
            f"{API_BASE}/contract/versions/diff",
            params={"base_version": 1, "target_version": -3},
            headers=auth_viewer,
        )
        assert res.status_code == 422, res.text

    def test_rejects_same_base_and_target(self, client, auth_viewer):
        """base == target 는 422 (중복 비교 방지)."""
        res = client.get(
            f"{API_BASE}/contract/versions/diff",
            params={"base_version": 2, "target_version": 2},
            headers=auth_viewer,
        )
        assert res.status_code == 422, res.text

    def test_rejects_non_uuid_scope_profile_id(self, client, auth_viewer):
        res = client.get(
            f"{API_BASE}/contract/versions/diff",
            params={
                "base_version": 1,
                "target_version": 2,
                "scope_profile_id": "not-a-uuid",
            },
            headers=auth_viewer,
        )
        assert res.status_code == 422, res.text


# ---------------------------------------------------------------------------
# POST /extraction-schemas/{doc_type}/rollback (P4-B)
# ---------------------------------------------------------------------------


class TestRollbackRouteValidation:
    def test_rejects_missing_target_version(self, client, auth_author):
        res = client.post(
            f"{API_BASE}/contract/rollback",
            json={},
            headers=auth_author,
        )
        assert res.status_code == 422, res.text

    def test_rejects_zero_target_version(self, client, auth_author):
        res = client.post(
            f"{API_BASE}/contract/rollback",
            json={"target_version": 0},
            headers=auth_author,
        )
        assert res.status_code == 422, res.text

    def test_rejects_negative_target_version(self, client, auth_author):
        res = client.post(
            f"{API_BASE}/contract/rollback",
            json={"target_version": -1},
            headers=auth_author,
        )
        assert res.status_code == 422, res.text

    def test_rejects_non_integer_target_version(self, client, auth_author):
        res = client.post(
            f"{API_BASE}/contract/rollback",
            json={"target_version": "abc"},
            headers=auth_author,
        )
        assert res.status_code == 422, res.text

    def test_rejects_non_uuid_scope_profile_id(self, client, auth_author):
        res = client.post(
            f"{API_BASE}/contract/rollback",
            json={
                "target_version": 1,
                "scope_profile_id": "not-a-uuid",
            },
            headers=auth_author,
        )
        assert res.status_code == 422, res.text

    def test_rejects_control_char_in_change_summary(self, client, auth_author):
        res = client.post(
            f"{API_BASE}/contract/rollback",
            json={
                "target_version": 1,
                "change_summary": "roll\x00back",
            },
            headers=auth_author,
        )
        assert res.status_code == 422, res.text

    def test_rejects_overlong_change_summary(self, client, auth_author):
        res = client.post(
            f"{API_BASE}/contract/rollback",
            json={
                "target_version": 1,
                "change_summary": "x" * 1025,
            },
            headers=auth_author,
        )
        assert res.status_code == 422, res.text


# ---------------------------------------------------------------------------
# P7-1-a/b: doc_type_code 대문자 정규화 + FK 위반 → 422 변환 (Pydantic 레이어만)
# ---------------------------------------------------------------------------


class TestDocTypeCodeNormalization:
    """
    P7-1-b: CreateExtractionSchemaRequest 의 doc_type_code 는
    항상 대문자로 정규화되어야 한다.

    - 소문자 입력 → 대문자로 변환되어 Pydantic 검증 통과.
    - 검증 실패 시 422 는 기존 동작 유지 (이 케이스는 regex 위반).
    - 정규화 결과 자체는 Pydantic 모델 단위 테스트 (DB 없이 가능).
    """

    def test_doc_type_code_is_upper_cased(self):
        from app.schemas.extraction import CreateExtractionSchemaRequest

        req = CreateExtractionSchemaRequest(
            doc_type_code="contract",
            fields={"x": _string_field("x")},
        )
        assert req.doc_type_code == "CONTRACT"

    def test_doc_type_code_mixed_case_is_upper_cased(self):
        from app.schemas.extraction import CreateExtractionSchemaRequest

        req = CreateExtractionSchemaRequest(
            doc_type_code="ConTract_v2",
            fields={"x": _string_field("x")},
        )
        assert req.doc_type_code == "CONTRACT_V2"

    def test_doc_type_code_already_upper_is_preserved(self):
        from app.schemas.extraction import CreateExtractionSchemaRequest

        req = CreateExtractionSchemaRequest(
            doc_type_code="POLICY",
            fields={"x": _string_field("x")},
        )
        assert req.doc_type_code == "POLICY"

    def test_doc_type_code_whitespace_is_stripped_then_upper(self):
        from app.schemas.extraction import CreateExtractionSchemaRequest

        req = CreateExtractionSchemaRequest(
            doc_type_code="  notice  ",
            fields={"x": _string_field("x")},
        )
        assert req.doc_type_code == "NOTICE"

    def test_doc_type_code_digit_prefix_still_422(self, client, auth_author):
        """정규화는 대소문자만 건드리고 regex 는 유지 — 숫자로 시작하면 여전히 거절."""
        res = client.post(
            API_BASE,
            json=_base_create_payload(doc_type_code="1contract"),
            headers=auth_author,
        )
        assert res.status_code == 422, res.text


class TestForeignKeyViolationIsFourTwoTwo:
    """
    P7-1-a: extraction_schemas.doc_type_code 가 document_types 에 없을 때
    psycopg2.errors.ForeignKeyViolation 을 500 이 아닌 422 로 변환해야 한다.

    실제 DB 없이 repo.create 를 monkeypatch 해서 예외 경로만 검증.
    """

    def test_fk_violation_maps_to_422(self, client, auth_author, monkeypatch):
        import psycopg2.errors
        from app.api.v1 import extraction_schemas as es_module

        # get_db 를 no-op context manager 로 교체 — 내부에서 repo 를 쓰지 않으므로
        # conn 은 None 이어도 상관없다 (그 전에 예외가 터진다).
        from contextlib import contextmanager

        @contextmanager
        def _fake_get_db():
            yield None  # repo 가 conn 을 실제로 쓰기 전에 예외를 던질 것

        # repo.create 가 ForeignKeyViolation 을 던지도록 교체
        class _FakeRepo:
            def __init__(self, conn):
                pass

            def create(self, **kwargs):
                exc = psycopg2.errors.ForeignKeyViolation(
                    'insert or update on table "extraction_schemas" violates foreign key '
                    'constraint "extraction_schemas_doc_type_code_fkey"'
                )
                raise exc

        monkeypatch.setattr(es_module, "get_db", _fake_get_db)
        monkeypatch.setattr(es_module, "ExtractionSchemaRepository", _FakeRepo)

        res = client.post(
            API_BASE,
            json=_base_create_payload(doc_type_code="NONEXISTENT_TYPE"),
            headers=auth_author,
        )
        # 기대: 500 아닌 422 + 안내 문구.
        assert res.status_code == 422, res.text
        body = res.json()
        detail = body.get("detail") or body.get("error", {}).get("message", "")
        # 메시지가 dict 안에 들어갈 수 있으므로 문자열화해서 검사
        as_text = detail if isinstance(detail, str) else str(body)
        assert "NONEXISTENT_TYPE" in as_text
        assert "document-types" in as_text or "문서 유형" in as_text or "존재하지 않" in as_text


# ---------------------------------------------------------------------------
# P7-2-a: 구조화 에러 코드
# ---------------------------------------------------------------------------


class TestForeignKeyViolationStructuredDetail:
    """
    P7-2-a: FK violation 응답이 문자열이 아닌 `{code, message, hint}` 구조.

    프론트(ApiError.code) 가 메시지 regex 대신 code 로 라우팅할 수 있도록
    계약을 테스트로 고정한다.
    """

    def test_detail_has_code_message_and_hint(self, client, auth_author, monkeypatch):
        import psycopg2.errors
        from app.api.v1 import extraction_schemas as es_module
        from contextlib import contextmanager

        @contextmanager
        def _fake_get_db():
            yield None

        class _FakeRepo:
            def __init__(self, conn):
                pass

            def create(self, **kwargs):
                raise psycopg2.errors.ForeignKeyViolation(
                    'insert or update on table "extraction_schemas" violates foreign key '
                    'constraint "extraction_schemas_doc_type_code_fkey"'
                )

        monkeypatch.setattr(es_module, "get_db", _fake_get_db)
        monkeypatch.setattr(es_module, "ExtractionSchemaRepository", _FakeRepo)

        res = client.post(
            API_BASE,
            json=_base_create_payload(doc_type_code="NONEXISTENT_TYPE"),
            headers=auth_author,
        )
        assert res.status_code == 422, res.text
        body = res.json()
        # FastAPI HTTPException(detail=dict) → 응답 { "detail": { ... } } 형태.
        detail = body.get("detail")
        assert isinstance(detail, dict), f"detail 은 dict 여야 함: {detail!r}"
        # code
        assert detail.get("code") == "DOC_TYPE_NOT_FOUND"
        # message
        msg = detail.get("message")
        assert isinstance(msg, str)
        assert "NONEXISTENT_TYPE" in msg
        assert "존재하지 않" in msg
        # hint
        hint = detail.get("hint")
        assert isinstance(hint, dict)
        assert hint.get("href") == "/admin/document-types"
        assert hint.get("label") == "문서 유형 관리 열기"


# ---------------------------------------------------------------------------
# P7-2-b: 경로 파라미터 정규화
# ---------------------------------------------------------------------------


class TestDocTypePathNormalization:
    """
    P7-2-b: GET/PUT/DELETE 등 `{doc_type}` 경로 파라미터가 소문자 / 혼합
    케이스로 들어와도 서버 내부에서 UPPER 로 정규화되어 repo 에 전달되어야
    한다. 실제 DB 없이 repo 를 monkeypatch 해 호출 인자를 관찰한다.
    """

    def _patch_repo(self, monkeypatch, method_name: str, returns):
        """extraction_schemas 라우터가 사용하는 repo 의 특정 메서드를 교체.

        교체된 메서드는 첫 번째 positional 인자(= doc_type) 를 captured 리스트에
        기록한 뒤 returns 를 반환한다. captured 리스트는 테스트가 확인.
        """
        from app.api.v1 import extraction_schemas as es_module
        from contextlib import contextmanager

        captured: list = []

        @contextmanager
        def _fake_get_db():
            yield None

        class _FakeRepo:
            def __init__(self, conn):
                pass

        def _fn(self, doc_type, *args, **kwargs):
            captured.append(doc_type)
            return returns

        setattr(_FakeRepo, method_name, _fn)

        monkeypatch.setattr(es_module, "get_db", _fake_get_db)
        monkeypatch.setattr(es_module, "ExtractionSchemaRepository", _FakeRepo)
        return captured

    def test_get_normalizes_lowercase(self, client, auth_reader, monkeypatch):
        captured = self._patch_repo(monkeypatch, "get_by_doc_type", returns=None)
        res = client.get(f"{API_BASE}/contract", headers=auth_reader)
        # repo 반환이 None → 404 (이는 의도한 흐름). 정규화는 이미 발생함.
        assert res.status_code == 404
        assert captured == ["CONTRACT"]

    def test_get_normalizes_mixed_case(self, client, auth_reader, monkeypatch):
        captured = self._patch_repo(monkeypatch, "get_by_doc_type", returns=None)
        res = client.get(f"{API_BASE}/ConTract_v2", headers=auth_reader)
        assert res.status_code == 404
        assert captured == ["CONTRACT_V2"]

    def test_get_versions_normalizes(self, client, auth_reader, monkeypatch):
        captured = self._patch_repo(monkeypatch, "get_versions", returns=[])
        res = client.get(f"{API_BASE}/policy/versions", headers=auth_reader)
        assert res.status_code == 200
        assert captured == ["POLICY"]

    def test_delete_normalizes(self, client, auth_author, monkeypatch):
        # delete 는 boolean 반환.
        captured = self._patch_repo(monkeypatch, "delete", returns=True)
        res = client.delete(f"{API_BASE}/manual", headers=auth_author)
        assert res.status_code == 204
        assert captured == ["MANUAL"]

    def test_invalid_path_returns_422(self, client, auth_reader):
        # 숫자 시작 → 정규화 후에도 regex 위반.
        res = client.get(f"{API_BASE}/1notgood", headers=auth_reader)
        assert res.status_code == 422, res.text
        body = res.json()
        detail = body.get("detail") or ""
        as_text = detail if isinstance(detail, str) else str(body)
        assert "doc_type" in as_text
        assert "형식" in as_text or "허용" in as_text

    def test_whitespace_only_path_rejected(self, client, auth_reader):
        # `%20` (URL-encoded space) 는 FastAPI 가 라우팅하지만 strip 후 빈 문자열.
        # 빈 경로(`/`) 는 다른 라우트(list) 로 매칭되므로, 여기서는 공백 + 이상 문자 조합.
        res = client.get(f"{API_BASE}/%20%20", headers=auth_reader)
        assert res.status_code == 422, res.text
