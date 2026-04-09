"""
청킹 서비스 — DocumentType별 최적 청킹 전략.

설계 원칙:
  - DocumentType 설정에서 chunking_config를 읽어 전략 결정 (하드코딩 금지)
  - node_based 전략: Node 트리의 자연스러운 경계를 청크 단위로 활용
  - 청크 크기 조정: min/max 토큰 기준 merge/split 로직
  - 부모 컨텍스트 주입: include_parent_context 설정 시 부모 제목 앞에 포함
  - 청크 메타데이터: document_id, version_id, node_id, node_path, chunk_index 포함
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 청킹 설정 모델
# ---------------------------------------------------------------------------

@dataclass
class ChunkingConfig:
    """DocumentType별 청킹 설정."""
    strategy: str = "node_based"           # node_based | fixed_size | semantic
    max_chunk_tokens: int = 512            # 청크 최대 토큰 수
    min_chunk_tokens: int = 50             # 청크 최소 토큰 수 (이하면 부모와 합치기)
    overlap_tokens: int = 50               # 청크 간 오버랩 토큰 수
    include_parent_context: bool = True    # 부모 노드 제목을 청크 앞에 포함
    index_version_policy: str = "published_only"  # published_only | latest | all


# ---------------------------------------------------------------------------
# 청크 데이터 모델
# ---------------------------------------------------------------------------

@dataclass
class DocumentChunk:
    """청크 단위 데이터 모델."""
    document_id: str
    version_id: str
    node_id: Optional[str]
    chunk_index: int
    source_text: str
    node_path: list[str]              # 문서 내 위치 경로 (제목 목록)
    document_type: str
    document_status: str
    token_count: int = 0
    # 권한 메타데이터 (Phase 10-8에서 채움)
    accessible_roles: list[str] = field(default_factory=list)
    accessible_user_ids: list[str] = field(default_factory=list)
    accessible_org_ids: list[str] = field(default_factory=list)
    is_public: bool = False


# ---------------------------------------------------------------------------
# 토큰 카운팅 유틸
# ---------------------------------------------------------------------------

def _count_tokens(text: str) -> int:
    """텍스트의 토큰 수를 추정한다.

    tiktoken이 설치된 경우 정확한 토큰 수를 반환하고,
    그렇지 않은 경우 whitespace 기반 근사치를 반환한다.
    """
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except (ImportError, Exception):
        # 근사치: 4자 ≈ 1토큰 (영어 기준)
        return max(1, len(text) // 4)


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    """텍스트를 최대 토큰 수로 잘라낸다."""
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        tokens = enc.encode(text)
        if len(tokens) <= max_tokens:
            return text
        return enc.decode(tokens[:max_tokens])
    except (ImportError, Exception):
        # 근사치로 자르기
        max_chars = max_tokens * 4
        return text[:max_chars] if len(text) > max_chars else text


# ---------------------------------------------------------------------------
# 노드 트리 유틸
# ---------------------------------------------------------------------------

@dataclass
class NodeInfo:
    """청킹용 노드 정보 (DB row에서 변환)."""
    id: str
    parent_id: Optional[str]
    node_type: str
    order_index: int
    title: Optional[str]
    content: Optional[str]
    children: list["NodeInfo"] = field(default_factory=list)


def _build_node_tree(node_rows: list[dict]) -> list[NodeInfo]:
    """DB row 목록에서 노드 트리를 구성한다."""
    nodes = {
        str(row["id"]): NodeInfo(
            id=str(row["id"]),
            parent_id=str(row["parent_id"]) if row.get("parent_id") else None,
            node_type=row.get("node_type", "paragraph"),
            order_index=row.get("order_index", 0),
            title=row.get("title"),
            content=row.get("content"),
        )
        for row in node_rows
    }

    roots = []
    for node in nodes.values():
        if node.parent_id and node.parent_id in nodes:
            nodes[node.parent_id].children.append(node)
        else:
            roots.append(node)

    # 자식 정렬
    def _sort_children(n: NodeInfo) -> None:
        n.children.sort(key=lambda c: c.order_index)
        for c in n.children:
            _sort_children(c)

    roots.sort(key=lambda n: n.order_index)
    for root in roots:
        _sort_children(root)

    return roots


# ---------------------------------------------------------------------------
# ChunkingService
# ---------------------------------------------------------------------------

class ChunkingService:
    """DocumentType 설정에 따라 문서를 청크로 분해한다."""

    _DEFAULT_CONFIG = ChunkingConfig()

    def chunk_version(
        self,
        *,
        document_id: str,
        version_id: str,
        document_type: str,
        document_status: str,
        node_rows: list[dict],
        chunking_config: Optional[dict] = None,
    ) -> list[DocumentChunk]:
        """버전의 노드들을 청크 목록으로 변환한다.

        Args:
            document_id: 문서 UUID
            version_id: 버전 UUID
            document_type: 문서 타입 코드 (POLICY, MANUAL, ...)
            document_status: 문서 상태 (published, draft, ...)
            node_rows: DB에서 조회한 노드 row 목록
            chunking_config: document_types.plugin_config['chunking_config'] 값
        """
        config = self._resolve_config(chunking_config)

        if config.strategy == "node_based":
            return self._chunk_node_based(
                document_id=document_id,
                version_id=version_id,
                document_type=document_type,
                document_status=document_status,
                node_rows=node_rows,
                config=config,
            )
        # fixed_size / semantic — 향후 확장. 현재는 node_based 폴백
        logger.warning("전략 '%s'은 지원되지 않습니다. node_based로 대체합니다.", config.strategy)
        return self._chunk_node_based(
            document_id=document_id,
            version_id=version_id,
            document_type=document_type,
            document_status=document_status,
            node_rows=node_rows,
            config=config,
        )

    # ---------------------------------------------------------------------------
    # node_based 청킹
    # ---------------------------------------------------------------------------

    def _chunk_node_based(
        self,
        *,
        document_id: str,
        version_id: str,
        document_type: str,
        document_status: str,
        node_rows: list[dict],
        config: ChunkingConfig,
    ) -> list[DocumentChunk]:
        """Node 트리 기반 청킹."""
        if not node_rows:
            return []

        roots = _build_node_tree(node_rows)
        raw_chunks: list[tuple[str, str, Optional[str], list[str]]] = []
        # (text, node_id, parent_title_path)

        def _visit(node: NodeInfo, parent_path: list[str]) -> None:
            # 현재 노드 텍스트 구성
            parts = []
            if config.include_parent_context and parent_path:
                parts.append(" > ".join(parent_path))
            if node.title:
                parts.append(node.title)
            if node.content:
                parts.append(node.content)
            text = "\n".join(p for p in parts if p).strip()

            current_path = parent_path + ([node.title] if node.title else [])

            # 내용이 있는 노드는 모두 청크 후보로 추가 (크기는 merge/split에서 조정)
            if text:
                raw_chunks.append((text, node.id, node.node_type, current_path))

            for child in node.children:
                _visit(child, current_path)

        for root in roots:
            _visit(root, [])

        # 크기 조정: max_chunk_tokens 초과 시 분할
        adjusted: list[tuple[str, str, Optional[str], list[str]]] = []
        for text, node_id, node_type, path in raw_chunks:
            token_count = _count_tokens(text)
            if token_count > config.max_chunk_tokens:
                # 분할 처리
                sub_chunks = self._split_by_tokens(text, config.max_chunk_tokens, config.overlap_tokens)
                for sub_text in sub_chunks:
                    adjusted.append((sub_text, node_id, node_type, path))
            else:
                adjusted.append((text, node_id, node_type, path))

        # 너무 작은 청크 병합
        merged = self._merge_small_chunks(adjusted, config.min_chunk_tokens, config.max_chunk_tokens)

        # DocumentChunk 생성
        chunks = []
        for idx, (text, node_id, node_type, path) in enumerate(merged):
            chunks.append(DocumentChunk(
                document_id=document_id,
                version_id=version_id,
                node_id=node_id,
                chunk_index=idx,
                source_text=text,
                node_path=path,
                document_type=document_type,
                document_status=document_status,
                token_count=_count_tokens(text),
            ))

        return chunks

    def _split_by_tokens(
        self, text: str, max_tokens: int, overlap_tokens: int
    ) -> list[str]:
        """텍스트를 max_tokens 단위로 분할하고 overlap_tokens만큼 오버랩한다."""
        try:
            import tiktoken
            enc = tiktoken.get_encoding("cl100k_base")
            tokens = enc.encode(text)
        except (ImportError, Exception):
            # 근사치
            chars_per_token = 4
            tokens = list(text)  # char list
            max_tokens_chars = max_tokens * chars_per_token
            overlap_chars = overlap_tokens * chars_per_token

            sub_chunks = []
            start = 0
            while start < len(text):
                end = min(start + max_tokens_chars, len(text))
                sub_chunks.append(text[start:end])
                if end >= len(text):
                    break
                start = end - overlap_chars
            return sub_chunks

        sub_chunks = []
        start = 0
        while start < len(tokens):
            end = min(start + max_tokens, len(tokens))
            chunk_tokens = tokens[start:end]
            try:
                sub_text = enc.decode(chunk_tokens)
            except Exception:
                sub_text = text[start * 4: end * 4]
            sub_chunks.append(sub_text)
            if end >= len(tokens):
                break
            start = end - overlap_tokens
        return sub_chunks

    def _merge_small_chunks(
        self,
        chunks: list[tuple[str, str, Optional[str], list[str]]],
        min_tokens: int,
        max_tokens: int,
    ) -> list[tuple[str, str, Optional[str], list[str]]]:
        """min_tokens 미만의 작은 청크를 인접 청크와 병합한다."""
        if not chunks:
            return []

        result = []
        buffer_text = ""
        buffer_node_id = None
        buffer_node_type = None
        buffer_path: list[str] = []

        for text, node_id, node_type, path in chunks:
            token_count = _count_tokens(text)

            if buffer_text:
                combined = buffer_text + "\n" + text
                combined_tokens = _count_tokens(combined)
                if combined_tokens <= max_tokens:
                    buffer_text = combined
                    # 첫 청크의 node_id/path 유지
                    continue
                else:
                    result.append((buffer_text, buffer_node_id, buffer_node_type, buffer_path))
                    buffer_text = text
                    buffer_node_id = node_id
                    buffer_node_type = node_type
                    buffer_path = path
            else:
                if token_count < min_tokens:
                    buffer_text = text
                    buffer_node_id = node_id
                    buffer_node_type = node_type
                    buffer_path = path
                else:
                    result.append((text, node_id, node_type, path))

        if buffer_text:
            result.append((buffer_text, buffer_node_id, buffer_node_type, buffer_path))

        return result

    # ---------------------------------------------------------------------------
    # 설정 해석
    # ---------------------------------------------------------------------------

    def _resolve_config(self, raw_config: Optional[dict]) -> ChunkingConfig:
        """document_types.plugin_config에서 chunking_config를 읽어 ChunkingConfig로 변환."""
        if not raw_config:
            return self._DEFAULT_CONFIG

        return ChunkingConfig(
            strategy=raw_config.get("strategy", self._DEFAULT_CONFIG.strategy),
            max_chunk_tokens=int(raw_config.get("max_chunk_tokens", self._DEFAULT_CONFIG.max_chunk_tokens)),
            min_chunk_tokens=int(raw_config.get("min_chunk_tokens", self._DEFAULT_CONFIG.min_chunk_tokens)),
            overlap_tokens=int(raw_config.get("overlap_tokens", self._DEFAULT_CONFIG.overlap_tokens)),
            include_parent_context=bool(raw_config.get("include_parent_context", self._DEFAULT_CONFIG.include_parent_context)),
            index_version_policy=raw_config.get("index_version_policy", self._DEFAULT_CONFIG.index_version_policy),
        )

    def get_chunking_config_for_type(
        self,
        conn,
        document_type: str,
    ) -> ChunkingConfig:
        """DB에서 DocumentType의 chunking_config를 조회한다."""
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT plugin_config FROM document_types WHERE type_code = %s",
                    (document_type,),
                )
                row = cur.fetchone()
                if not row:
                    logger.warning(
                        "document_type='%s'가 document_types 테이블에 없어 기본 청킹 설정을 사용합니다.",
                        document_type,
                    )
                    return self._DEFAULT_CONFIG
                plugin_config = row.get("plugin_config") or {}
                raw_config = plugin_config.get("chunking_config")
                if raw_config:
                    return self._resolve_config(raw_config)
                logger.warning(
                    "document_type='%s' plugin_config에 chunking_config가 없어 기본 청킹 설정을 사용합니다.",
                    document_type,
                )
        except Exception as exc:
            logger.warning("DocumentType '%s' chunking_config 조회 실패: %s", document_type, exc)
        return self._DEFAULT_CONFIG


chunking_service = ChunkingService()
