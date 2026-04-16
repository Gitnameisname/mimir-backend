"""Prompt Registry — 프롬프트 템플릿 중앙 관리.

JSON 시드 파일에서 프롬프트를 로드하고 키로 조회한다.
DB 저장은 Phase 3에서 확장 예정. 현재는 파일 기반 로드만 지원한다.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 시드 파일 디렉터리: 이 파일 기준 상대 경로
_SEEDS_DIR = Path(__file__).parent / "seeds"


class PromptRegistry:
    """프롬프트 템플릿 레지스트리.

    시드 JSON 파일에서 로드한 템플릿을 메모리에 캐시한다.
    파일 로드 실패 시 경고 로그 후 None 반환 — 호출자가 기본값을 사용해야 한다.
    """

    _instance: Optional["PromptRegistry"] = None
    _cache: dict[str, str]

    def __init__(self) -> None:
        self._cache = {}
        self._load_seeds()

    @classmethod
    def instance(cls) -> "PromptRegistry":
        """싱글턴 인스턴스 반환."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def get(self, key: str) -> Optional[str]:
        """키로 프롬프트 템플릿 문자열을 조회한다.

        Args:
            key: 프롬프트 식별자 (예: "query_rewrite_standalone")

        Returns:
            템플릿 문자열 또는 None (키 없을 때)
        """
        return self._cache.get(key)

    def register(self, key: str, template: str) -> None:
        """런타임에 프롬프트 템플릿을 등록한다 (테스트/재정의용)."""
        self._cache[key] = template

    def _load_seeds(self) -> None:
        """시드 디렉터리의 JSON 파일을 모두 로드한다."""
        if not _SEEDS_DIR.exists():
            logger.debug("Prompt seeds directory not found: %s", _SEEDS_DIR)
            return

        for seed_path in _SEEDS_DIR.glob("*.json"):
            try:
                with open(seed_path, encoding="utf-8") as f:
                    data = json.load(f)
                key = data.get("key")
                template = data.get("template")
                if key and template:
                    self._cache[key] = template
                    logger.debug("Loaded prompt seed: %s", key)
                else:
                    logger.warning(
                        "Prompt seed %s missing 'key' or 'template' field", seed_path
                    )
            except Exception as exc:
                logger.warning("Failed to load prompt seed %s: %s", seed_path, exc)
