"""
DocumentType retrieval_config 스키마 — Phase 2 FG2.2

DocumentType별 Retriever/Reranker 기본 설정을 정의한다.
"""
from __future__ import annotations

import os
import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

# 허용되는 HuggingFace 모델 ID 패턴: "repo/model-name" 형식
# 로컬 절대경로, 상대경로(..), 쉘 특수문자 등은 허용하지 않는다.
_SAFE_MODEL_ID_RE = re.compile(r"^[a-zA-Z0-9_\-\.]+(/[a-zA-Z0-9_\-\.]+)?$")


class RetrieverParams(BaseModel):
    """Retriever 파라미터."""

    fts_weight: float = Field(0.4, ge=0.0, le=1.0)
    vector_weight: float = Field(0.6, ge=0.0, le=1.0)
    similarity_threshold: float = Field(0.3, ge=0.0, le=1.0)


class RerankerParams(BaseModel):
    """Reranker 파라미터."""

    model: Optional[str] = None
    freshness_bonus: float = Field(0.05, ge=0.0)
    pinned_bonus: float = Field(0.10, ge=0.0)

    @field_validator("model")
    @classmethod
    def validate_model_name(cls, v: Optional[str]) -> Optional[str]:
        """모델 이름/경로 보안 검증.

        허용: HuggingFace 스타일 'org/model-name' 또는 단순 모델명.
        거부: 절대경로, 상대경로(..), OS 구분자, 쉘 특수문자.

        보안: 경로 순회(Path Traversal) 및 임의 파일 시스템 접근 방지.
        환경변수 TRUSTED_RERANKER_MODELS 설정 시 해당 목록으로 추가 제한 가능.
        """
        if v is None:
            return v
        # 절대경로 거부
        if os.path.isabs(v):
            raise ValueError(
                f"모델 이름에 절대경로는 허용되지 않습니다: {v!r}. "
                "HuggingFace 모델 ID(예: 'org/model-name') 또는 단순 모델명을 사용하세요."
            )
        # 상위 디렉토리 참조 거부
        if ".." in v.split("/"):
            raise ValueError(
                f"모델 이름에 '..'(상위 디렉토리 참조)는 허용되지 않습니다: {v!r}"
            )
        # 허용 패턴 검사
        if not _SAFE_MODEL_ID_RE.match(v):
            raise ValueError(
                f"모델 이름 형식이 유효하지 않습니다: {v!r}. "
                "영문자, 숫자, -, _, . 만 허용되며 최대 한 번의 '/'를 포함할 수 있습니다."
            )
        # 환경변수로 추가 allowlist 제한 가능
        trusted_env = os.getenv("TRUSTED_RERANKER_MODELS", "").strip()
        if trusted_env:
            trusted = {m.strip() for m in trusted_env.split(",") if m.strip()}
            if v not in trusted:
                raise ValueError(
                    f"모델 {v!r}은(는) TRUSTED_RERANKER_MODELS 목록에 없습니다."
                )
        return v


class RetrievalConfig(BaseModel):
    """DocumentType별 Retriever/Reranker 설정.

    DB: document_types.retrieval_config JSONB 컬럼에 저장.
    기본값: FTS + NullReranker (폐쇄망에서도 동작)
    """

    default_retriever: Literal["fts", "vector", "hybrid"] = "fts"
    retriever_params: RetrieverParams = Field(default_factory=RetrieverParams)
    default_reranker: Optional[Literal["cross_encoder", "rule_based", "null"]] = None
    reranker_params: RerankerParams = Field(default_factory=RerankerParams)
