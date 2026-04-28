"""
Natural-language document reference → document_id + version_ref resolver.

S3 Phase 4 FG 4-2 §2.1.3 — `resolve_document_reference` 도구의 도메인 코어.

5단계 해소 로직 (앞 단계가 충족 시 즉시 best_match 확정):
  1. exact_title       — 제목 정확 일치 (대소문자 무시)              confidence 0.99
  2. alias             — documents.metadata.aliases 일치             confidence 0.95
  3. recent_context    — context.recent_document_ids 안에서 부분 일치 confidence 0.85
  4. semantic          — 벡터 검색 (top-K, cosine 정규화)            confidence ∈ [0.0, 0.94]
  5. fts_fallback      — MIMIR_OFFLINE=1 일 때 4) 대체 (FTS rank 정규화)

ACL: 모든 단계에서 ScopeProfile 필터를 통과한 문서만 후보로.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


_OFFLINE_ENV = "MIMIR_OFFLINE"
_DEFAULT_FALLBACK_THRESHOLD = 0.90  # FTS fallback 은 더 보수적 (§2.1.3)


@dataclass
class _Candidate:
    document_id: str
    title: str
    confidence: float
    match_kind: str
    version_ref: str = "latest_published"


def _is_offline() -> bool:
    return os.environ.get(_OFFLINE_ENV, "").strip().lower() in {"1", "true", "yes"}


def _normalize(text: str) -> str:
    return (text or "").strip().lower()


def _safe_ts_query(q: str) -> str:
    """단순 FTS query 정규화 — search_service 와 같은 패턴."""
    parts = [p for p in q.split() if p.strip()]
    return " & ".join(parts) if parts else ""


# ---------------------------------------------------------------------------
# 단계별 후보 수집
# ---------------------------------------------------------------------------


def _stage_exact_title(
    conn, reference: str, *, allowed_doc_ids: Optional[set[str]],
) -> list[_Candidate]:
    norm = _normalize(reference)
    if not norm:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, title
            FROM documents
            WHERE LOWER(title) = %s
            ORDER BY updated_at DESC
            LIMIT 10
            """,
            (norm,),
        )
        rows = cur.fetchall() or []
    cands: list[_Candidate] = []
    for r in rows:
        doc_id = str(r["id"])
        if allowed_doc_ids is not None and doc_id not in allowed_doc_ids:
            continue
        cands.append(
            _Candidate(
                document_id=doc_id,
                title=r["title"],
                confidence=0.99,
                match_kind="exact_title",
            )
        )
    return cands


def _stage_alias(
    conn, reference: str, *, allowed_doc_ids: Optional[set[str]],
) -> list[_Candidate]:
    """documents.metadata.aliases 또는 metadata.previous_titles 의 일치 검사.

    metadata 가 JSONB / dict / 미존재 어떤 경우든 안전 (try/except + 빈 리스트 fallback).
    """
    norm = _normalize(reference)
    if not norm:
        return []
    cands: list[_Candidate] = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, title, metadata
                FROM documents
                WHERE metadata IS NOT NULL
                  AND (
                    metadata::jsonb @? '$.aliases[*]'
                    OR metadata::jsonb @? '$.previous_titles[*]'
                  )
                ORDER BY updated_at DESC
                LIMIT 50
                """
            )
            rows = cur.fetchall() or []
    except Exception as exc:
        logger.debug("alias stage skipped (metadata 미지원): %s", exc)
        return []

    for r in rows:
        doc_id = str(r["id"])
        if allowed_doc_ids is not None and doc_id not in allowed_doc_ids:
            continue
        meta = r.get("metadata") or {}
        if isinstance(meta, str):
            try:
                import json
                meta = json.loads(meta)
            except Exception:
                meta = {}
        candidates = []
        for key in ("aliases", "previous_titles"):
            v = meta.get(key) if isinstance(meta, dict) else None
            if isinstance(v, list):
                candidates.extend(str(x) for x in v if x)
        if any(_normalize(x) == norm for x in candidates):
            cands.append(
                _Candidate(
                    document_id=doc_id,
                    title=r["title"],
                    confidence=0.95,
                    match_kind="alias",
                )
            )
    return cands


def _stage_recent_context(
    conn,
    reference: str,
    *,
    recent_document_ids: list[str],
    allowed_doc_ids: Optional[set[str]],
) -> list[_Candidate]:
    if not recent_document_ids:
        return []
    norm = _normalize(reference)
    cands: list[_Candidate] = []
    placeholders = ", ".join(["%s::uuid"] * len(recent_document_ids))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, title FROM documents
            WHERE id IN ({placeholders})
            """,
            tuple(recent_document_ids),
        )
        rows = cur.fetchall() or []
    for r in rows:
        doc_id = str(r["id"])
        if allowed_doc_ids is not None and doc_id not in allowed_doc_ids:
            continue
        title_norm = _normalize(r["title"] or "")
        # 부분 일치 (양방향) — 토큰 1개 이상 공유 시 후보
        ref_tokens = set(norm.split())
        title_tokens = set(title_norm.split())
        if ref_tokens & title_tokens or norm in title_norm or title_norm in norm:
            cands.append(
                _Candidate(
                    document_id=doc_id,
                    title=r["title"],
                    confidence=0.85,
                    match_kind="recent_context",
                )
            )
    return cands


