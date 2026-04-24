"""
Diff 서비스 계층 — Phase 9.

구성 요소:
  - normalize_content()    : 노드 내용 정규화 유틸리티
  - TextDiffer             : 단어 단위 텍스트 인라인 diff (LCS 기반)
  - NodeDiffer             : Node 트리 구조 diff (ID 기반 매칭)
  - DiffSummaryGenerator   : DiffResult → 자연어 변경 요약
  - DiffService            : 외부 진입점 (버전 두 개 → DiffResult)

설계 원칙:
  - 버전 불변성 활용: 동일 버전 쌍의 diff 결과는 항상 동일 → 캐싱 가능
  - 트리 diff: 노드 ID 기반 매칭 (Myers 보다 단순하고 정확)
  - 텍스트 diff: 단어 단위 LCS (한국어 가독성 확보)
  - 권한 검증은 router/service 진입점에서 처리 (이 모듈은 순수 로직)
"""

import json
import logging
import re
from typing import Any, Optional

import psycopg2.extensions

from app.api.errors.exceptions import ApiNotFoundError, ApiValidationError
from app.models.node import Node
from app.repositories.nodes_repository import nodes_repository
from app.repositories.versions_repository import versions_repository
from app.schemas.diff import (
    ChangeType,
    ChangedSection,
    DiffResult,
    DiffSeverity,
    DiffSummary,
    DiffSummaryResponse,
    InlineDiffToken,
    MoveInfo,
    MoveType,
    NodeDiff,
    NodeSnapshot,
    VersionRef,
)

logger = logging.getLogger(__name__)

# 단일 diff 처리 최대 노드 수
MAX_NODES_SYNC = 10_000

# 인라인 diff 기본 최대 텍스트 길이 (문자)
DEFAULT_MAX_INLINE_LENGTH = 5_000

# LCS DP 테이블 최대 셀 수 (메모리/CPU DoS 방지)
# n*m > MAX_LCS_CELLS 이면 인라인 diff를 건너뜀 (skipped=True 반환)
# 1,000,000셀 ≈ 최대 ~8MB RAM, ~1,000토큰×1,000토큰 수준
MAX_LCS_CELLS = 1_000_000

# 최상위 섹션 최대 표시 수
MAX_CHANGED_SECTIONS = 10

# 재귀 부모 탐색 최대 깊이 (무한 루프 방지)
MAX_PARENT_DEPTH = 50


# ---------------------------------------------------------------------------
# 내용 정규화 유틸리티
# ---------------------------------------------------------------------------


def normalize_content(value: Any) -> str:
    """노드 내용(content/metadata)을 비교용 정규화 문자열로 변환.

    - dict/list → 키 순서 정렬된 JSON 문자열
    - str → 후행 공백 제거
    - None → ""
    """
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    return str(value).rstrip()


def _node_to_snapshot(node: Node) -> NodeSnapshot:
    return NodeSnapshot(
        node_id=node.id,
        node_type=node.node_type,
        title=node.title,
        content=node.content,
        parent_id=node.parent_id,
        order=node.order_index,
        metadata=node.metadata or {},
    )


def _node_content_key(node: Node) -> str:
    """노드 내용 비교용 복합 키."""
    return "|".join(
        [
            normalize_content(node.content),
            normalize_content(node.title),
            node.node_type,
            normalize_content(node.metadata),
        ]
    )


# ---------------------------------------------------------------------------
# TextDiffer — 단어 단위 LCS 기반 텍스트 인라인 diff
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> list[str]:
    """텍스트를 단어/토큰 단위로 분리.

    공백 및 구두점을 별도 토큰으로 유지하여 재구성 시 원문 보존.
    한국어 어절(공백 기준)과 영어 단어 모두 처리한다.
    """
    # 단어(한국어 포함), 공백, 기타 문자 단위로 분리
    return re.findall(r'\S+|\s+', text) if text else []


