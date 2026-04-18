"""
임베딩 서비스 — EmbeddingProvider 추상화 레이어.

설계 원칙:
  - EmbeddingProvider 인터페이스로 모델 교체 가능
  - OpenAIEmbeddingProvider: text-embedding-3-small (1536차원)
  - 배치 처리로 API 비용 최소화
  - 토큰 사용량 추적 (Admin 비용 현황 조회용)
  - 오류 처리 및 재시도 로직 (최대 3회)
"""

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class EmbeddingResult:
    """임베딩 생성 결과."""
    embeddings: list[list[float]]   # 각 텍스트의 임베딩 벡터
    model: str                       # 사용된 모델명
    total_tokens: int                # 사용된 총 토큰 수
    failed_indices: list[int] = field(default_factory=list)  # 실패한 인덱스


class EmbeddingProvider(ABC):
    """임베딩 모델 추상화 인터페이스."""

    @abstractmethod
    def embed_batch(self, texts: list[str]) -> EmbeddingResult:
        """텍스트 배치를 임베딩으로 변환한다."""
        ...

    @abstractmethod
    def embed_single(self, text: str) -> list[float]:
        """텍스트 하나를 임베딩으로 변환한다."""
        ...

    @property
    @abstractmethod
    def model_name(self) -> str:
        """사용 중인 모델명."""
        ...

    @property
    @abstractmethod
    def dimensions(self) -> int:
        """임베딩 벡터 차원수."""
        ...


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """OpenAI text-embedding-3-small 기반 임베딩 제공자.

    - 배치 처리로 API 비용 최소화
    - 최대 3회 재시도 (지수 백오프)
    - 빈 텍스트 처리: 제로 벡터 반환
    """

    _MAX_RETRIES = 3
    _RETRY_BASE_DELAY = 1.0  # seconds

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        dimensions: Optional[int] = None,
    ):
        self._api_key = api_key or settings.openai_api_key
        self._model = model or settings.embedding_model
        self._dimensions = dimensions or settings.embedding_dimensions
        self._client = None  # lazy init

    def _get_client(self):
        if self._client is None:
            try:
                from openai import OpenAI
                self._client = OpenAI(api_key=self._api_key)
            except ImportError:
                raise RuntimeError(
                    "openai 패키지가 설치되지 않았습니다. pip install openai 를 실행하세요."
                )
        return self._client

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed_batch(self, texts: list[str]) -> EmbeddingResult:
        """텍스트 배치를 임베딩으로 변환한다.

        빈 텍스트는 제로 벡터로 대체한다.
        실패 시 최대 3회 재시도.
        """
        if not texts:
            return EmbeddingResult(embeddings=[], model=self._model, total_tokens=0)

        # 빈 텍스트 인덱스 처리
        empty_indices = {i for i, t in enumerate(texts) if not t or not t.strip()}
        non_empty_texts = [t for i, t in enumerate(texts) if i not in empty_indices]
        non_empty_indices = [i for i in range(len(texts)) if i not in empty_indices]

        embeddings: list[Optional[list[float]]] = [None] * len(texts)
        failed_indices: list[int] = []

        # 빈 텍스트에 제로 벡터 할당
        zero_vec = [0.0] * self._dimensions
        for idx in empty_indices:
            embeddings[idx] = zero_vec

        if non_empty_texts:
            result_embeddings, total_tokens = self._call_openai_with_retry(non_empty_texts)
            if result_embeddings:
                for i, orig_idx in enumerate(non_empty_indices):
                    embeddings[orig_idx] = result_embeddings[i] if i < len(result_embeddings) else zero_vec
            else:
                failed_indices = list(non_empty_indices)
                for idx in non_empty_indices:
                    embeddings[idx] = zero_vec
                total_tokens = 0
        else:
            total_tokens = 0

        return EmbeddingResult(
            embeddings=[e for e in embeddings if e is not None],
            model=self._model,
            total_tokens=total_tokens,
            failed_indices=failed_indices,
        )

    def embed_single(self, text: str) -> list[float]:
        """단건 텍스트 임베딩. 실패 시 제로 벡터 반환."""
        if not text or not text.strip():
            return [0.0] * self._dimensions
        result = self.embed_batch([text])
        return result.embeddings[0] if result.embeddings else [0.0] * self._dimensions

    def _call_openai_with_retry(
        self, texts: list[str]
    ) -> tuple[list[list[float]], int]:
        """OpenAI API 호출 (재시도 포함)."""
        client = self._get_client()
        last_exc: Optional[Exception] = None

        for attempt in range(self._MAX_RETRIES):
            try:
                response = client.embeddings.create(
                    model=self._model,
                    input=texts,
                    dimensions=self._dimensions,
                )
                embeddings = [item.embedding for item in response.data]
                total_tokens = response.usage.total_tokens if response.usage else 0
                return embeddings, total_tokens

            except Exception as exc:
                last_exc = exc
                if attempt < self._MAX_RETRIES - 1:
                    delay = self._RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        "OpenAI embedding API 오류 (시도 %d/%d): %s. %.1f초 후 재시도.",
                        attempt + 1, self._MAX_RETRIES, exc, delay,
                    )
                    time.sleep(delay)
                else:
                    logger.error(
                        "OpenAI embedding API 최대 재시도 초과: %s", exc
                    )

        return [], 0