def _stage_semantic_or_fts(
    conn,
    reference: str,
    *,
    max_candidates: int,
    preferred_doc_types: Optional[list[str]],
    allowed_doc_ids: Optional[set[str]],
) -> list[_Candidate]:
    """벡터 검색 또는 폐쇄망에서 FTS fallback.

    벡터 검색 인프라가 가용하지 않거나 MIMIR_OFFLINE=1 이면 FTS fallback.
    """
    if _is_offline():
        return _fts_fallback(
            conn,
            reference,
            max_candidates=max_candidates,
            preferred_doc_types=preferred_doc_types,
            allowed_doc_ids=allowed_doc_ids,
        )

    # 벡터 검색 시도 — search_service.search_documents_hybrid 위임 가능 시 시도.
    # 실패 시 FTS 로 fallback (벡터 인프라 부재 / 임베딩 미생성 / 외부 의존 장애).
    try:
        return _vector_semantic(
            conn,
            reference,
            max_candidates=max_candidates,
            preferred_doc_types=preferred_doc_types,
            allowed_doc_ids=allowed_doc_ids,
        )
    except Exception as exc:
        logger.info("semantic stage fallback to FTS: %s", exc)
        return _fts_fallback(
            conn,
            reference,
            max_candidates=max_candidates,
            preferred_doc_types=preferred_doc_types,
            allowed_doc_ids=allowed_doc_ids,
        )


def _vector_semantic(
    conn,
    reference: str,
    *,
    max_candidates: int,
    preferred_doc_types: Optional[list[str]],
    allowed_doc_ids: Optional[set[str]],
) -> list[_Candidate]:
    """search_service.SearchService.search_documents_hybrid 위임.

    벡터 결과의 rank 를 [0.0, 0.94] 로 정규화 (exact_title=0.99 / alias=0.95 보다 보수적).
    """
    from app.services.search_service import SearchService

    svc = SearchService()
    try:
        raw = svc.search_documents_hybrid(
            conn=conn,
            q=reference,
            limit=max_candidates,
            doc_type=(preferred_doc_types[0] if preferred_doc_types else None),
        )
    except Exception:
        # 인터페이스 호환 안 되거나 미구현 — FTS fallback
        raise
    items = raw.results if hasattr(raw, "results") else (raw or [])
    cands: list[_Candidate] = []
    if not items:
        return cands
    raw_scores = [float(getattr(it, "rank", 0) or getattr(it, "score", 0) or 0.0) for it in items]
    max_score = max(raw_scores) if raw_scores else 1.0
    for it, raw_score in zip(items, raw_scores):
        doc_id = str(getattr(it, "id", "") or getattr(it, "document_id", "") or "")
        if not doc_id:
            continue
        if allowed_doc_ids is not None and doc_id not in allowed_doc_ids:
            continue
        title = str(getattr(it, "title", "") or getattr(it, "document_title", "") or "")
        # 정규화: 최대 점수 기준으로 [0.0, 0.94]
        confidence = min(0.94, (raw_score / max_score) * 0.94 if max_score > 0 else 0.0)
        cands.append(
            _Candidate(
                document_id=doc_id,
                title=title,
                confidence=confidence,
                match_kind="semantic",
            )
        )
    return cands