def _lcs_diff(tokens_a: list[str], tokens_b: list[str]) -> list[InlineDiffToken]:
    """LCS 기반 diff → InlineDiffToken 목록 반환.

    시간 복잡도: O(n*m) — 큰 텍스트는 max_inline_length로 제한 권장.

    Raises:
        ValueError: DP 셀 수가 MAX_LCS_CELLS 초과 (호출부에서 skipped=True 처리)
    """
    n, m = len(tokens_a), len(tokens_b)

    # O(n*m) 메모리/CPU 폭증 방지 — 셀 수 상한 초과 시 즉시 거부
    if n * m > MAX_LCS_CELLS:
        raise ValueError(
            f"LCS DP 셀 수({n * m:,})가 최대({MAX_LCS_CELLS:,})를 초과합니다. "
            "인라인 diff를 건너뜁니다."
        )

    # LCS DP 테이블 (전체 보관 — 역추적에 필요)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if tokens_a[i - 1] == tokens_b[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])

    # 역추적으로 diff 생성
    result: list[InlineDiffToken] = []
    i, j = n, m

    stack: list[InlineDiffToken] = []
    while i > 0 or j > 0:
        if i > 0 and j > 0 and tokens_a[i - 1] == tokens_b[j - 1]:
            stack.append(InlineDiffToken(type="unchanged", text=tokens_a[i - 1]))
            i -= 1
            j -= 1
        elif j > 0 and (i == 0 or dp[i][j - 1] >= dp[i - 1][j]):
            stack.append(InlineDiffToken(type="added", text=tokens_b[j - 1]))
            j -= 1
        else:
            stack.append(InlineDiffToken(type="deleted", text=tokens_a[i - 1]))
            i -= 1

    result = list(reversed(stack))

    # 연속된 동일 타입 토큰 병합 (렌더링 효율)
    merged: list[InlineDiffToken] = []
    for token in result:
        if merged and merged[-1].type == token.type:
            merged[-1] = InlineDiffToken(type=token.type, text=merged[-1].text + token.text)
        else:
            merged.append(token)

    return merged


class TextDiffer:
    """단어 단위 텍스트 인라인 diff 서비스."""

    def diff(
        self,
        text_a: Optional[str],
        text_b: Optional[str],
        max_length: int = DEFAULT_MAX_INLINE_LENGTH,
    ) -> tuple[list[InlineDiffToken], bool]:
        """두 텍스트 간 단어 단위 diff 계산.

        Returns:
            (tokens, skipped)
            skipped=True 이면 max_length 초과로 계산을 건너뜀.
        """
        a = text_a or ""
        b = text_b or ""

        if len(a) + len(b) > max_length:
            return [], True

        if a == b:
            return [InlineDiffToken(type="unchanged", text=a)] if a else [], False

        tokens_a = _tokenize(a)
        tokens_b = _tokenize(b)

        try:
            return _lcs_diff(tokens_a, tokens_b), False
        except Exception:
            logger.exception("텍스트 diff 계산 실패 — 건너뜀")
            return [], True


text_differ = TextDiffer()


# ---------------------------------------------------------------------------
# NodeDiffer — Node 트리 구조 diff
# ---------------------------------------------------------------------------


class DiffTooLargeError(Exception):
    """노드 수 초과로 동기 diff 불가."""
    pass


