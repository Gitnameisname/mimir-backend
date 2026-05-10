"""
Wikilink resolver — S3 Phase 2 FG 2-3.

``[[제목]]`` 토큰의 ``raw_text`` 를 ``documents.title`` 과 매칭해
``to_document_id`` / ``resolved_status`` 를 결정한다.

매칭 정책 (task2-3.md §2.1 (3))
-------------------------------
1. viewer Scope 안에서 title 완전 일치 문서 1개 → ``resolved`` (해당 doc id)
2. 완전 일치 복수 → ``ambiguous`` (해결 안 함, to_document_id = None)
3. 일치 없음 → ``missing`` (to_document_id = None)

NFC 정규화
----------
``raw_text`` 와 ``documents.title`` 모두 NFC 정규화 후 비교.
한글 조합형/완성형 차이 흡수 (task2-3.md §8 R-04, FG2-3_Pre-flight_갱신.md §2.6).

ACL — 정본은 외부에서 결정
--------------------------
Resolver 는 자체 ACL 결정 안 함. 호출자가 ``viewer_scope_profile_ids`` 를 keyword-only
required 로 전달. ``None`` = 필터 skip (admin/내부), ``[]`` = 결과 없음, ``[ids]`` = IN
(``DocumentLinksRepository.find_candidates_by_title`` 의 정책 그대로).

존재 유출 방지
--------------
viewer Scope 외 문서가 같은 제목을 가져도 후보로 들어가지 않는다 → 사용자는 ``missing`` 으로만
관측. 다른 사용자의 문서 존재 자체가 누설되지 않음 (task2-3.md §9 보안 보고서 항목).
"""

from __future__ import annotations

import logging
import unicodedata
from typing import Any, Optional, Sequence

import psycopg2.extensions

from app.models.document_link import ResolvedStatus
from app.repositories.document_links_repository import (
    DocumentLinksRepository,
    document_links_repository as _default_repository,
)

logger = logging.getLogger(__name__)


def normalize_title(s: str) -> str:
    """비교용 NFC 정규화 + 양쪽 공백 strip.

    documents.title 과 raw_text 양쪽에 같은 함수를 적용해 비교 시 한글
    조합형/완성형 차이를 흡수.
    """
    return unicodedata.normalize("NFC", (s or "").strip())


def resolve_wikilinks(
    conn: psycopg2.extensions.connection,
    *,
    from_document_id: str,
    links: Sequence[tuple[str, str]],
    viewer_scope_profile_ids: Optional[Sequence[str]],
    repository: DocumentLinksRepository = _default_repository,
) -> list[tuple[Optional[str], str, str, ResolvedStatus]]:
    """``raw_text`` 를 ``documents.title`` 로 매칭해 4-튜플 list 반환.

    Args:
        conn: DB connection (psycopg2)
        from_document_id: 출발 문서 id (디버그/로깅 용도)
        links: ``(raw_text, node_id)`` 튜플 list — ``extract_wikilinks_from_snapshot`` 결과
        viewer_scope_profile_ids: 매칭 시 적용할 viewer Scope 필터.
            ``None`` = 필터 skip (admin/내부), ``[]`` = 결과 없음, ``[ids]`` = IN.
        repository: DI 용 repository (테스트에서 주입)

    Returns:
        ``[(to_document_id_or_None, node_id, raw_text, resolved_status), ...]``
        ``DocumentLinksRepository.replace_for_document`` 의 ``rows`` 인자에 그대로 전달 가능.
    """
    if not links:
        return []

    # raw_text 별로 후보 cache (같은 raw_text 가 여러 node 에서 등장 시 1회만 조회)
    candidate_cache: dict[str, list[dict[str, Any]]] = {}

    result: list[tuple[Optional[str], str, str, ResolvedStatus]] = []
    for raw_text, node_id in links:
        title_norm = normalize_title(raw_text)
        if not title_norm:
            # extract_wikilinks_from_snapshot 가 이미 strip 후 빈 문자열 거부했지만
            # NFC 정규화 후 빈 문자열일 가능성 — 안전망
            result.append((None, node_id, raw_text, "missing"))
            continue

        if title_norm not in candidate_cache:
            candidate_cache[title_norm] = repository.find_candidates_by_title(
                conn,
                title=title_norm,
                viewer_scope_profile_ids=viewer_scope_profile_ids,
            )

        candidates = candidate_cache[title_norm]

        # NFC 정규화 후 비교 — repository 가 raw 비교를 했을 수 있어 한 번 더 필터
        # (DB 에 저장된 title 이 NFC 정규화돼있지 않을 수 있음)
        normalized_candidates = [
            c for c in candidates if normalize_title(c["title"]) == title_norm
        ]

        if not normalized_candidates:
            status: ResolvedStatus = "missing"
            to_doc: Optional[str] = None
        elif len(normalized_candidates) == 1:
            status = "resolved"
            to_doc = normalized_candidates[0]["id"]
        else:
            status = "ambiguous"
            to_doc = None

        result.append((to_doc, node_id, raw_text, status))

    if logger.isEnabledFor(logging.INFO):
        resolved = sum(1 for r in result if r[3] == "resolved")
        ambiguous = sum(1 for r in result if r[3] == "ambiguous")
        missing = sum(1 for r in result if r[3] == "missing")
        logger.info(
            "resolve_wikilinks — from_document_id=%s, total=%d, "
            "resolved=%d, ambiguous=%d, missing=%d",
            from_document_id, len(result), resolved, ambiguous, missing,
        )
    return result


__all__ = ["normalize_title", "resolve_wikilinks"]
