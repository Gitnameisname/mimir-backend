"""LLM output validation — LLM02 Insecure Output Handling 대응."""
from __future__ import annotations

import json
import logging
from typing import Any

from app.utils.html_sanitizer import sanitize_html

logger = logging.getLogger(__name__)

# LLM 출력 최대 문자 수
MAX_SUMMARY_LENGTH = 10_000
MAX_KEY_POINT_LENGTH = 1_000


class OutputValidationError(Exception):
    """LLM 출력 검증 실패."""


class LLMOutputValidator:
    """LLM 출력 JSON 파싱 + 스키마 검증 + sanitization."""

    def validate_and_sanitize(self, raw_output: str) -> dict[str, Any]:
        """JSON 파싱 → 필드 검증 → HTML sanitize 순서로 처리한다."""
        # Step 1: JSON 파싱
        try:
            parsed = json.loads(raw_output)
        except json.JSONDecodeError as exc:
            raise OutputValidationError(f"Invalid JSON from LLM: {exc}") from exc

        if not isinstance(parsed, dict):
            raise OutputValidationError("LLM output must be a JSON object")

        # Step 2: 필드 검증
        self._validate_fields(parsed)

        # Step 3: sanitize
        return self._sanitize_recursive(parsed)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _validate_fields(self, data: dict[str, Any]) -> None:
        summary = data.get("summary", "")
        if not isinstance(summary, str):
            raise OutputValidationError("'summary' must be a string")
        if len(summary) > MAX_SUMMARY_LENGTH:
            raise OutputValidationError(
                f"'summary' exceeds {MAX_SUMMARY_LENGTH} characters"
            )

        confidence = data.get("confidence_score")
        if confidence is not None:
            if not isinstance(confidence, (int, float)):
                raise OutputValidationError("'confidence_score' must be numeric")
            if not (0.0 <= float(confidence) <= 1.0):
                raise OutputValidationError(
                    "'confidence_score' must be between 0.0 and 1.0"
                )

        citations = data.get("citations", [])
        if not isinstance(citations, list):
            raise OutputValidationError("'citations' must be a list")
        for citation in citations:
            if not isinstance(citation, dict):
                raise OutputValidationError("Each citation must be an object")
            for required in ("document_id",):
                if required not in citation:
                    raise OutputValidationError(
                        f"Citation missing required field: '{required}'"
                    )

    def _sanitize_recursive(self, data: Any) -> Any:
        if isinstance(data, str):
            return sanitize_html(data)
        if isinstance(data, list):
            return [self._sanitize_recursive(item) for item in data]
        if isinstance(data, dict):
            return {k: self._sanitize_recursive(v) for k, v in data.items()}
        return data
