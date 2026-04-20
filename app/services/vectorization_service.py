"""
벡터화 파이프라인 서비스 — 청킹 + 임베딩 + 저장.

설계 원칙:
  - VectorizationPipeline: 단일 문서 버전을 청크 → 임베딩 → 저장하는 전체 흐름
  - PostgreSQL document_chunks: 메타데이터 + ACL 스냅샷 보관 (embedding 컬럼 없음)
  - Milvus: chunk_id + embedding 보관 (벡터 유사도 검색 전용)
  - 배치 처리로 임베딩 API 비용 최소화 (settings.embedding_batch_size)
  - 권한 메타데이터(ACL 스냅샷) 각 청크에 반영
  - 재색인 시 기존 청크 소프트 삭제 (is_current = false)
  - 배경 작업(background_jobs) 테이블과 연계하여 상태 추적
  - 실패 시 최대 3회 재시도 (임베딩 레벨에서 처리)
"""

import logging
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import psycopg2.extensions
import psycopg2.extras

from app.config import settings
from app.services.chunking_service import DocumentChunk, chunking_service
from app.services.embedding_service import EmbeddingProvider, get_embedding_provider

logger = logging.getLogger(__name__)

_PUBLISHED_ROLES = ["VIEWER", "AUTHOR", "REVIEWER", "APPROVER", "PUBLISHER", "ORG_ADMIN", "SUPER_ADMIN"]
_DRAFT_ROLES = ["AUTHOR", "REVIEWER", "APPROVER", "PUBLISHER", "ORG_ADMIN", "SUPER_ADMIN"]
_ORG_METADATA_KEYS = ("organization_id", "org_id", "organization_ids", "org_ids")
_USER_METADATA_KEYS = ("owner_user_id", "user_id", "owner_user_ids", "accessible_user_ids")


def _normalize_acl_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _extract_acl_values(metadata: dict[str, Any], keys: tuple[str, ...]) -> list[str]:
    values: list[str] = []
    for key in keys:
        values.extend(_normalize_acl_values(metadata.get(key)))
    return list(dict.fromkeys(values))


# ---------------------------------------------------------------------------
# 권한 메타데이터 스냅샷
# ---------------------------------------------------------------------------

@dataclass
class PermissionSnapshot:
    """문서 ACL 스냅샷 — 청크 생성 시점 권한 정보."""
    accessible_roles: list[str] = field(default_factory=list)
    accessible_user_ids: list[str] = field(default_factory=list)
    accessible_org_ids: list[str] = field(default_factory=list)
    is_public: bool = False