class NodeDiffer:
    """Node ID 기반 트리 diff 서비스."""

    def diff(
        self,
        nodes_a: list[Node],
        nodes_b: list[Node],
        inline_diff: bool = False,
        include_unchanged: bool = False,
        max_inline_length: int = DEFAULT_MAX_INLINE_LENGTH,
    ) -> tuple[list[NodeDiff], bool]:
        """두 노드 목록을 비교하여 NodeDiff 목록을 반환.

        Args:
            nodes_a: 이전 버전 노드 목록
            nodes_b: 이후 버전 노드 목록
            inline_diff: True이면 MODIFIED 노드에 인라인 diff 포함
            include_unchanged: True이면 UNCHANGED 노드도 포함
            max_inline_length: 인라인 diff 최대 텍스트 길이

        Returns:
            (diffs, has_data_issue)

        Raises:
            DiffTooLargeError: 노드 수가 MAX_NODES_SYNC 초과
        """
        total = len(nodes_a) + len(nodes_b)
        if total > MAX_NODES_SYNC:
            raise DiffTooLargeError(
                f"노드 수({total})가 최대({MAX_NODES_SYNC})를 초과합니다."
            )

        has_data_issue = False

        # ID → Node 맵 생성 (중복 ID 감지)
        map_a: dict[str, Node] = {}
        for node in nodes_a:
            if node.id in map_a:
                logger.warning("중복 node_id 감지: %s (version_a)", node.id)
                has_data_issue = True
            else:
                map_a[node.id] = node

        map_b: dict[str, Node] = {}
        for node in nodes_b:
            if node.id in map_b:
                logger.warning("중복 node_id 감지: %s (version_b)", node.id)
                has_data_issue = True
            else:
                map_b[node.id] = node

        all_ids = set(map_a.keys()) | set(map_b.keys())
        diffs: list[NodeDiff] = []

        for node_id in all_ids:
            node_a = map_a.get(node_id)
            node_b = map_b.get(node_id)

            diff = self._classify(
                node_id,
                node_a,
                node_b,
                inline_diff=inline_diff,
                max_inline_length=max_inline_length,
            )

            if not include_unchanged and diff.change_type == ChangeType.UNCHANGED:
                continue
            diffs.append(diff)

        return diffs, has_data_issue

    def _classify(
        self,
        node_id: str,
        node_a: Optional[Node],
        node_b: Optional[Node],
        inline_diff: bool = False,
        max_inline_length: int = DEFAULT_MAX_INLINE_LENGTH,
    ) -> NodeDiff:
        """단일 노드 변경 유형 분류."""
        if node_a is None and node_b is not None:
            return NodeDiff(
                node_id=node_id,
                change_type=ChangeType.ADDED,
                before=None,
                after=_node_to_snapshot(node_b),
            )

        if node_a is not None and node_b is None:
            return NodeDiff(
                node_id=node_id,
                change_type=ChangeType.DELETED,
                before=_node_to_snapshot(node_a),
                after=None,
            )

        # 양쪽 모두 존재
        assert node_a is not None and node_b is not None

        content_changed = _node_content_key(node_a) != _node_content_key(node_b)
        position_changed = (
            node_a.parent_id != node_b.parent_id
            or node_a.order_index != node_b.order_index
        )

        before_snap = _node_to_snapshot(node_a)
        after_snap = _node_to_snapshot(node_b)

        if not content_changed and not position_changed:
            return NodeDiff(
                node_id=node_id,
                change_type=ChangeType.UNCHANGED,
                before=before_snap,
                after=after_snap,
            )

        move_info: Optional[MoveInfo] = None
        if position_changed:
            move_type = (
                MoveType.HIERARCHY_CHANGE
                if node_a.parent_id != node_b.parent_id
                else MoveType.REORDER
            )
            move_info = MoveInfo(
                old_parent_id=node_a.parent_id,
                new_parent_id=node_b.parent_id,
                old_order=node_a.order_index,
                new_order=node_b.order_index,
                move_type=move_type,
            )

        if content_changed:
            # MODIFIED (위치 변경도 move_info에 포함)
            inline_tokens: Optional[list[InlineDiffToken]] = None
            skipped = False

            if inline_diff:
                # content 텍스트 기준 인라인 diff
                text_a = node_a.content or node_a.title or ""
                text_b = node_b.content or node_b.title or ""
                inline_tokens, skipped = text_differ.diff(
                    text_a, text_b, max_length=max_inline_length
                )
                if skipped:
                    inline_tokens = None

            return NodeDiff(
                node_id=node_id,
                change_type=ChangeType.MODIFIED,
                before=before_snap,
                after=after_snap,
                inline_diff=inline_tokens,
                inline_diff_skipped=skipped,
                move_info=move_info,
            )

        # 위치만 변경 → MOVED
        return NodeDiff(
            node_id=node_id,
            change_type=ChangeType.MOVED,
            before=before_snap,
            after=after_snap,
            move_info=move_info,
        )