class LocalEmbeddingProvider(EmbeddingProvider):
    """로컬 임베딩 모델 제공자 (비용 절감용 대안).

    실제 로컬 모델 연동은 향후 구현.
    현재는 빈 리스트 반환 (placeholder) — _save_chunks에서 embedding = NULL로
    저장되어 semantic_search의 'AND embedding IS NOT NULL' 필터에 의해 자동 제외된다.
    zero vector를 반환하면 NULL 필터를 우회하여 무의미한 검색 결과가 생기므로 금지.
    """

    @property
    def model_name(self) -> str:
        return "local-placeholder"

    @property
    def dimensions(self) -> int:
        return settings.embedding_dimensions

    def embed_batch(self, texts: list[str]) -> EmbeddingResult:
        logger.warning(
            "LocalEmbeddingProvider: placeholder 구현 — 실제 벡터가 아닙니다. "
            "청크 %d건이 embedding=NULL로 저장되어 벡터 검색에서 제외됩니다.",
            len(texts),
        )
        return EmbeddingResult(
            embeddings=[[] for _ in texts],  # NULL로 저장 → 벡터 검색 제외
            model=self.model_name,
            total_tokens=0,
        )

    def embed_single(self, text: str) -> list[float]:
        # 빈 리스트 반환 → semantic_search의 zero-vector 검사에서 차단
        return []


class SBERTEmbeddingProvider(EmbeddingProvider):
    """로컬 SBERT 임베딩 서비스 제공자.

    EMBEDDING_SVC_URL(또는 embedding_service_url) 설정 시 사용.
    POST {url}/embed {"texts": [...]} → {"embeddings": [[...], ...]}
    """

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    @property
    def model_name(self) -> str:
        return getattr(settings, "sbert_model_name", "sbert-local")

    @property
    def dimensions(self) -> int:
        return getattr(settings, "embedding_dim", settings.embedding_dimensions)

    def embed_batch(self, texts: list[str]) -> EmbeddingResult:
        import httpx
        url = f"{self._base_url}/embed"
        try:
            resp = httpx.post(
                url,
                json={"texts": texts},
                timeout=getattr(settings, "embedding_svc_timeout", 30),
            )
            resp.raise_for_status()
            data = resp.json()
            embeddings = data.get("embeddings", data.get("data", []))
            return EmbeddingResult(
                embeddings=embeddings,
                model=self.model_name,
                total_tokens=sum(len(t.split()) for t in texts),
            )
        except Exception as exc:
            logger.error("SBERT 임베딩 서비스 오류 (%s): %s", url, exc)
            return EmbeddingResult(embeddings=[[] for _ in texts], model=self.model_name, total_tokens=0)

    def embed_single(self, text: str) -> list[float]:
        result = self.embed_batch([text])
        return result.embeddings[0] if result.embeddings else []


def get_embedding_provider() -> EmbeddingProvider:
    """현재 설정에 따라 적절한 EmbeddingProvider를 반환한다.

    우선순위: SBERT 로컬 서비스 > OpenAI > placeholder
    """
    svc_url = getattr(settings, "embedding_svc_url", "") or getattr(settings, "embedding_service_url", "")
    if svc_url:
        return SBERTEmbeddingProvider(svc_url)
    if settings.openai_api_key:
        return OpenAIEmbeddingProvider()
    logger.warning(
        "임베딩 서비스가 설정되지 않았습니다. LocalEmbeddingProvider(placeholder)를 사용합니다."
    )
    return LocalEmbeddingProvider()
