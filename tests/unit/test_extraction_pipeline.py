"""
ExtractionPipelineService 단위 테스트.

검증 목표:
  - _parse_json(): 평문 JSON, ```json 블록, ``` 블록, 파싱 실패
  - _compute_confidence(): null / 복합 / 긴 텍스트 / 짧은 텍스트 / 단순 값
  - _estimate_cost(): 공급자별 비용 계산, 토큰 없음 처리
  - _fallback_prompt(): 필드 목록 포함 여부
  - run(): LLM mock 연동 전체 파이프라인 (DB mock 사용)
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("INTERNAL_SERVICE_SECRET", "test-internal")

from app.services.extraction.extraction_pipeline_service import ExtractionPipelineService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_service(**kwargs) -> ExtractionPipelineService:
    defaults = {"timeout_sec": 10, "max_retries": 1}
    defaults.update(kwargs)
    return ExtractionPipelineService(**defaults)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# _parse_json
# ---------------------------------------------------------------------------

class TestParseJson:
    def test_plain_json(self):
        svc = make_service()
        result = svc._parse_json('{"name": "Alice", "age": 30}')
        assert result == {"name": "Alice", "age": 30}

    def test_json_in_backtick_block(self):
        svc = make_service()
        text = '```json\n{"key": "value"}\n```'
        result = svc._parse_json(text)
        assert result == {"key": "value"}

    def test_json_in_plain_backtick_block(self):
        svc = make_service()
        text = '```\n{"key": "value"}\n```'
        result = svc._parse_json(text)
        assert result == {"key": "value"}

    def test_invalid_json_raises_value_error(self):
        svc = make_service()
        with pytest.raises(ValueError, match="유효한 JSON"):
            svc._parse_json("not a json string")

    def test_empty_object(self):
        svc = make_service()
        assert svc._parse_json("{}") == {}

    def test_nested_structure(self):
        svc = make_service()
        text = '{"items": [1, 2, 3], "meta": {"count": 3}}'
        result = svc._parse_json(text)
        assert result["items"] == [1, 2, 3]
        assert result["meta"]["count"] == 3

    def test_whitespace_stripped(self):
        svc = make_service()
        result = svc._parse_json('  \n {"a": 1}  \n  ')
        assert result == {"a": 1}


# ---------------------------------------------------------------------------
# _compute_confidence
# ---------------------------------------------------------------------------

class TestComputeConfidence:
    def test_null_value_confidence(self):
        svc = make_service()
        scores = svc._compute_confidence(
            schema_fields={"name": {}},
            extracted_fields={"name": None},
        )
        assert len(scores) == 1
        assert scores[0].confidence == 0.0
        assert scores[0].field_name == "name"

    def test_list_value_confidence(self):
        svc = make_service()
        scores = svc._compute_confidence(
            schema_fields={"tags": {}},
            extracted_fields={"tags": ["a", "b"]},
        )
        assert scores[0].confidence == 0.75

    def test_dict_value_confidence(self):
        svc = make_service()
        scores = svc._compute_confidence(
            schema_fields={"meta": {}},
            extracted_fields={"meta": {"key": "val"}},
        )
        assert scores[0].confidence == 0.75

    def test_long_string_confidence(self):
        svc = make_service()
        long_text = "x" * 200
        scores = svc._compute_confidence(
            schema_fields={"summary": {}},
            extracted_fields={"summary": long_text},
        )
        assert scores[0].confidence == 0.85

    def test_short_string_confidence(self):
        svc = make_service()
        scores = svc._compute_confidence(
            schema_fields={"code": {}},
            extracted_fields={"code": "A1"},
        )
        assert scores[0].confidence == 0.70

    def test_normal_string_confidence(self):
        svc = make_service()
        scores = svc._compute_confidence(
            schema_fields={"title": {}},
            extracted_fields={"title": "Normal length title here"},
        )
        assert scores[0].confidence == 0.90

    def test_multiple_fields(self):
        svc = make_service()
        scores = svc._compute_confidence(
            schema_fields={"a": {}, "b": {}, "c": {}},
            extracted_fields={"a": None, "b": "hello world value", "c": ["x"]},
        )
        assert len(scores) == 3
        confidences = {s.field_name: s.confidence for s in scores}
        assert confidences["a"] == 0.0
        assert confidences["b"] == 0.90
        assert confidences["c"] == 0.75

    def test_empty_extracted_fields(self):
        svc = make_service()
        scores = svc._compute_confidence(schema_fields={}, extracted_fields={})
        assert scores == []


# ---------------------------------------------------------------------------
# _estimate_cost
# ---------------------------------------------------------------------------

class TestEstimateCost:
    def test_openai_cost(self):
        svc = make_service()
        svc._provider_type = "openai"
        cost = svc._estimate_cost({"total": 1_000_000})
        assert cost == pytest.approx(5.0)

    def test_anthropic_cost(self):
        svc = make_service()
        svc._provider_type = "anthropic"
        cost = svc._estimate_cost({"total": 1_000_000})
        assert cost == pytest.approx(3.0)

    def test_local_cost_is_none(self):
        svc = make_service()
        svc._provider_type = "local"
        cost = svc._estimate_cost({"total": 1_000_000})
        # 비용이 0이므로 None 반환
        assert cost is None

    def test_none_tokens_returns_none(self):
        svc = make_service()
        assert svc._estimate_cost(None) is None

    def test_empty_tokens_returns_none(self):
        svc = make_service()
        assert svc._estimate_cost({}) is None

    def test_small_token_count(self):
        svc = make_service()
        svc._provider_type = "openai"
        cost = svc._estimate_cost({"total": 100})
        assert cost == pytest.approx(100 / 1_000_000 * 5.0)

    def test_unknown_provider_costs_zero(self):
        svc = make_service()
        svc._provider_type = "unknown_provider"
        cost = svc._estimate_cost({"total": 1_000_000})
        assert cost is None  # 0.0 → None


# ---------------------------------------------------------------------------
# _fallback_prompt
# ---------------------------------------------------------------------------

class TestFallbackPrompt:
    def test_contains_all_field_names(self):
        svc = make_service()
        fields = {"author": {}, "date": {}, "title": {}}
        prompt = svc._fallback_prompt(fields, "Sample document text")
        for field in fields:
            assert field in prompt

    def test_contains_document_text(self):
        svc = make_service()
        doc_text = "This is the document content."
        prompt = svc._fallback_prompt({}, doc_text)
        assert doc_text in prompt

    def test_contains_json_format_hint(self):
        svc = make_service()
        prompt = svc._fallback_prompt({"f": {}}, "text")
        assert "JSON" in prompt


# ---------------------------------------------------------------------------
# run() — 전체 파이프라인 (LLM + DB mock)
# ---------------------------------------------------------------------------

class TestExtractionPipelineRun:
    def _make_mock_llm(self, response_json: str):
        llm = MagicMock()
        llm.complete = AsyncMock(return_value=(response_json, 100))
        return llm

    def _make_mock_conn(self, candidate_id=None):
        from unittest.mock import MagicMock
        conn = MagicMock()
        candidate = MagicMock()
        candidate.id = candidate_id or uuid4()
        return conn, candidate

    def test_successful_run_returns_candidate_id(self):
        llm = self._make_mock_llm('{"author": "Alice", "date": "2026-01-01"}')
        svc = make_service(llm_provider=llm)

        mock_candidate = MagicMock()
        mock_candidate.id = uuid4()

        conn = MagicMock()

        with patch(
            "app.repositories.extraction_candidate_repository.ExtractionCandidateRepository"
        ) as MockRepo:
            MockRepo.return_value.create.return_value = mock_candidate
            result = run(svc.run(
                document_id=uuid4(),
                document_version=1,
                document_text="Alice wrote this on 2026-01-01.",
                doc_type_code="ARTICLE",
                schema_fields={"author": {}, "date": {}},
                schema_version=1,
                conn=conn,
            ))

        assert result == str(mock_candidate.id)

    def test_missing_fields_filled_with_none(self):
        """스키마에 있지만 LLM이 누락한 필드는 null로 보완된다."""
        llm = self._make_mock_llm('{"author": "Bob"}')  # date 누락
        svc = make_service(llm_provider=llm)

        mock_candidate = MagicMock()
        mock_candidate.id = uuid4()
        conn = MagicMock()

        captured_fields = {}

        def capture_create(**kwargs):
            captured_fields.update(kwargs.get("extracted_fields", {}))
            return mock_candidate

        with patch(
            "app.repositories.extraction_candidate_repository.ExtractionCandidateRepository"
        ) as MockRepo:
            MockRepo.return_value.create.side_effect = capture_create
            run(svc.run(
                document_id=uuid4(),
                document_version=1,
                document_text="Bob wrote something.",
                doc_type_code="ARTICLE",
                schema_fields={"author": {}, "date": {}},
                schema_version=1,
                conn=conn,
            ))

        assert captured_fields["author"] == "Bob"
        assert captured_fields["date"] is None

    def test_llm_failure_raises(self):
        """LLM 호출 실패 시 예외가 전파된다."""
        llm = MagicMock()
        llm.complete = AsyncMock(side_effect=RuntimeError("LLM unavailable"))

        svc = ExtractionPipelineService(
            llm_provider=llm,
            max_retries=1,
            timeout_sec=5,
        )

        with patch("app.repositories.extraction_candidate_repository.ExtractionCandidateRepository"):
            with patch("app.services.rag_service.MockLLMProvider") as MockLocal:
                MockLocal.return_value.complete = AsyncMock(
                    side_effect=RuntimeError("local also fails")
                )
                with pytest.raises(RuntimeError):
                    run(svc.run(
                        document_id=uuid4(),
                        document_version=1,
                        document_text="text",
                        doc_type_code="TYPE",
                        schema_fields={"f": {}},
                        schema_version=1,
                        conn=MagicMock(),
                    ))