node_differ = NodeDiffer()


# ---------------------------------------------------------------------------
# DiffSummaryGenerator
# ---------------------------------------------------------------------------


_NODE_TYPE_LABELS: dict[str, str] = {
    "section": "섹션",
    "paragraph": "단락",
    "list": "목록",
    "table": "표",
    "heading": "헤딩",
    "image": "이미지",
    "code": "코드 블록",
}


def _node_type_label(node_type: str) -> str:
    return _NODE_TYPE_LABELS.get(node_type, "항목")


class DiffSummaryGenerator:
    """DiffResult → 사람이 읽기 쉬운 변경 요약 생성."""

    def generate(
        self,
        node_diffs: list[NodeDiff],
        nodes_b: list[Node],
        nodes_a: Optional[list[Node]] = None,
    ) -> DiffSummary:
        """변경 요약 생성.

        Args:
            node_diffs: NodeDiffer.diff() 결과 (UNCHANGED 포함 여부 무관)
            nodes_b: 이후 버전 노드 목록 (부모 탐색에 사용)
            nodes_a: 이전 버전 노드 목록 (DELETED 노드의 부모 탐색 fallback).
                     None 이면 DELETED 노드의 최상위 섹션을 찾을 수 없을 수 있음.
        """
        counts: dict[ChangeType, int] = {ct: 0 for ct in ChangeType}
        for nd in node_diffs:
            counts[nd.change_type] += 1

        # unchanged는 별도 파라미터로 받지 않으므로 0으로 유지
        total_added = counts[ChangeType.ADDED]
        total_deleted = counts[ChangeType.DELETED]
        total_modified = counts[ChangeType.MODIFIED]
        total_moved = counts[ChangeType.MOVED]
        total_unchanged = counts[ChangeType.UNCHANGED]

        # 텍스트 변경 문자수 추정
        changed_chars = 0
        for nd in node_diffs:
            if nd.change_type == ChangeType.ADDED and nd.after and nd.after.content:
                changed_chars += len(nd.after.content)
            elif nd.change_type == ChangeType.DELETED and nd.before and nd.before.content:
                changed_chars += len(nd.before.content)
            elif nd.change_type == ChangeType.MODIFIED:
                if nd.inline_diff:
                    changed_chars += sum(
                        len(t.text) for t in nd.inline_diff if t.type != "unchanged"
                    )
                else:
                    before_len = len(nd.before.content or "") if nd.before else 0
                    after_len = len(nd.after.content or "") if nd.after else 0
                    changed_chars += abs(after_len - before_len)

        # 자연어 요약 생성
        description = self._build_description(
            total_added, total_deleted, total_modified, total_moved
        )

        # 심각도 계산
        total_changed = total_added + total_deleted + total_modified + total_moved
        total_all = total_changed + total_unchanged
        severity = self._calc_severity(total_changed, total_all)

        # 변경된 최상위 섹션 식별 (DELETED 노드 부모 탐색을 위해 nodes_a 맵도 포함)
        node_b_map = {n.id: n for n in nodes_b}
        changed_sections = self._identify_changed_sections(
            node_diffs, node_b_map, nodes_a
        )

        return DiffSummary(
            total_added=total_added,
            total_deleted=total_deleted,
            total_modified=total_modified,
            total_moved=total_moved,
            total_unchanged=total_unchanged,
            changed_characters=changed_chars,
            description=description,
            severity=severity if total_changed > 0 else None,
            changed_sections=changed_sections,
        )

    def _build_description(
        self,
        added: int,
        deleted: int,
        modified: int,
        moved: int,
    ) -> str:
        if added == 0 and deleted == 0 and modified == 0 and moved == 0:
            return "변경 사항 없음"

        parts: list[str] = []
        if added:
            parts.append(f"{added}개 추가")
        if deleted:
            parts.append(f"{deleted}개 삭제")
        if modified:
            parts.append(f"{modified}개 수정")
        if moved:
            parts.append(f"{moved}개 이동")

        return "항목 " + ", ".join(parts) + "됨"

    def _calc_severity(self, changed: int, total: int) -> Optional[DiffSeverity]:
        if total == 0 or changed == 0:
            return None
        ratio = changed / total
        if ratio >= 0.3:
            return DiffSeverity.MAJOR
        if ratio >= 0.1:
            return DiffSeverity.MINOR
        return DiffSeverity.TRIVIAL

    def _identify_changed_sections(
        self,
        node_diffs: list[NodeDiff],
        node_map: dict[str, Node],
        nodes_a: Optional[list[Node]] = None,
    ) -> list[ChangedSection]:
        """변경이 발생한 최상위 섹션 목록 추출 (최대 MAX_CHANGED_SECTIONS개)."""
        # 변경된 노드 목록 (UNCHANGED 제외)
        changed_ids = {
            nd.node_id
            for nd in node_diffs
            if nd.change_type != ChangeType.UNCHANGED
        }
        if not changed_ids:
            return []

        # DELETED 노드 부모 탐색을 위해 nodes_a 맵을 fallback으로 병합
        node_a_map: dict[str, Node] = {n.id: n for n in nodes_a} if nodes_a else {}
        # nodes_b가 우선, DELETED된 노드는 node_a_map에서 탐색
        combined_map = {**node_a_map, **node_map}

        section_map: dict[str, list[str]] = {}  # top_section_id → [changed_node_ids]

        for node_id in changed_ids:
            top = self._find_top_section(node_id, combined_map)
            if top:
                section_map.setdefault(top, []).append(node_id)
            else:
                # 최상위 섹션이 없으면 자신이 섹션
                section_map.setdefault(node_id, []).append(node_id)

        # 변경 유형 결정 (해당 섹션 자체의 변경 유형 우선)
        diff_type_map = {nd.node_id: nd.change_type for nd in node_diffs}

        sections: list[ChangedSection] = []
        for section_id, sub_ids in list(section_map.items())[:MAX_CHANGED_SECTIONS]:
            node = combined_map.get(section_id)
            change_type = diff_type_map.get(section_id, ChangeType.MODIFIED)
            sections.append(
                ChangedSection(
                    node_id=section_id,
                    title=node.title if node else None,
                    change_type=change_type,
                    sub_changes=len(sub_ids),
                )
            )

        return sections

    def _find_top_section(
        self,
        node_id: str,
        node_map: dict[str, Node],
        depth: int = 0,
    ) -> Optional[str]:
        """노드의 최상위 section 조상 ID를 반환. 없으면 None."""
        if depth > MAX_PARENT_DEPTH:
            return None
        node = node_map.get(node_id)
        if node is None:
            return None
        if node.parent_id is None:
            # 최상위 노드
            return node.id if node.node_type == "section" else None
        # 부모가 있으면 위로 탐색
        parent = node_map.get(node.parent_id)
        if parent is None:
            return node.id if node.node_type == "section" else None
        top = self._find_top_section(node.parent_id, node_map, depth + 1)
        return top if top else (node.id if node.node_type == "section" else None)


