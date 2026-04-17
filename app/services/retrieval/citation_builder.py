"""
Citation 생성 로직 — Phase 2 FG2.1

Retriever가 document_chunks에서 청크를 조회할 때마다 CitationBuilder를 호출하여
Citation 5-tuple을 자동 생성한다.
S1 RetrievedChunk(dataclass)와 schemas.rag.RetrievedChunk(Pydantic) 모두 처리한다.
"""
from __future__ import annotations

import hashlib  # sha256 content hash (delegated to Citation.from_chunk)
import logging
from typing import Optional, Union
from uuid import UUID

from app.schemas.citation import Citation

logger = logging.getLogger(__name__)

# node_id가 None일 때 사용하는 nil UUID (0으로 채운 UUID)
_NIL_NODE_ID = UUID(int=0)


class CitationBuilder:
    """청크 메타데이터 → Citation 변환기."""

    @staticmethod
    def build(
        document_id: Union[str, UUID],
        version_id: Union[str, UUID],
        node_id: Union[str, UUID, None],
        source_text: str,
        span_offset: Optional[int] = None,
    ) -> Citation:
        """청크 메타데이터로부터 Citation을 생성한다.

        Args:
            document_id: 문서 UUID
            version_id: 버전 UUID
            node_id: 노드 UUID. None이면 nil UUID(0)로 대체.
            source_text: 청크 원문 (SHA-256 계산 대상)
            span_offset: 청크 내 문자 오프셋 (단락 단위 인용 시 None)

        Returns:
            Citation 5-tuple 객체

        Raises:
            ValueError: source_text가 빈 문자열인 경우
        """
        if not source_text:
            raise ValueError("source_text must not be empty")

        # node_id None 처리: nil UUID로 대체
        if node_id is None:
            resolved_node_id = _NIL_NODE_ID
        else:
            try:
                resolved_node_id = UUID(str(node_id))
            except (ValueError, AttributeError) as exc:
                raise ValueError(
                    f"node_id={node_id!r}를 UUID로 변환할 수 없습니다: {exc}"
                ) from exc

        citation = Citation.from_chunk(
            document_id=document_id,
            version_id=version_id,
            node_id=resolved_node_id,
            source_text=source_text,
            span_offset=span_offset,
        )
        logger.debug(
            "Citation built: doc=%s ver=%s node=%s hash=%s...",
            document_id,
            version_id,
            node_id,
            citation.content_hash[:8],
        )
        return citation

    @staticmethod
    def from_retrieved_chunk(chunk: object) -> Citation:
        """S1 RetrievedChunk 객체로부터 Citation을 생성한다.

        S1 RetrievedChunk 필드 매핑:
          chunk.document_id  → Citation.document_id
          chunk.version_id   → Citation.version_id
          chunk.node_id      → Citation.node_id  (None이면 nil UUID)
          chunk.source_text  → SHA-256 계산

        Args:
            chunk: S1 RetrievedChunk 객체 (dataclass 또는 Pydantic model)
                   필수 속성: document_id, version_id, node_id, source_text

        Returns:
            Citation 5-tuple 객체

        Raises:
            ValueError: 필수 속성 누락 또는 source_text 빈 문자열
        """
        for attr in ("document_id", "version_id", "source_text"):
            if not hasattr(chunk, attr):
                raise ValueError(
                    f"RetrievedChunk에 필수 속성 '{attr}'이 없습니다."
                )

        node_id = getattr(chunk, "node_id", None)
        if node_id is None:
            logger.warning(
                "node_id가 None인 청크 (document_id=%s) — nil UUID로 대체",
                getattr(chunk, "document_id", "?"),
            )

        return CitationBuilder.build(
            document_id=chunk.document_id,
            version_id=chunk.version_id,
            node_id=node_id,
            source_text=chunk.source_text,
        )
