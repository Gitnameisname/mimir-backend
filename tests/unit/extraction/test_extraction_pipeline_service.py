"""
ExtractionPipelineService 단위 테스트 — Phase 8 FG8.2

LLM을 Mock 처리하여 파이프라인 로직을 검증한다.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.services.extraction.extraction_pipeline_service import ExtractionPipelineService


@pytest.fixture
def mock_llm():
    llm = AsyncMock()
    llm.complete = AsyncMock(
        return_value=(
            json.dumps(
                {
                    "title": "policy_title",
                    "effective_date": "2024-01-01",
                    "department": "legal",
                },
                ensure_ascii=False,
            ),
            350,
        )
    )
    return llm


@pytest.fixture
def svc(mock_llm, tmp_path):
    # 임시 템플릿 파일 생성
    tmpl_dir = tmp_path / "templates"
    tmpl_dir.mkdir()
    (tmpl_dir / "extraction_prompt.jinja2").write_text(
        "extract from {{ document_text }} schema: {{ schema_json }}",
        encoding="utf-8",
    )
    return ExtractionPipelineService(llm_provider=mock_llm, template_dir=tmpl_dir)


# ---------------------------------------------------------------------------
# _parse_json
# ---------------------------------------------------------------------------

class TestParseJson:
    def test_plain_json(self, svc):
        result = svc._parse_json('{"key": "value"}')
        assert result == {"key": "value"}

    def test_json_in_code_block(self, svc):
        text = '```json\n{"key": "value"}\n```'
        result = svc._parse_json(text)
        assert result["key"] == "value"

    def test_json_in_generic_code_block(self, svc):
        text = '```\n{"key": "value"}\n```'
        result = svc._parse_json(text)
        assert result["key"] == "value"

    def test_invalid_json_raises(self, svc):
        with pytest.raises(ValueError, match="유효한 JSON"):
            svc._parse_json("{ not valid json }")

    def test_empty_object(self, svc):
        result = svc._parse_json("{}")
        assert result == {}


# ---------------------------------------------------------------------------
# _compute_confidence
# ---------------------------------------------------------------------------

class TestComputeConfidence:
    def test_none_value_gives_zero_confidence(self, svc):
        schema = {"field1": {}}
        fields = {"field1": None}
        scores = svc._compute_confidence(schema, fields)
        assert len(scores) == 1
        assert scores[0].confidence == 0.0

    def test_long_text_gives_high_confidence(self, svc):
        schema = {"summary": {}}
        long_text = "x" * 200
        fields = {"summary": long_text}
        scores = svc._compute_confidence(schema, fields)
        assert scores[0].confidence == 0.85

    def test_short_text_gives_lower_confidence(self, svc):
        schema = {"code": {}}
        fields = {"code": "AB"}
        scores = svc._compute_confidence(schema, fields)
        assert scores[0].confidence == 0.70

    def test_complex_type_confidence(self, svc):
        schema = {"items": {}}
        fields = {"items": ["a", "b", "c"]}
        scores = svc._compute_confidence(schema, fields)
        assert scores[0].confidence == 0.75

    def test_simple_value_confidence(self, svc):
        schema = {"date": {}}
        fields = {"date": "2024-01-01"}
        scores = svc._compute_confidence(schema, fields)
        assert scores[0].confidence == 0.90


# ---------------------------------------------------------------------------
# _estimate_cost
# ---------------------------------------------------------------------------

class TestEstimateCost:
    def test_openai_cost(self, svc):
        svc._provider_type = "openai"
        cost = svc._estimate_cost({"total": 1_000_000})
        assert cost == pytest.approx(5.0)

    def test_local_cost_is_zero(self, svc):
        svc._provider_type = "local"
        cost = svc._estimate_cost({"total": 1_000_000})
        assert cost is None

    def test_no_tokens_returns_none(self, svc):
        cost = svc._estimate_cost(None)
        assert cost is None


# ---------------------------------------------------------------------------
# _call_with_retry
# ---------------------------------------------------------------------------

class TestCallWithRetry:
    def test_success_first_attempt(self, svc, mock_llm):
        text, tokens = asyncio.run(svc._call_with_retry("test prompt"))
        parsed = json.loads(text)
        assert parsed["title"] == "policy_title"
        assert tokens == {"total": 350}

    def test_retry_on_timeout(self, svc, mock_llm):
        call_count = [0]

        async def flaky_complete(system_prompt, messages):
            call_count[0] += 1
            if call_count[0] < 2:
                raise asyncio.TimeoutError()
            return json.dumps({"title": "ok"}), 100

        mock_llm.complete = flaky_complete
        svc._max_retries = 3
        svc._timeout_sec = 5

        text, _ = asyncio.run(svc._call_with_retry("test"))
        assert "ok" in text
        assert call_count[0] == 2

    def test_fallback_succeeds_with_mock_provider(self, svc, mock_llm):
        """외부 LLM 실패 시 MockLLMProvider로 fallback하여 빈 문자열을 반환한다."""
        async def always_fail(system_prompt, messages):
            raise Exception("external LLM down")

        mock_llm.complete = always_fail
        svc._max_retries = 1
        svc._timeout_sec = 1

        # MockLLMProvider는 빈 문자열을 반환하므로 fallback 후 tuple을 반환해야 함
        text, _ = asyncio.run(svc._call_with_retry("test"))
        assert isinstance(text, str)


# ---------------------------------------------------------------------------
# run (통합 흐름)
# ---------------------------------------------------------------------------

class TestRun:
    def test_run_creates_candidate(self, svc, mock_llm):
        """run()이 ExtractionCandidateRepository.create()를 호출하는지 확인."""
        mock_repo = MagicMock()
        mock_candidate = MagicMock()
        mock_candidate.id = uuid4()
        mock_candidate.document_id = uuid4()
        mock_candidate.document_version = 1
        mock_candidate.extraction_latency_ms = 100
        mock_repo.create.return_value = mock_candidate

        schema_fields = {
            "title": {"field_type": "string", "required": True, "instruction": "제목 추출"},
            "effective_date": {"field_type": "date", "required": False},
            "department": {"field_type": "string", "required": True},
        }

        with patch(
            "app.repositories.extraction_candidate_repository.ExtractionCandidateRepository",
            return_value=mock_repo,
        ):
            mock_conn = MagicMock()
            result_id = asyncio.run(svc.run(
                document_id=uuid4(),
                document_version=1,
                document_text="privacy policy document content...",
                doc_type_code="POLICY",
                schema_fields=schema_fields,
                schema_version=1,
                scope_profile_id=uuid4(),
                conn=mock_conn,
            ))

        assert result_id is not None
        mock_repo.create.assert_called_once()

    def test_run_fills_missing_fields_with_none(self, svc, mock_llm):
        """LLM이 일부 필드를 누락하면 None으로 채워진다."""
        mock_llm.complete = AsyncMock(
            return_value=(json.dumps({"title": "policy"}), 100)
        )

        schema_fields = {
            "title": {"field_type": "string", "required": True},
            "effective_date": {"field_type": "date", "required": False},
        }

        mock_repo = MagicMock()
        mock_candidate = MagicMock()
        mock_candidate.id = uuid4()
        mock_candidate.document_id = uuid4()
        mock_candidate.document_version = 1
        mock_candidate.extraction_latency_ms = 50
        mock_repo.create.return_value = mock_candidate

        with patch(
            "app.repositories.extraction_candidate_repository.ExtractionCandidateRepository",
            return_value=mock_repo,
        ):
            asyncio.run(svc.run(
                document_id=uuid4(),
                document_version=1,
                document_text="policy document",
                doc_type_code="POLICY",
                schema_fields=schema_fields,
                schema_version=1,
                conn=MagicMock(),
            ))

        call_kwargs = mock_repo.create.call_args.kwargs
        assert call_kwargs["extracted_fields"].get("effective_date") is None