diff_summary_generator = DiffSummaryGenerator()


# ---------------------------------------------------------------------------
# 인메모리 캐시 (Phase 9 MVP)
# ---------------------------------------------------------------------------

_diff_cache: dict[str, DiffResult] = {}
_summary_cache: dict[str, DiffSummaryResponse] = {}


def _cache_key(document_id: str, v1_id: str, v2_id: str) -> str:
    """버전 ID를 정규화하여 캐시 키 생성 (방향 무관).

    document_id 포함으로 invalidate_cache_for_document 검색 가능.
    """
    a, b = sorted([v1_id, v2_id])
    return f"diff:{document_id}:{a}:{b}"


def _get_cached_diff(document_id: str, v1_id: str, v2_id: str) -> Optional[DiffResult]:
    return _diff_cache.get(_cache_key(document_id, v1_id, v2_id))


def _set_cached_diff(document_id: str, v1_id: str, v2_id: str, result: DiffResult) -> None:
    _diff_cache[_cache_key(document_id, v1_id, v2_id)] = result


def _get_cached_summary(document_id: str, v1_id: str, v2_id: str) -> Optional[DiffSummaryResponse]:
    return _summary_cache.get(_cache_key(document_id, v1_id, v2_id))


def _set_cached_summary(document_id: str, v1_id: str, v2_id: str, result: DiffSummaryResponse) -> None:
    _summary_cache[_cache_key(document_id, v1_id, v2_id)] = result