def _fts_fallback(
    conn,
    reference: str,
    *,
    max_candidates: int,
    preferred_doc_types: Optional[list[str]],
    allowed_doc_ids: Optional[set[str]],
) -> list[_Candidate]:
    """FTS 만으로 후보 산출. 벡터 인프라 없이 동작 (S2 ⑦)."""
    ts_query = _safe_ts_query(reference)
    if not ts_query:
        return []
    where = ["d.search_vector @@ to_tsquery('simple', %s)"]
    params: list[Any] = [ts_query]
    if preferred_doc_types:
        placeholders = ", ".join(["%s"] * len(preferred_doc_types))
        where.append(f"d.document_type IN ({placeholders})")
        params.extend([t.upper() for t in preferred_doc_types])
    sql = f"""
        SELECT d.id, d.title,
               ts_rank(d.search_vector, to_tsquery('simple', %s)) AS rank
        FROM documents d
        WHERE {' AND '.join(where)}
        ORDER BY rank DESC
        LIMIT %s
    """
    params_full = params + [ts_query, max_candidates]
    # ORDER BY 의 ts_query 가 한 번 더 들어가야 함 — 위 SQL 에 placeholder 추가
    sql = f"""
        SELECT d.id, d.title,
               ts_rank(d.search_vector, to_tsquery('simple', %s)) AS rank
        FROM documents d
        WHERE {' AND '.join(where)}
        ORDER BY rank DESC
        LIMIT %s
    """
    final_params = [ts_query] + params + [max_candidates]
    cands: list[_Candidate] = []
    try:
        with conn.cursor() as cur:
            cur.execute(sql, final_params)
            rows = cur.fetchall() or []
    except Exception as exc:
        logger.warning("FTS fallback failed: %s", exc)
        return []
    if not rows:
        return cands
    raw_scores = [float(r.get("rank") or 0) for r in rows]
    max_score = max(raw_scores) if raw_scores else 1.0
    for r, raw_score in zip(rows, raw_scores):
        doc_id = str(r["id"])
        if allowed_doc_ids is not None and doc_id not in allowed_doc_ids:
            continue
        # FTS fallback 은 더 보수적 — 최대 0.90 (§2.1.3)
        confidence = min(_DEFAULT_FALLBACK_THRESHOLD, (raw_score / max_score) * _DEFAULT_FALLBACK_THRESHOLD if max_score > 0 else 0.0)
        cands.append(
            _Candidate(
                document_id=doc_id,
                title=r["title"],
                confidence=confidence,
                match_kind="fts_fallback",
            )
        )
    return cands


# ---------------------------------------------------------------------------
# 통합 진입점
# ---------------------------------------------------------------------------


@dataclass
class ResolveResult:
    resolved: bool
    needs_disambiguation: bool
    best_match: Optional[_Candidate]
    candidates: list[_Candidate]


def resolve_reference(
    conn,
    reference: str,
    *,
    recent_document_ids: Optional[list[str]] = None,
    preferred_doc_types: Optional[list[str]] = None,
    max_candidates: int = 5,
    confidence_threshold: float = 0.85,
    allowed_doc_ids: Optional[set[str]] = None,
) -> ResolveResult:
    """5단계 해소.

    Args:
        allowed_doc_ids: ScopeProfile 통과 문서 id 집합. None 이면 ACL 미적용 (admin/내부).

    Returns:
        ResolveResult — best_match 가 confidence_threshold 이상이면 resolved=True,
        그 외엔 needs_disambiguation=True + candidates 만.
    """
    recent_document_ids = list(recent_document_ids or [])

    # 단계별 누적 (앞 단계의 confidence 가 높음)
    all_cands: list[_Candidate] = []

    # Stage 1
    cands = _stage_exact_title(conn, reference, allowed_doc_ids=allowed_doc_ids)
    all_cands.extend(cands)
    # Stage 2
    cands = _stage_alias(conn, reference, allowed_doc_ids=allowed_doc_ids)
    all_cands.extend(cands)
    # Stage 3
    cands = _stage_recent_context(
        conn,
        reference,
        recent_document_ids=recent_document_ids,
        allowed_doc_ids=allowed_doc_ids,
    )
    all_cands.extend(cands)
    # Stage 4 / 5 (semantic 또는 FTS fallback)
    cands = _stage_semantic_or_fts(
        conn,
        reference,
        max_candidates=max_candidates,
        preferred_doc_types=preferred_doc_types,
        allowed_doc_ids=allowed_doc_ids,
    )
    all_cands.extend(cands)

    # 같은 document_id 의 중복 — 가장 높은 confidence 만 보존
    by_doc: dict[str, _Candidate] = {}
    for c in all_cands:
        prev = by_doc.get(c.document_id)
        if prev is None or c.confidence > prev.confidence:
            by_doc[c.document_id] = c
    deduped = sorted(by_doc.values(), key=lambda c: c.confidence, reverse=True)[:max_candidates]

    if not deduped:
        return ResolveResult(
            resolved=False,
            needs_disambiguation=False,
            best_match=None,
            candidates=[],
        )

    best = deduped[0]
    if best.confidence >= confidence_threshold:
        return ResolveResult(
            resolved=True,
            needs_disambiguation=False,
            best_match=best,
            candidates=deduped,
        )

    return ResolveResult(
        resolved=False,
        needs_disambiguation=True,
        best_match=None,
        candidates=deduped,
    )
