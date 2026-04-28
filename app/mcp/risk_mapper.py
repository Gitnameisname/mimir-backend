"""
prompt_injection_detector 결과 → MCPDetectedRisk 매핑 — S3 Phase 4 FG 4-1 §2.1.3.

R6 (차단이 아닌 경고): 본 매핑은 위험 메타데이터만 만든다. 응답 자체 차단은 하지 않는다.
호출자 (mcp_router) 가 envelope.detected_risks 에 부착해 클라이언트에 전달.

prompt_injection.py 의 패턴 카테고리 (PromptInjectionDetector._ALL_PATTERNS) →
MCPDetectedRisk.code / severity 매핑 표:

  | 패턴 카테고리              | code                  | severity |
  |---------------------------|-----------------------|----------|
  | instruction_override      | directive_pattern     | high     |
  | code_execution            | secret_leak           | high     |
  | markup_injection          | url_obfuscation       | medium   |
  | boundary_manipulation     | directive_pattern     | high     |
  | (기타 / 일반 anomaly)     | anomaly               | low      |

폐쇄망 안전 (S2 ⑦): 본 모듈은 InjectionDetectionResult 구조만 의존 — 외부 LLM 호출 없음.
"""
from __future__ import annotations

from typing import Iterable

from app.schemas.mcp import MCPDetectedRisk


# 패턴 카테고리 (prompt_injection 모듈의 _ALL_PATTERNS 라벨 prefix) → (code, severity) 매핑.
# label 형식: "<category>:<pattern_prefix>" — split(":", 1) 로 카테고리 추출.
_CATEGORY_MAP: dict[str, tuple[str, str]] = {
    "instruction_override": ("directive_pattern", "high"),
    "boundary_manipulation": ("directive_pattern", "high"),
    "code_execution": ("secret_leak", "high"),
    "markup_injection": ("url_obfuscation", "medium"),
}

_DEFAULT = ("anomaly", "low")


def _label_to_category(label: str) -> str:
    """``"category:pattern_prefix"`` → ``"category"``."""
    if ":" in label:
        return label.split(":", 1)[0]
    return label


def map_injection_patterns(patterns_detected: Iterable[str]) -> list[MCPDetectedRisk]:
    """``InjectionDetectionResult.injection_patterns_detected`` → MCPDetectedRisk 리스트.

    같은 (code, severity) 의 위험은 1건만 반환 (중복 제거 — 클라이언트 노이즈 감소).
    note 필드에 매칭된 패턴 이름 prefix 를 첨부 (디버깅 / 운영 진단).
    """
    seen: dict[tuple[str, str], list[str]] = {}
    for label in patterns_detected:
        category = _label_to_category(label)
        code, severity = _CATEGORY_MAP.get(category, _DEFAULT)
        seen.setdefault((code, severity), []).append(label)

    risks: list[MCPDetectedRisk] = []
    for (code, severity), labels in seen.items():
        # note 는 첫 라벨만 (장문 회피)
        note = labels[0] if labels else None
        risks.append(MCPDetectedRisk(code=code, severity=severity, note=note))
    return risks


def map_injection_result(result) -> list[MCPDetectedRisk]:
    """``InjectionDetectionResult`` 객체 또는 None → MCPDetectedRisk 리스트.

    None / 위험 없음 → 빈 리스트.
    """
    if result is None:
        return []
    patterns = getattr(result, "injection_patterns_detected", None) or []
    return map_injection_patterns(patterns)
