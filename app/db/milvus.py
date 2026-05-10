"""
Milvus 클라이언트 싱글톤 — pymilvus 기반.

컬렉션 스키마:
  - chunk_id   : VARCHAR(36) PK  — PostgreSQL document_chunks.id
  - embedding  : FLOAT_VECTOR    — 임베딩 벡터 (차원은 settings.embedding_dim)

검색 전략:
  1. Milvus에서 top_k * 2 후보 chunk_id 검색 (벡터 유사도)
  2. PostgreSQL에서 chunk_id IN (...) + ACL 필터로 최종 결과 반환

S2 원칙 ⑦: 폐쇄망에서도 동작 — MILVUS_HOST 미설정 시 _NullMilvusClient로
  폴백해 서비스가 degrade되지만 실패하지 않음.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_client: Optional["_MilvusClientWrapper"] = None
COLLECTION_NAME = "document_chunks"


class _NullMilvusClient:
    """Milvus 연결 불가 시 폴백 — 항상 빈 결과 반환."""

    def is_available(self) -> bool:
        return False

    def upsert(self, chunk_id: str, embedding: list[float]) -> None:
        pass

    def delete(self, chunk_ids: list[str]) -> None:
        pass

    def search(self, embedding: list[float], top_k: int) -> list[str]:
        return []

    def search_with_score(
        self, embedding: list[float], top_k: int,
    ) -> list[tuple[str, float]]:
        return []


class _MilvusClientWrapper:
    def __init__(self, host: str, port: int, dim: int, user: str = "", password: str = "") -> None:
        from pymilvus import MilvusClient as _MC
        uri = f"http://{host}:{port}"
        if user and password:
            self._client = _MC(uri=uri, token=f"{user}:{password}")
        else:
            self._client = _MC(uri=uri)
        self._dim = dim
        self._ensure_collection()

    def is_available(self) -> bool:
        return True

    def _ensure_collection(self) -> None:
        from pymilvus import DataType
        if self._client.has_collection(COLLECTION_NAME):
            return
        schema = self._client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("chunk_id", DataType.VARCHAR, max_length=36, is_primary=True)
        schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=self._dim)

        index_params = self._client.prepare_index_params()
        index_params.add_index(
            field_name="embedding",
            index_type="HNSW",
            metric_type="COSINE",
            params={"M": 16, "efConstruction": 64},
        )
        self._client.create_collection(
            collection_name=COLLECTION_NAME,
            schema=schema,
            index_params=index_params,
        )
        logger.info("Milvus 컬렉션 '%s' 생성 완료 (dim=%d)", COLLECTION_NAME, self._dim)

    def upsert(self, chunk_id: str, embedding: list[float]) -> None:
        self._client.upsert(
            collection_name=COLLECTION_NAME,
            data=[{"chunk_id": chunk_id, "embedding": embedding}],
        )

    def upsert_batch(self, records: list[dict]) -> None:
        """[{"chunk_id": ..., "embedding": [...]}] 배치 upsert."""
        if records:
            self._client.upsert(collection_name=COLLECTION_NAME, data=records)

    def delete(self, chunk_ids: list[str]) -> None:
        if not chunk_ids:
            return
        ids_str = ", ".join(f'"{cid}"' for cid in chunk_ids)
        self._client.delete(
            collection_name=COLLECTION_NAME,
            filter=f"chunk_id in [{ids_str}]",
        )

    def search(self, embedding: list[float], top_k: int) -> list[str]:
        return [chunk_id for chunk_id, _ in self.search_with_score(embedding, top_k)]

    def search_with_score(
        self, embedding: list[float], top_k: int,
    ) -> list[tuple[str, float]]:
        """Milvus 벡터 유사도 검색 — (chunk_id, similarity) 튜플 반환.

        similarity 정의: ``1 - distance`` (metric_type=COSINE 기준).
          - PyMilvus 2.4+ 의 COSINE metric 은 distance 를 ``1 - cos_sim`` 로 반환.
          - 따라서 similarity = 1 - distance 가 직관적 cosine 유사도 (1=동일, 0=무관, -1=정반대).
          - VectorRetriever 의 ``similarity_threshold`` 와 직접 비교 가능.
        """
        results = self._client.search(
            collection_name=COLLECTION_NAME,
            data=[embedding],
            anns_field="embedding",
            search_params={"metric_type": "COSINE", "params": {"ef": 64}},
            limit=top_k,
            output_fields=["chunk_id"],
        )
        if not results:
            return []
        out: list[tuple[str, float]] = []
        for hit in results[0]:
            chunk_id = hit["entity"]["chunk_id"]
            # PyMilvus hit 는 dict-like — distance 키 우선, 없으면 0.0 fallback
            distance = float(hit.get("distance", 0.0)) if hasattr(hit, "get") else 0.0
            similarity = 1.0 - distance
            out.append((chunk_id, similarity))
        return out


def get_milvus() -> _MilvusClientWrapper | _NullMilvusClient:
    """Milvus 클라이언트 싱글톤을 반환한다.

    NullClient 상태이면 재연결을 시도한다 (서버 시작 시 Milvus가 아직
    준비되지 않았다가 나중에 올라오는 경우를 처리).
    """
    global _client
    if _client is None or isinstance(_client, _NullMilvusClient):
        _client = _init_milvus()
    return _client


def _init_milvus() -> _MilvusClientWrapper | _NullMilvusClient:
    try:
        from app.config import settings
        host = settings.milvus_host
        port = settings.milvus_port
        dim = settings.embedding_dim
        user = settings.milvus_user
        password = settings.milvus_password
        if not host:
            logger.warning("MILVUS_HOST 미설정 — 벡터 검색 비활성 (NullClient)")
            return _NullMilvusClient()
        client = _MilvusClientWrapper(host=host, port=port, dim=dim, user=user, password=password)
        logger.info("Milvus 연결 성공 (%s:%d, dim=%d, auth=%s)", host, port, dim, bool(user))
        return client
    except Exception as exc:
        logger.warning("Milvus 초기화 실패 (%s) — NullClient 폴백", exc)
        return _NullMilvusClient()