def invalidate_cache_for_document(document_id: str) -> None:
    """문서 삭제 시 관련 캐시 무효화.

    캐시 키 형식: `diff:{doc_id}:{min_vid}:{max_vid}` 를 사용하므로
    document_id 접두사로 검색하여 삭제한다.
    """
    prefix = f"diff:{document_id}:"
    keys_to_del = [k for k in _diff_cache if k.startswith(prefix)]
    for k in keys_to_del:
        _diff_cache.pop(k, None)
        _summary_cache.pop(k, None)


# ---------------------------------------------------------------------------
# DiffService — 외부 진입점
# ---------------------------------------------------------------------------


def _build_version_ref(version: Any) -> VersionRef:
    return VersionRef(
        id=version.id,
        version_number=version.version_number,
        status=version.status,
        created_at=version.created_at.isoformat(),
        created_by=version.created_by,
        label=version.label,
        change_summary=version.change_summary,
    )


class DiffService:
    """두 버전 간 diff를 계산하고 캐싱하는 서비스."""

    def compute_diff(
        self,
        conn: psycopg2.extensions.connection,
        *,
        document_id: str,
        version_a_id: str,
        version_b_id: str,
        inline_diff: bool = False,
        include_unchanged: bool = False,
        max_inline_length: int = DEFAULT_MAX_INLINE_LENGTH,
    ) -> DiffResult:
        """두 버전 ID로 DiffResult를 계산한다.

        캐시가 있으면 캐시를 반환. inline_diff 옵션은 캐싱하지 않음
        (inline_diff=False 기본값만 캐싱).
        """
        if version_a_id == version_b_id:
            raise ApiValidationError(
                "동일한 버전끼리는 비교할 수 없습니다.",
                details=[{"field": "version_id", "reason": "SAME_VERSION"}],
            )

        # 캐시 확인 (inline_diff=False 일 때만)
        if not inline_diff and not include_unchanged:
            cached = _get_cached_diff(document_id, version_a_id, version_b_id)
            if cached:
                return cached

        # 버전 조회
        version_a = versions_repository.get_by_document_and_version_id(
            conn, document_id, version_a_id
        )
        if version_a is None:
            raise ApiNotFoundError(f"버전을 찾을 수 없습니다: {version_a_id}")

        version_b = versions_repository.get_by_document_and_version_id(
            conn, document_id, version_b_id
        )
        if version_b is None:
            raise ApiNotFoundError(f"버전을 찾을 수 없습니다: {version_b_id}")

        # 노드 조회
        nodes_a = nodes_repository.list_by_version_id(conn, version_a_id)
        nodes_b = nodes_repository.list_by_version_id(conn, version_b_id)

        # diff 계산
        try:
            diffs, has_data_issue = node_differ.diff(
                nodes_a,
                nodes_b,
                inline_diff=inline_diff,
                include_unchanged=include_unchanged,
                max_inline_length=max_inline_length,
            )
        except DiffTooLargeError as exc:
            raise ApiValidationError(
                str(exc),
                details=[{"field": "nodes", "reason": "DIFF_TOO_LARGE"}],
            )

        # 요약 생성 — DELETED 노드 부모 탐색을 위해 nodes_a 도 전달
        summary = diff_summary_generator.generate(diffs, nodes_b, nodes_a=nodes_a)

        result = DiffResult(
            document_id=document_id,
            version_a=_build_version_ref(version_a),
            version_b=_build_version_ref(version_b),
            summary=summary,
            nodes=diffs,
            has_data_issue=has_data_issue,
        )

        # 기본 옵션일 때만 캐싱
        if not inline_diff and not include_unchanged:
            _set_cached_diff(document_id, version_a_id, version_b_id, result)

        return result

    def compute_diff_with_previous(
        self,
        conn: psycopg2.extensions.connection,
        *,
        document_id: str,
        version_id: str,
        inline_diff: bool = False,
        include_unchanged: bool = False,
        max_inline_length: int = DEFAULT_MAX_INLINE_LENGTH,
    ) -> DiffResult:
        """직전 버전 대비 diff를 계산한다.

        직전 버전 결정 우선순위:
          1. parent_version_id (lineage)
          2. version_number - 1 기준 조회
        """
        version_b = versions_repository.get_by_document_and_version_id(
            conn, document_id, version_id
        )
        if version_b is None:
            raise ApiNotFoundError(f"버전을 찾을 수 없습니다: {version_id}")

        # 직전 버전 결정
        version_a = None
        if version_b.parent_version_id:
            version_a = versions_repository.get_by_id(conn, version_b.parent_version_id)

        if version_a is None and version_b.version_number > 1:
            # parent_version_id가 없으면 version_number - 1 기준 탐색
            # 주의: 파라미터는 반드시 %s 바인딩 사용 (f-string에 변수 미사용)
            sql = """
                SELECT id, document_id, version_number, label, status, change_summary,
                       source, metadata, created_by, created_at,
                       parent_version_id, restored_from_version_id,
                       title_snapshot, summary_snapshot, metadata_snapshot, content_snapshot,
                       published_by, published_at
                FROM versions
                WHERE document_id = %s AND version_number = %s
                LIMIT 1
            """
            with conn.cursor() as cur:
                cur.execute(sql, (document_id, version_b.version_number - 1))
                row = cur.fetchone()
            if row:
                from app.repositories.versions_repository import _row_to_version
                version_a = _row_to_version(dict(row))

        if version_a is None:
            raise ApiNotFoundError(
                "직전 버전이 없습니다. 최초 버전은 이전 버전과 비교할 수 없습니다.",
                details=[{"reason": "NO_PREVIOUS_VERSION"}],
            )

        return self.compute_diff(
            conn,
            document_id=document_id,
            version_a_id=version_a.id,
            version_b_id=version_b.id,
            inline_diff=inline_diff,
            include_unchanged=include_unchanged,
            max_inline_length=max_inline_length,
        )

    def compute_summary_only(
        self,
        conn: psycopg2.extensions.connection,
        *,
        document_id: str,
        version_a_id: str,
        version_b_id: str,
    ) -> DiffSummaryResponse:
        """변경 요약만 반환 (전체 diff 제외, 캐싱 활용)."""
        cached = _get_cached_summary(document_id, version_a_id, version_b_id)
        if cached:
            return cached

        full = self.compute_diff(
            conn,
            document_id=document_id,
            version_a_id=version_a_id,
            version_b_id=version_b_id,
        )
        result = DiffSummaryResponse(
            document_id=full.document_id,
            version_a=full.version_a,
            version_b=full.version_b,
            summary=full.summary,
        )
        _set_cached_summary(document_id, version_a_id, version_b_id, result)
        return result

    def compute_summary_with_previous(
        self,
        conn: psycopg2.extensions.connection,
        *,
        document_id: str,
        version_id: str,
    ) -> DiffSummaryResponse:
        """직전 버전 대비 변경 요약 반환."""
        # full diff 계산 (내부에서 diff 캐시 활용)
        full = self.compute_diff_with_previous(
            conn, document_id=document_id, version_id=version_id
        )
        # summary 캐시 확인 (full diff 결과에서 version ID를 알 수 있으므로 계산 후 확인)
        cached = _get_cached_summary(document_id, full.version_a.id, full.version_b.id)
        if cached:
            return cached
        result = DiffSummaryResponse(
            document_id=full.document_id,
            version_a=full.version_a,
            version_b=full.version_b,
            summary=full.summary,
        )
        _set_cached_summary(document_id, full.version_a.id, full.version_b.id, result)
        return result


diff_service = DiffService()
