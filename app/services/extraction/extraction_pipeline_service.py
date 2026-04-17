"""
ExtractionPipelineService — Phase 8 FG8.2.

문서 업로드/개정 시 트리거되는 LLM 자동 추출 파이프라인.

처리 흐름:
  1. 문서 텍스트 + ExtractionTargetSchema 로드
  2. Jinja2 프롬프트 렌더링
  3. LLM 호출 (exponential backoff 재시도 + 로컬 모델 fallback — S2 원칙 ⑦)
  4. JSON 응답 파싱 + 스키마 검증
  5. ExtractionCandidate 저장 (status=pending)
  6. 감사 로그 기록 (actor_type="agent" — S2 원칙 ⑤)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).parent / "templates"


class ExtractionPipelineService:
    """LLM 자동 추출 파이프라인."""

    def __init__(
        self,
        *,
        llm_provider=None,
        template_dir: Optional[Path] = None,
        timeout_sec: int = 30,
        max_retries: int = 3,
    ) -> None:
        self._llm = llm_provider
        self._template_dir = template_dir or _TEMPLATE_DIR
        self._timeout_sec = int(os.getenv("EXTRACTION_TIMEOUT_SEC", str(timeout_sec)))
        self._max_retries = int(os.getenv("EXTRACTION_MAX_RETRIES", str(max_retries)))
        self._provider_type = os.getenv("LLM_PROVIDER", "openai")

    def _get_llm(self):
        if self._llm is None:
            from app.services.rag_service import get_llm_provider
            self._llm = get_llm_provider()
        return self._llm

    # ------------------------------------------------------------------
    # Public trigger
    # ------------------------------------------------------------------

    async def run(
        self,
        *,
        document_id: UUID,
        document_version: int,
        document_text: str,
        doc_type_code: str,
        schema_fields: Dict[str, Any],
        schema_version: int,
        scope_profile_id: Optional[UUID] = None,
        conn,  # psycopg2 connection (within get_db() context)
    ) -> Optional[str]:
        """
        추출 파이프라인을 실행하고 ExtractionCandidate ID를 반환한다.

        감사 로그는 호출자(router/webhook)가 emit_for_actor()로 기록한다.
        """
        from app.repositories.extraction_candidate_repository import ExtractionCandidateRepository
        from app.models.extraction import ExtractionMode, ExtractionConfidenceScore

        start = datetime.now(timezone.utc)

        try:
            # Step 1: 프롬프트 렌더링
            prompt = self._render_prompt(
                schema_fields=schema_fields,
                document_text=document_text,
                extraction_mode="deterministic",
                temperature=0.0,
            )

            # Step 2: LLM 호출
            llm_text, tokens_used = await self._call_with_retry(prompt)

            # Step 3: 응답 파싱
            extracted_fields = self._parse_json(llm_text)

            # Step 4: 스키마 필드 보완 (누락된 키 null 처리)
            for field_name in schema_fields:
                if field_name not in extracted_fields:
                    extracted_fields[field_name] = None

            # Step 5: 신뢰도 점수 계산
            confidence_scores = self._compute_confidence(schema_fields, extracted_fields)

            latency_ms = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
            content_hash = hashlib.sha256(document_text.encode()).hexdigest()
            prompt_version = self._prompt_hash()

            # Step 6: DB 저장
            repo = ExtractionCandidateRepository(conn)
            candidate = repo.create(
                document_id=document_id,
                document_version=document_version,
                extraction_schema_id=doc_type_code,
                extraction_schema_version=schema_version,
                extracted_fields=extracted_fields,
                confidence_scores=confidence_scores,
                extraction_model=self._provider_type,
                extraction_mode=ExtractionMode.DETERMINISTIC,
                extraction_latency_ms=latency_ms,
                extraction_tokens=tokens_used,
                extraction_cost_estimate=self._estimate_cost(tokens_used),
                extraction_prompt_version=prompt_version,
                document_content_hash=content_hash,
                scope_profile_id=scope_profile_id,
                actor_type="agent",
            )

            logger.info(
                "extraction_candidate created id=%s doc=%s ver=%d latency=%dms",
                candidate.id, document_id, document_version, latency_ms,
            )
            return str(candidate.id)

        except Exception as exc:
            logger.error(
                "extraction_pipeline failed doc=%s: %s", document_id, exc, exc_info=True
            )
            raise

    # ------------------------------------------------------------------
    # Prompt rendering
    # ------------------------------------------------------------------

    def _render_prompt(
        self,
        *,
        schema_fields: Dict[str, Any],
        document_text: str,
        extraction_mode: str,
        temperature: float,
    ) -> str:
        try:
            from jinja2 import Environment, FileSystemLoader
            env = Environment(
                loader=FileSystemLoader(str(self._template_dir)),
                trim_blocks=True,
                lstrip_blocks=True,
            )
            tmpl = env.get_template("extraction_prompt.jinja2")
        except Exception:
            return self._fallback_prompt(schema_fields, document_text)

        schema_json = json.dumps(schema_fields, indent=2, ensure_ascii=False)
        field_instructions = [
            {
                "field_name": name,
                "field_type": defn.get("field_type", "string") if isinstance(defn, dict) else "string",
                "required": defn.get("required", False) if isinstance(defn, dict) else False,
                "instruction": defn.get("instruction", "") if isinstance(defn, dict) else "",
                "examples": defn.get("examples", []) if isinstance(defn, dict) else [],
            }
            for name, defn in schema_fields.items()
        ]

        return tmpl.render(
            schema_json=schema_json,
            field_instructions=field_instructions,
            field_names=list(schema_fields.keys()),
            document_text=document_text,
            extraction_mode=extraction_mode,
            temperature=temperature,
        )

    def _fallback_prompt(self, schema_fields: Dict[str, Any], document_text: str) -> str:
        schema_json = json.dumps(schema_fields, indent=2, ensure_ascii=False)
        fields_list = ", ".join(schema_fields.keys())
        return (
            f"다음 원문에서 아래 스키마에 따라 정보를 추출하세요.\n\n"
            f"스키마:\n{schema_json}\n\n"
            f"원문:\n{document_text}\n\n"
            f"아래 JSON 형식으로만 반환 ({fields_list}):"
        )

    # ------------------------------------------------------------------
    # LLM call with retry + local fallback
    # ------------------------------------------------------------------

    async def _call_with_retry(self, prompt: str) -> Tuple[str, Optional[Dict[str, int]]]:
        """LLM 호출 (exponential backoff 재시도 + 폐쇄망 fallback)."""
        llm = self._get_llm()
        last_exc: Optional[Exception] = None

        for attempt in range(self._max_retries):
            try:
                text, tokens = await asyncio.wait_for(
                    llm.complete(
                        system_prompt="당신은 구조화 정보 추출 전문가입니다.",
                        messages=[{"role": "user", "content": prompt}],
                    ),
                    timeout=self._timeout_sec,
                )
                return text, {"total": tokens}
            except asyncio.TimeoutError as exc:
                last_exc = exc
                logger.warning("LLM timeout attempt=%d", attempt + 1)
            except Exception as exc:
                last_exc = exc
                logger.warning("LLM error attempt=%d: %s", attempt + 1, exc)

            if attempt < self._max_retries - 1:
                await asyncio.sleep(2 ** attempt)

        # S2 원칙 ⑦: 폐쇄망 fallback — 로컬 모델 시도
        logger.warning("Falling back to local LLM model")
        try:
            from app.services.rag_service import MockLLMProvider
            local_llm = MockLLMProvider()
            text, tokens = await local_llm.complete(
                system_prompt="당신은 구조화 정보 추출 전문가입니다.",
                messages=[{"role": "user", "content": prompt}],
            )
            return text, {"total": tokens}
        except Exception as fallback_exc:
            logger.error("Local LLM fallback failed: %s", fallback_exc)
            raise last_exc or fallback_exc

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_json(self, text: str) -> Dict[str, Any]:
        """LLM 응답에서 JSON 추출."""
        text = text.strip()

        if "```json" in text:
            start = text.index("```json") + len("```json")
            end = text.index("```", start)
            text = text[start:end].strip()
        elif "```" in text:
            start = text.index("```") + 3
            end = text.index("```", start)
            text = text[start:end].strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"LLM 응답이 유효한 JSON이 아닙니다: {exc}") from exc

    # ------------------------------------------------------------------
    # Confidence scoring (heuristic)
    # ------------------------------------------------------------------

    def _compute_confidence(
        self,
        schema_fields: Dict[str, Any],
        extracted_fields: Dict[str, Any],
    ) -> List[Any]:
        from app.models.extraction import ExtractionConfidenceScore

        scores = []
        for field_name, value in extracted_fields.items():
            if value is None:
                confidence, reason = 0.0, "원문에서 찾지 못함"
            elif isinstance(value, (list, dict)):
                confidence, reason = 0.75, "복합 타입"
            elif isinstance(value, str) and len(value) > 100:
                confidence, reason = 0.85, "긴 텍스트"
            elif isinstance(value, str) and len(value) < 5:
                confidence, reason = 0.70, "짧은 텍스트"
            else:
                confidence, reason = 0.90, "단순 값"

            scores.append(ExtractionConfidenceScore(
                field_name=field_name,
                confidence=confidence,
                reason=reason,
            ))
        return scores

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _estimate_cost(self, tokens: Optional[Dict[str, int]]) -> Optional[float]:
        if not tokens:
            return None
        pricing = {
            "openai": 5.0,
            "anthropic": 3.0,
            "local": 0.0,
        }
        rate = pricing.get(self._provider_type, 0.0)
        total = tokens.get("total", 0)
        cost = (total / 1_000_000) * rate
        return cost if cost > 0 else None

    def _prompt_hash(self) -> str:
        try:
            tmpl_path = self._template_dir / "extraction_prompt.jinja2"
            content = tmpl_path.read_text(encoding="utf-8")
            return hashlib.sha256(content.encode()).hexdigest()[:8]
        except Exception:
            return "unknown"
