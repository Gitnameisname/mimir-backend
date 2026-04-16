"""
PII(개인 식별 정보) 감지 서비스 — Phase 3 S2.

S2 원칙 ⑦ (폐쇄망 호환):
  - 외부 ML API 의존 없음 — 정규식 기반 순수 로컬 구현
  - 외부 의존 없으므로 off/on 환경변수 제어 불필요 (항상 동작)

감지 패턴 (기본값, 하드코딩):
  - email       : RFC 5321 단순 패턴
  - phone_kr    : 한국 휴대폰 번호 (01X-XXXX-XXXX)
  - phone_kr_10 : 10자리 붙여쓰기 (01XXXXXXXXX)
  - rrn_kr      : 주민등록번호 (XXXXXX-XXXXXXX)
  - credit_card : 신용카드 (4×4 그룹, 하이픈/공백 선택)

선택적 DB 패턴 확장:
  - pii_patterns 테이블에서 활성 패턴 추가 로드 가능 (없으면 기본값만 사용)
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 기본 PII 패턴 (하드코딩 — 폐쇄망에서도 동작)
# ---------------------------------------------------------------------------

_DEFAULT_PATTERNS: dict[str, str] = {
    "email": r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
    "phone_kr": r"01[016789]-\d{3,4}-\d{4}",
    "phone_kr_10": r"01[016789]\d{7,8}",
    "rrn_kr": r"\d{6}-[1-4]\d{6}",
    "credit_card": r"\b\d{4}[\ \-]?\d{4}[\ \-]?\d{4}[\ \-]?\d{4}\b",
}

# 환경변수로 감지 on/off (기본: on)
_PII_DETECTION_ENABLED = os.getenv("PII_DETECTION_ENABLED", "true").lower() != "false"


class PiiDetector:
    """정규식 기반 PII 감지 (동기, 폐쇄망 호환).

    사용법:
        detector = PiiDetector()
        result = detector.detect("문의: user@example.com, 010-1234-5678")
        # {"email": ["user@example.com"], "phone_kr": ["010-1234-5678"]}
    """

    def __init__(self, extra_patterns: Optional[dict[str, str]] = None) -> None:
        """
        Args:
            extra_patterns: 기본 패턴에 추가할 {name: regex} 딕셔너리 (DB 로드 결과 등)
        """
        self._patterns: dict[str, re.Pattern] = {}
        merged = {**_DEFAULT_PATTERNS, **(extra_patterns or {})}
        for name, expr in merged.items():
            try:
                self._patterns[name] = re.compile(expr, re.IGNORECASE)
            except re.error as e:
                logger.warning("PII 패턴 컴파일 실패 name=%s error=%s", name, e)

    # ------------------------------------------------------------------
    # 공개 API
    # ------------------------------------------------------------------

    def detect(self, text: str) -> dict[str, list[str]]:
        """텍스트에서 PII 감지.

        Args:
            text: 검사할 문자열

        Returns:
            {pattern_name: [matched_string, ...], ...}
            감지된 패턴이 없으면 빈 dict.
        """
        if not _PII_DETECTION_ENABLED:
            return {}
        if not text:
            return {}

        result: dict[str, list[str]] = {}
        for name, pattern in self._patterns.items():
            matches = pattern.findall(text)
            if matches:
                result[name] = matches
        return result

    def detect_fields(self, fields: dict[str, str]) -> dict[str, list[str]]:
        """여러 필드를 한 번에 검사.

        Args:
            fields: {"field_name": "text_content", ...}

        Returns:
            {"field_name": [detected_pii_type, ...], ...}
            PII가 감지된 필드만 포함.
        """
        result: dict[str, list[str]] = {}
        for field_name, text in fields.items():
            detected = self.detect(text or "")
            if detected:
                result[field_name] = list(detected.keys())
        return result

    def has_pii(self, text: str) -> bool:
        """PII 포함 여부 빠른 검사."""
        return bool(self.detect(text))

    def annotate_turn(
        self,
        user_message: str,
        assistant_response: str,
    ) -> dict[str, Any]:
        """턴 (user_message, assistant_response) 의 PII 감지 결과 반환.

        Returns::

            {
              "has_pii": bool,
              "user_message": ["email", ...],   # 감지된 패턴 이름 목록
              "assistant_response": [...],
              "detected_at": "ISO8601 문자열",
            }
        """
        from datetime import datetime, timezone

        um_patterns = list(self.detect(user_message).keys())
        ar_patterns = list(self.detect(assistant_response).keys())
        has_pii = bool(um_patterns or ar_patterns)

        return {
            "has_pii": has_pii,
            "user_message": um_patterns,
            "assistant_response": ar_patterns,
            "detected_at": datetime.now(timezone.utc).isoformat(),
        }


# ---------------------------------------------------------------------------
# 모듈 수준 싱글턴 (기본 패턴 사용)
# ---------------------------------------------------------------------------

_detector_instance: Optional[PiiDetector] = None


def get_pii_detector() -> PiiDetector:
    """모듈 수준 싱글턴 PiiDetector 반환."""
    global _detector_instance
    if _detector_instance is None:
        _detector_instance = PiiDetector()
    return _detector_instance