def _get_permission_snapshot(
    conn: psycopg2.extensions.connection,
    document_id: str,
) -> PermissionSnapshot:
    """문서의 현재 ACL 스냅샷을 가져온다.

    현재는 문서 상태(published) 기반 단순 권한 모델.
    Phase 2 ACL 정책이 확장되면 이 함수를 수정하여 반영.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, document_type, metadata, created_by FROM documents WHERE id = %s::uuid",
                (document_id,),
            )
            row = cur.fetchone()
            if not row:
                return PermissionSnapshot()

            metadata = row.get("metadata") or {}
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            org_ids = _extract_acl_values(metadata, _ORG_METADATA_KEYS)
            user_ids = _extract_acl_values(metadata, _USER_METADATA_KEYS)
            user_ids.extend(_normalize_acl_values(row.get("created_by")))
            user_ids = list(dict.fromkeys(user_ids))

            visibility = str(metadata.get("visibility") or "").lower()
            is_public = (
                row["status"] == "published"
                and not org_ids
                and visibility not in {"restricted", "private", "internal"}
            )

            if row["status"] == "published":
                return PermissionSnapshot(
                    accessible_roles=_PUBLISHED_ROLES,
                    accessible_user_ids=user_ids,
                    accessible_org_ids=org_ids,
                    is_public=is_public,
                )
            return PermissionSnapshot(
                accessible_roles=_DRAFT_ROLES,
                accessible_user_ids=user_ids,
                accessible_org_ids=org_ids,
                is_public=False,
            )
    except Exception as exc:
        logger.warning("권한 스냅샷 조회 실패 (document_id=%s): %s", document_id, exc)
        return PermissionSnapshot()


# ---------------------------------------------------------------------------
# 벡터화 결과
# ---------------------------------------------------------------------------

@dataclass
class VectorizationResult:
    """벡터화 파이프라인 실행 결과."""
    document_id: str
    version_id: str
    chunks_created: int = 0
    chunks_failed: int = 0
    total_tokens: int = 0
    model: str = ""
    error: Optional[str] = None
    job_id: Optional[str] = None


# ---------------------------------------------------------------------------
# VectorizationPipeline
# ---------------------------------------------------------------------------

class VectorizationPipeline:
    """청킹 → 임베딩 → pgvector 저장 전체 파이프라인."""

    def __init__(self, embedding_provider: Optional[EmbeddingProvider] = None):
        self._embedding_provider = embedding_provider

    def _get_provider(self) -> EmbeddingProvider:
        if self._embedding_provider is None:
            self._embedding_provider = get_embedding_provider()
        return self._embedding_provider

    # ---------------------------------------------------------------------------
    # 단건 문서 버전 벡터화
    # ---------------------------------------------------------------------------

    def vectorize_version(
        self,
        conn: psycopg2.extensions.connection,
        *,
        document_id: str,
        version_id: str,
        job_id: Optional[str] = None,
    ) -> VectorizationResult:
        """문서 버전 하나를 벡터화한다.

        1. 버전 정보 및 노드 조회
        2. DocumentType의 chunking_config 조회
        3. 청킹
        4. 권한 메타데이터 스냅샷
        5. 임베딩 생성 (배치)
        6. 기존 청크 소프트 삭제
        7. 신규 청크 저장
        8. 토큰 사용량 기록
        """
        result = VectorizationResult(document_id=document_id, version_id=version_id, job_id=job_id)

        try:
            # 1. 버전 정보 조회
            version_row = self._fetch_version(conn, version_id, document_id)
            if not version_row:
                result.error = f"버전을 찾을 수 없습니다: {version_id}"
                logger.warning(result.error)
                return result

            document_type = version_row["document_type"]
            document_status = version_row["document_status"]

            # 2. chunking_config 조회
            chunking_config_obj = chunking_service.get_chunking_config_for_type(conn, document_type)

            # 3. 노드 조회 및 청킹
            node_rows = self._fetch_nodes(conn, version_id)
            chunks = chunking_service.chunk_version(
                document_id=document_id,
                version_id=version_id,
                document_type=document_type,
                document_status=document_status,
                node_rows=node_rows,
                chunking_config={
                    "strategy": chunking_config_obj.strategy,
                    "max_chunk_tokens": chunking_config_obj.max_chunk_tokens,
                    "min_chunk_tokens": chunking_config_obj.min_chunk_tokens,
                    "overlap_tokens": chunking_config_obj.overlap_tokens,
                    "include_parent_context": chunking_config_obj.include_parent_context,
                    "index_version_policy": chunking_config_obj.index_version_policy,
                },
            )

            if not chunks:
                logger.info(
                    "벡터화할 청크가 없습니다 (document_id=%s, version_id=%s)",
                    document_id, version_id,
                )
                return result

            # 4. 권한 메타데이터 스냅샷
            perm = _get_permission_snapshot(conn, document_id)
            for chunk in chunks:
                chunk.accessible_roles = perm.accessible_roles
                chunk.accessible_user_ids = perm.accessible_user_ids
                chunk.accessible_org_ids = perm.accessible_org_ids
                chunk.is_public = perm.is_public

            # 5. 임베딩 생성 (배치)
            provider = self._get_provider()
            texts = [c.source_text for c in chunks]
            total_tokens = 0
            embeddings: list[Optional[list[float]]] = []

            batch_size = settings.embedding_batch_size
            for i in range(0, len(texts), batch_size):
                batch_texts = texts[i:i + batch_size]
                emb_result = provider.embed_batch(batch_texts)
                embeddings.extend(emb_result.embeddings)
                total_tokens += emb_result.total_tokens
                if emb_result.failed_indices:
                    logger.warning(
                        "임베딩 실패 청크 수: %d (batch offset=%d)",
                        len(emb_result.failed_indices), i,
                    )

            result.model = provider.model_name
            result.total_tokens = total_tokens

            # 6. 기존 청크 소프트 삭제
            self._soft_delete_existing_chunks(conn, version_id)

            # 7. 신규 청크 저장
            saved, failed = self._save_chunks(conn, chunks, embeddings, provider.model_name)
            result.chunks_created = saved
            result.chunks_failed = failed

            # 8. 토큰 사용량 기록
            if total_tokens > 0:
                self._record_token_usage(
                    conn,
                    document_id=document_id,
                    job_id=job_id,
                    model=provider.model_name,
                    total_tokens=total_tokens,
                    chunk_count=saved,
                )

            logger.info(
                "벡터화 완료: document_id=%s, version_id=%s, chunks=%d, tokens=%d",
                document_id, version_id, saved, total_tokens,
            )

        except Exception as exc:
            result.error = str(exc)
            logger.error(
                "벡터화 실패 (document_id=%s, version_id=%s): %s",
                document_id, version_id, exc,
                exc_info=True,
            )

        return result

    # ---------------------------------------------------------------------------
    # 배치 벡터화 (전체 문서 또는 특정 타입)
    # ---------------------------------------------------------------------------

    def vectorize_all_published(
        self,
        conn: psycopg2.extensions.connection,
        *,
        document_type: Optional[str] = None,
        limit: int = 100,
        job_id: Optional[str] = None,
    ) -> dict:
        """Published 상태인 문서들을 일괄 벡터화한다."""
        params: list = ["published"]
        where_extra = ""
        if document_type:
            where_extra = " AND d.document_type = %s"
            params.append(document_type)

        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT d.id AS document_id, d.current_published_version_id AS version_id
                FROM documents d
                WHERE d.status = %s
                  AND d.current_published_version_id IS NOT NULL
                  {where_extra}
                ORDER BY d.updated_at DESC
                LIMIT %s
                """,
                params + [limit],
            )
            rows = cur.fetchall()

        total = len(rows)
        succeeded = 0
        failed = 0

        for row in rows:
            doc_id = str(row["document_id"])
            ver_id = str(row["version_id"])
            r = self.vectorize_version(conn, document_id=doc_id, version_id=ver_id, job_id=job_id)
            if r.error:
                failed += 1
            else:
                succeeded += 1
            # 연속 API 호출 간 짧은 대기 — OpenAI RPM/TPM Rate Limit 완화
            time.sleep(0.1)

        return {"total": total, "succeeded": succeeded, "failed": failed}

    # ---------------------------------------------------------------------------
    # 권한 메타데이터 갱신 (재임베딩 없이)
    # ---------------------------------------------------------------------------

    def update_permission_metadata(
        self,
        conn: psycopg2.extensions.connection,
        document_id: str,
    ) -> int:
        """문서의 ACL 변경 시 청크 권한 메타데이터만 갱신한다 (벡터 재계산 없음)."""
        perm = _get_permission_snapshot(conn, document_id)
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE document_chunks
                SET
                    accessible_roles = %s,
                    accessible_user_ids = %s,
                    accessible_org_ids = %s,
                    is_public = %s,
                    updated_at = NOW()
                WHERE document_id = %s::uuid AND is_current = TRUE
                """,
                (
                    perm.accessible_roles,
                    perm.accessible_user_ids,
                    perm.accessible_org_ids,
                    perm.is_public,
                    document_id,
                ),
            )
            updated = cur.rowcount
        logger.info("권한 메타데이터 갱신: document_id=%s, updated_chunks=%d", document_id, updated)
        return updated

    # ---------------------------------------------------------------------------
    # 벡터 유사도 검색 (권한 필터링 포함)
    # ---------------------------------------------------------------------------

    def semantic_search(
        self,
        conn: psycopg2.extensions.connection,
        query: str,
        *,
        actor_role: Optional[str] = None,
        actor_user_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        document_type: Optional[str] = None,
        top_k: int = 20,
    ) -> list[dict]:
        """쿼리 텍스트와 유사한 청크를 검색한다.

        1단계: Milvus에서 top_k * 3 후보 chunk_id 검색 (벡터 유사도)
        2단계: PostgreSQL에서 chunk_id IN (...) + ACL 필터로 최종 결과 반환
        """
        from app.db.milvus import get_milvus
        milvus = get_milvus()

        provider = self._get_provider()
        query_embedding = provider.embed_single(query)

        if not query_embedding or not any(query_embedding):
            logger.warning("쿼리 임베딩 생성 실패 — 빈 결과 반환 (query=%s…)", query[:80])
            return []

        if not milvus.is_available():
            logger.warning("Milvus 비활성 — 벡터 검색 불가, 빈 결과 반환")
            return []

        # 1단계: Milvus 벡터 검색 (ACL 무관, top_k * 3 후보)
        candidate_ids = milvus.search(query_embedding, top_k=top_k * 3)
        if not candidate_ids:
            return []

        # 2단계: PostgreSQL ACL 필터 + 메타데이터 조회
        acl_terms = ["is_public = TRUE"]
        acl_params: list = []
        if actor_role:
            acl_terms.append("%s = ANY(accessible_roles)")
            acl_params.append(actor_role)
        if actor_user_id:
            acl_terms.append("%s = ANY(accessible_user_ids)")
            acl_params.append(actor_user_id)
        if organization_id:
            acl_terms.append("%s = ANY(accessible_org_ids)")
            acl_params.append(organization_id)
        role_filter = " AND (" + " OR ".join(acl_terms) + ")"

        type_filter = ""
        type_params: list = []
        if document_type:
            type_filter = " AND document_type = %s"
            type_params = [document_type]

        # Milvus 결과 순서(유사도 내림차순)를 CASE WHEN으로 보존
        id_order = ", ".join(f"'{cid}'" for cid in candidate_ids)
        order_clause = f"CASE id::text {' '.join(f'WHEN {repr(cid)} THEN {idx}' for idx, cid in enumerate(candidate_ids))} ELSE {len(candidate_ids)} END"

        all_params = list(acl_params) + list(type_params) + [top_k]

        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    id,
                    document_id,
                    version_id,
                    node_id,
                    chunk_index,
                    source_text,
                    node_path,
                    document_type,
                    document_status,
                    token_count
                FROM document_chunks
                WHERE is_current = TRUE
                  AND id::text IN ({id_order})
                  {role_filter}
                  {type_filter}
                ORDER BY {order_clause}
                LIMIT %s
                """,
                all_params,
            )
            rows = cur.fetchall()

        # Milvus 유사도는 search 결과 순서로 근사값 부여 (1.0에서 감소)
        id_to_score = {cid: 1.0 - idx * 0.01 for idx, cid in enumerate(candidate_ids)}

        return [
            {
                "chunk_id": str(row["id"]),
                "document_id": str(row["document_id"]),
                "version_id": str(row["version_id"]),
                "node_id": str(row["node_id"]) if row.get("node_id") else None,
                "chunk_index": row["chunk_index"],
                "source_text": row["source_text"],
                "node_path": row["node_path"] or [],
                "document_type": row["document_type"],
                "document_status": row["document_status"],
                "token_count": row["token_count"],
                "similarity": id_to_score.get(str(row["id"]), 0.0),
            }
            for row in rows
        ]

    # ---------------------------------------------------------------------------
    # 내부 유틸
    # ---------------------------------------------------------------------------

    def _fetch_version(
        self,
        conn: psycopg2.extensions.connection,
        version_id: str,
        document_id: str,
    ) -> Optional[dict]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT v.id, v.document_id, v.status AS version_status,
                       d.document_type, d.status AS document_status
                FROM versions v
                JOIN documents d ON v.document_id = d.id
                WHERE v.id = %s::uuid AND v.document_id = %s::uuid
                """,
                (version_id, document_id),
            )
            return cur.fetchone()

    def _fetch_nodes(
        self,
        conn: psycopg2.extensions.connection,
        version_id: str,
    ) -> list[dict]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, parent_id, node_type, order_index, title, content
                FROM nodes
                WHERE version_id = %s::uuid
                ORDER BY order_index ASC
                """,
                (version_id,),
            )
            rows = cur.fetchall()

        if rows:
            return rows

        # nodes 테이블이 비어있으면 content_snapshot에서 파싱 (fallback)
        return self._parse_nodes_from_snapshot(conn, version_id)

    def _parse_nodes_from_snapshot(
        self,
        conn: psycopg2.extensions.connection,
        version_id: str,
    ) -> list[dict]:
        """content_snapshot(ProseMirror JSON)을 nodes 테이블에 삽입하고 row 목록을 반환한다."""
        import uuid as _uuid
        import json as _json

        with conn.cursor() as cur:
            cur.execute(
                "SELECT content_snapshot FROM versions WHERE id = %s::uuid",
                (version_id,),
            )
            row = cur.fetchone()

        if not row or not row["content_snapshot"]:
            return []

        snapshot = row["content_snapshot"]
        if isinstance(snapshot, str):
            try:
                snapshot = _json.loads(snapshot)
            except Exception:
                return []

        top_level = snapshot.get("content", []) if isinstance(snapshot, dict) else []
        node_rows = []
        for order_index, node in enumerate(top_level):
            node_type = node.get("type", "paragraph")
            text_parts = [
                c.get("text", "")
                for c in node.get("content", [])
                if isinstance(c, dict) and c.get("type") == "text"
            ]
            text = " ".join(text_parts).strip()
            if node_type == "heading":
                node_rows.append({
                    "id": str(_uuid.uuid4()),
                    "parent_id": None,
                    "node_type": "heading",
                    "order_index": order_index,
                    "title": text or None,
                    "content": None,
                })
            else:
                node_rows.append({
                    "id": str(_uuid.uuid4()),
                    "parent_id": None,
                    "node_type": node_type,
                    "order_index": order_index,
                    "title": None,
                    "content": text or None,
                })

        # nodes 테이블에 삽입하여 FK 제약 충족
        if node_rows:
            with conn.cursor() as cur:
                for n in node_rows:
                    cur.execute(
                        """
                        INSERT INTO nodes (id, version_id, parent_id, node_type, order_index, title, content)
                        VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO NOTHING
                        """,
                        (
                            n["id"], version_id, n["parent_id"], n["node_type"],
                            n["order_index"], n["title"], n["content"],
                        ),
                    )

        logger.info(
            "content_snapshot에서 %d개 노드 파싱·삽입 (version_id=%s)",
            len(node_rows), version_id,
        )
        return node_rows

    def _soft_delete_existing_chunks(
        self,
        conn: psycopg2.extensions.connection,
        version_id: str,
    ) -> None:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE document_chunks
                SET is_current = FALSE, updated_at = NOW()
                WHERE version_id = %s::uuid AND is_current = TRUE
                """,
                (version_id,),
            )

    def _save_chunks(
        self,
        conn: psycopg2.extensions.connection,
        chunks: list[DocumentChunk],
        embeddings: list[list[float]],
        model_name: str,
    ) -> tuple[int, int]:
        """청크 메타데이터를 PostgreSQL에, 임베딩을 Milvus에 저장한다.

        PostgreSQL: id, source_text, ACL 스냅샷 등 메타데이터
        Milvus: chunk_id + embedding 벡터
        각 INSERT를 개별 savepoint로 감싸 한 청크 실패가 전체 트랜잭션을
        aborted 상태로 만들지 않도록 한다.
        """
        from app.db.milvus import get_milvus
        milvus = get_milvus()

        saved = 0
        failed = 0
        milvus_records: list[dict] = []

        with conn.cursor() as cur:
            for i, chunk in enumerate(chunks):
                try:
                    cur.execute("SAVEPOINT sp_chunk")
                    embedding = embeddings[i] if i < len(embeddings) else None

                    cur.execute(
                        """
                        INSERT INTO document_chunks (
                            document_id, version_id, node_id,
                            chunk_index, source_text, embedding_model,
                            token_count, node_path, document_type, document_status,
                            accessible_roles, accessible_user_ids, accessible_org_ids,
                            is_public, is_current
                        ) VALUES (
                            %s::uuid, %s::uuid, %s::uuid,
                            %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, %s, %s,
                            %s, TRUE
                        )
                        RETURNING id
                        """,
                        (
                            chunk.document_id,
                            chunk.version_id,
                            chunk.node_id,
                            chunk.chunk_index,
                            chunk.source_text,
                            model_name if embedding else None,
                            chunk.token_count,
                            chunk.node_path,
                            chunk.document_type,
                            chunk.document_status,
                            chunk.accessible_roles,
                            chunk.accessible_user_ids,
                            chunk.accessible_org_ids,
                            chunk.is_public,
                        ),
                    )
                    row = cur.fetchone()
                    chunk_id = str(row["id"]) if row else None
                    cur.execute("RELEASE SAVEPOINT sp_chunk")
                    saved += 1

                    if chunk_id and embedding and any(embedding):
                        milvus_records.append({"chunk_id": chunk_id, "embedding": embedding})

                except Exception as exc:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_chunk")
                    logger.error("청크 저장 실패 (index=%d): %s", i, exc)
                    failed += 1

        # Milvus 배치 upsert (PostgreSQL 커밋 후)
        if milvus_records:
            try:
                milvus.upsert_batch(milvus_records)
                logger.info("Milvus upsert 완료: %d 벡터", len(milvus_records))
            except Exception as exc:
                logger.error("Milvus upsert 실패 (벡터 검색 불가): %s", exc)

        return saved, failed

    def _record_token_usage(
        self,
        conn: psycopg2.extensions.connection,
        *,
        document_id: str,
        job_id: Optional[str],
        model: str,
        total_tokens: int,
        chunk_count: int,
    ) -> None:
        try:
            with conn.cursor() as cur:
                cur.execute("SAVEPOINT sp_token_usage")
                try:
                    cur.execute(
                        """
                        INSERT INTO embedding_token_usage (job_id, document_id, model, total_tokens, chunk_count)
                        VALUES (%s::uuid, %s::uuid, %s, %s, %s)
                        """,
                        (job_id, document_id, model, total_tokens, chunk_count),
                    )
                    cur.execute("RELEASE SAVEPOINT sp_token_usage")
                except Exception as exc:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_token_usage")
                    logger.warning("토큰 사용량 기록 실패: %s", exc)
        except Exception as exc:
            logger.warning("토큰 사용량 기록 실패 (savepoint 오류): %s", exc)

    # ---------------------------------------------------------------------------
    # 청크 cleanup (소프트 삭제된 청크 물리 삭제)
    # ---------------------------------------------------------------------------

    def cleanup_old_chunks(
        self,
        conn: psycopg2.extensions.connection,
        days_old: int = 30,
    ) -> int:
        """is_current=FALSE이고 오래된 청크를 물리 삭제한다."""
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM document_chunks
                WHERE is_current = FALSE
                  AND updated_at < NOW() - make_interval(days => %s)
                """,
                (days_old,),
            )
            deleted = cur.rowcount
        logger.info("청크 cleanup: %d건 삭제 (days_old=%d)", deleted, days_old)
        return deleted


vectorization_pipeline = VectorizationPipeline()
