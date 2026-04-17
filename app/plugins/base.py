"""
DocumentType 플러그인 기반 인터페이스 및 레지스트리.

설계 원칙 (CLAUDE.md):
  - 문서 타입은 하드코딩 금지 — 구조는 generic + config 기반
  - DocumentType별 분기 로직은 서비스 레이어에서만 → 플러그인 경유로 대체
  - 모든 로직은 "type-aware" 해야 함

레지스트리 조회 우선순위:
  1. 코드 등록 플러그인 (내장 타입 — 시스템 부팅 시 자동 등록)
  2. DB 기반 설정 (Admin UI에서 오버라이드)
  3. DefaultDocumentTypePlugin (폴백)
"""

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ChunkingConfig 데이터 클래스 (Phase 10 호환)
# ---------------------------------------------------------------------------

@dataclass
class ChunkingConfig:
    """DocumentType별 청킹 설정."""
    strategy: str = "node_based"
    max_chunk_tokens: int = 512
    min_chunk_tokens: int = 50
    overlap_tokens: int = 50
    include_parent_context: bool = True
    parent_context_depth: int = 2
    index_version_policy: str = "published_only"
    exclude_node_types: list[str] = field(default_factory=list)
    merge_strategy: str = "merge_siblings"


# ---------------------------------------------------------------------------
# 서브플러그인 인터페이스 (기본 구현 포함)
# ---------------------------------------------------------------------------

class MetadataSchemaPlugin:
    """타입별 metadata JSON Schema 관리."""

    def get_schema(self) -> dict:
        """JSON Schema (draft-07) 반환. 빈 dict = 모든 metadata 허용."""
        return {}

    def validate(self, metadata: dict) -> list[str]:
        """검증 오류 메시지 목록 반환 (빈 리스트 = 유효).

        jsonschema 패키지가 설치되지 않은 경우 검증을 건너뛴다 (모든 metadata 허용).
        """
        schema = self.get_schema()
        if not schema:
            return []
        try:
            import jsonschema
        except ImportError:
            # jsonschema 미설치 시 검증 건너뜀
            logger.debug("jsonschema 패키지가 설치되지 않아 metadata 검증을 건너뜁니다.")
            return []
        try:
            jsonschema.validate(instance=metadata, schema=schema)
            return []
        except jsonschema.ValidationError as exc:
            return [exc.message]
        except jsonschema.SchemaError as exc:
            return [f"스키마 오류: {exc.message}"]
        except Exception as exc:
            return [str(exc)]

    def get_ui_schema(self) -> dict:
        """UI 렌더링 힌트 (필드 순서, 라벨, 플레이스홀더 등)."""
        return {}


class EditorPlugin:
    """타입별 편집기 동작 규칙."""

    def get_allowed_node_types(self) -> list[str]:
        """편집기에서 추가 가능한 노드 타입. 빈 리스트 = 모두 허용."""
        return []

    def get_default_structure(self) -> list[dict]:
        """신규 문서 생성 시 기본 노드 구조 템플릿."""
        return []

    def get_editor_config(self) -> dict:
        """편집기 추가 설정 (툴바, 단축키 등)."""
        return {}


class RendererPlugin:
    """타입별 렌더링 규칙."""

    def get_render_config(self) -> dict:
        """노드 타입별 렌더링 규칙."""
        return {}

    def get_node_css_class(self, node_type: str) -> Optional[str]:
        """노드 타입에 추가할 CSS 클래스."""
        return None

    def get_toc_config(self) -> dict:
        """목차(TOC) 구성 설정."""
        return {"enabled": True, "depth": 3}


class ChunkingPlugin:
    """타입별 청킹 전략."""

    def get_config(self) -> ChunkingConfig:
        """타입별 ChunkingConfig 반환."""
        return ChunkingConfig()

    def should_index(self, version_status: str) -> bool:
        """해당 버전 상태의 문서를 벡터화할지 여부."""
        policy = self.get_config().index_version_policy
        if policy == "published_only":
            return version_status in ("PUBLISHED", "published")
        elif policy in ("latest", "all"):
            return True
        return False


class RAGPlugin:
    """타입별 RAG 프롬프트/설정."""

    def get_prompt_template(self) -> "PromptTemplate":
        return DefaultPromptTemplate()

    def get_context_config(self) -> dict:
        """컨텍스트 설정 반환."""
        return {"max_context_tokens": 6000, "top_n": 5}

    def get_reranker_config(self) -> dict:
        return {"enabled": True, "top_n": 5}


class SearchPlugin:
    """타입별 검색 설정."""

    def get_config(self) -> dict:
        """Phase 8 search_config 형식 반환. 빈 dict = 기본값 사용."""
        return {}

    def get_boost_config(self) -> dict:
        """검색 필드별 가중치."""
        return {
            "title": 2.0,
            "content": 1.0,
            "metadata": 0.5,
        }

    def get_searchable_node_types(self) -> list[str]:
        """검색 대상 노드 타입. 빈 리스트 = 전체."""
        return []

    def get_snippet_config(self) -> dict:
        """스니펫 생성 설정."""
        return {"max_length": 200, "highlight": True}


class WorkflowPlugin:
    """타입별 워크플로 단계/규칙."""

    def get_allowed_transitions(self) -> dict:
        """상태 전이 제약. 빈 dict = Phase 5 기본 워크플로 그대로."""
        return {}

    def requires_approval(self) -> bool:
        """승인 단계 필수 여부."""
        return True

    def get_review_roles(self) -> list[str]:
        """검토 가능한 역할 목록. 빈 리스트 = 모든 역할."""
        return []


# ---------------------------------------------------------------------------
# PromptTemplate 추상화 (Phase 11 연계)
# ---------------------------------------------------------------------------

class PromptTemplate:
    """RAG 시스템 프롬프트 템플릿 기본 클래스."""

    _SYSTEM_TEMPLATE = """당신은 문서 기반 지식 도우미입니다.
아래의 문서 컨텍스트를 바탕으로 사용자의 질문에 정확하게 답변하세요.

규칙:
1. 반드시 제공된 컨텍스트에 근거해서만 답변하세요.
2. 답변에서 근거로 사용한 내용은 [숫자] 형식으로 출처를 표기하세요. 예: [1], [2]
3. 컨텍스트에 없는 내용은 "제공된 문서에서 해당 정보를 찾을 수 없습니다"라고 답변하세요.
4. 답변은 한국어로 작성하세요.
5. 마크다운 형식을 활용해 가독성 있게 작성하세요.

<document_context>
{context}
</document_context>"""

    def render(self, context: str) -> str:
        """컨텍스트를 포함한 시스템 프롬프트 반환."""
        return self._SYSTEM_TEMPLATE.format(context=context)


class DefaultPromptTemplate(PromptTemplate):
    """기본 프롬프트 템플릿 (Phase 11 호환)."""
    pass


# ---------------------------------------------------------------------------
# Default 서브플러그인 구현체
# ---------------------------------------------------------------------------

class DefaultMetadataSchemaPlugin(MetadataSchemaPlugin):
    pass


class DefaultEditorPlugin(EditorPlugin):
    pass


class DefaultRendererPlugin(RendererPlugin):
    pass


class DefaultChunkingPlugin(ChunkingPlugin):
    pass


class DefaultRAGPlugin(RAGPlugin):
    pass


class DefaultSearchPlugin(SearchPlugin):
    pass


class DefaultWorkflowPlugin(WorkflowPlugin):
    pass


# ---------------------------------------------------------------------------
# DocumentTypePlugin 루트 인터페이스
# ---------------------------------------------------------------------------

class DocumentTypePlugin:
    """DocumentType 플러그인 루트 인터페이스.

    서브클래스는 get_type_name()과 get_display_name()을 반드시 구현한다.
    서브플러그인 접근자는 기본 구현을 반환하며, 오버라이드로 커스터마이징한다.
    """

    def get_type_name(self) -> str:
        """타입 코드 반환. 영문 대문자, 숫자, 밑줄만 허용 (예: POLICY)."""
        raise NotImplementedError

    def get_display_name(self) -> str:
        """UI 표시 이름 반환 (예: 정책/규정)."""
        raise NotImplementedError

    def get_description(self) -> str:
        return ""

    # 서브플러그인 접근자 (기본 구현 제공)
    def metadata_schema_plugin(self) -> MetadataSchemaPlugin:
        return DefaultMetadataSchemaPlugin()

    def editor_plugin(self) -> EditorPlugin:
        return DefaultEditorPlugin()

    def renderer_plugin(self) -> RendererPlugin:
        return DefaultRendererPlugin()

    def chunking_plugin(self) -> ChunkingPlugin:
        return DefaultChunkingPlugin()

    def rag_plugin(self) -> RAGPlugin:
        return DefaultRAGPlugin()

    def search_plugin(self) -> SearchPlugin:
        return DefaultSearchPlugin()

    def workflow_plugin(self) -> WorkflowPlugin:
        return DefaultWorkflowPlugin()


# ---------------------------------------------------------------------------
# DefaultDocumentTypePlugin (폴백)
# ---------------------------------------------------------------------------

class DefaultDocumentTypePlugin(DocumentTypePlugin):
    """미등록 타입에 대한 폴백 플러그인."""

    def __init__(self, type_name: str = "UNKNOWN"):
        self._type_name = type_name

    def get_type_name(self) -> str:
        return self._type_name

    def get_display_name(self) -> str:
        return self._type_name

    def get_description(self) -> str:
        return "기본 문서 유형 (플러그인 미등록)"


# ---------------------------------------------------------------------------
# ConfigurableDocumentTypePlugin (DB 기반 설정)
# ---------------------------------------------------------------------------

class ConfigurableChunkingPlugin(ChunkingPlugin):
    """DB plugin_config에서 로드한 chunking_config로 동작."""

    def __init__(self, raw: dict, base: Optional[ChunkingConfig] = None):
        self._raw = raw
        self._base = base or ChunkingConfig()

    def get_config(self) -> ChunkingConfig:
        r = self._raw
        b = self._base
        return ChunkingConfig(
            strategy=r.get("strategy", b.strategy),
            max_chunk_tokens=int(r.get("max_chunk_tokens", b.max_chunk_tokens)),
            min_chunk_tokens=int(r.get("min_chunk_tokens", b.min_chunk_tokens)),
            overlap_tokens=int(r.get("overlap_tokens", b.overlap_tokens)),
            include_parent_context=bool(r.get("include_parent_context", b.include_parent_context)),
            parent_context_depth=int(r.get("parent_context_depth", b.parent_context_depth)),
            index_version_policy=r.get("index_version_policy", b.index_version_policy),
            exclude_node_types=r.get("exclude_node_types", list(b.exclude_node_types)),
            merge_strategy=r.get("merge_strategy", b.merge_strategy),
        )


class ConfigurableRAGPlugin(RAGPlugin):
    """DB plugin_config에서 로드한 rag_config로 동작."""

    def __init__(self, raw: dict, base_template: Optional[PromptTemplate] = None):
        self._raw = raw
        self._base_template = base_template or DefaultPromptTemplate()

    def get_prompt_template(self) -> PromptTemplate:
        custom_prompt = self._raw.get("system_prompt")
        if custom_prompt:
            # P12-SEC-01: custom_prompt의 중괄호를 이스케이프하여 .format() KeyError 방지.
            # 관리자가 입력한 프롬프트에 {var} 형식 문자열이 있어도 안전하게 처리된다.
            safe_prompt = str(custom_prompt).replace("{", "{{").replace("}", "}}")
            template = PromptTemplate()
            template._SYSTEM_TEMPLATE = safe_prompt + "\n\n<document_context>\n{context}\n</document_context>"
            return template
        return self._base_template

    def get_context_config(self) -> dict:
        defaults = {"max_context_tokens": 6000, "top_n": 5}
        return {**defaults, **self._raw}


class ConfigurableSearchPlugin(SearchPlugin):
    """DB plugin_config에서 로드한 search_config로 동작."""

    def __init__(self, raw: dict):
        self._raw = raw

    def get_config(self) -> dict:
        return self._raw

    def get_boost_config(self) -> dict:
        return self._raw.get("boost", super().get_boost_config())

    def get_searchable_node_types(self) -> list[str]:
        return self._raw.get("searchable_node_types", [])

    def get_snippet_config(self) -> dict:
        return self._raw.get("snippet", {"max_length": 200, "highlight": True})


class ConfigurableMetadataSchemaPlugin(MetadataSchemaPlugin):
    """DB plugin_config에서 로드한 metadata_schema로 동작."""

    def __init__(self, schema: dict):
        self._schema = schema

    def get_schema(self) -> dict:
        return self._schema


class ConfigurableDocumentTypePlugin(DocumentTypePlugin):
    """DB document_types 테이블 설정으로 동작하는 플러그인.

    Admin UI에서 생성/편집된 타입이 이 클래스로 동작한다.
    plugin_config 내 서브키: chunking_config, rag_config, search_config,
    metadata_schema, editor_config, renderer_config, workflow_config
    """

    def __init__(self, type_name: str, config: dict):
        self._type_name = type_name
        self._config = config
        # 내장 플러그인 기반 (없으면 기본값)
        self._base: Optional[DocumentTypePlugin] = config.get("_base_plugin")

    def get_type_name(self) -> str:
        return self._type_name

    def get_display_name(self) -> str:
        return self._config.get("display_name", self._type_name)

    def get_description(self) -> str:
        return self._config.get("description", "")

    def chunking_plugin(self) -> ChunkingPlugin:
        """청킹 플러그인 반환.

        우선순위: DB chunking_config > _base plugin config > DefaultChunkingPlugin.
        DB config가 있으면 _base config를 기본값으로 삼아 오버라이드한다.
        """
        raw = self._config.get("chunking_config") or {}
        base_config = self._base.chunking_plugin().get_config() if self._base else None
        if raw:
            return ConfigurableChunkingPlugin(raw, base_config)
        return self._base.chunking_plugin() if self._base else DefaultChunkingPlugin()

    def rag_plugin(self) -> RAGPlugin:
        """RAG 플러그인 반환.

        우선순위: DB rag_config(system_prompt 포함) > _base prompt template > DefaultRAGPlugin.
        """
        raw = self._config.get("rag_config") or {}
        base_template = self._base.rag_plugin().get_prompt_template() if self._base else None
        if raw:
            return ConfigurableRAGPlugin(raw, base_template)
        return self._base.rag_plugin() if self._base else DefaultRAGPlugin()

    def search_plugin(self) -> SearchPlugin:
        raw = self._config.get("search_config") or {}
        if raw:
            return ConfigurableSearchPlugin(raw)
        return self._base.search_plugin() if self._base else DefaultSearchPlugin()

    def metadata_schema_plugin(self) -> MetadataSchemaPlugin:
        schema = self._config.get("metadata_schema") or {}
        if schema:
            return ConfigurableMetadataSchemaPlugin(schema)
        return self._base.metadata_schema_plugin() if self._base else DefaultMetadataSchemaPlugin()

    def editor_plugin(self) -> EditorPlugin:
        return self._base.editor_plugin() if self._base else DefaultEditorPlugin()

    def renderer_plugin(self) -> RendererPlugin:
        return self._base.renderer_plugin() if self._base else DefaultRendererPlugin()

    def workflow_plugin(self) -> WorkflowPlugin:
        return self._base.workflow_plugin() if self._base else DefaultWorkflowPlugin()


# ---------------------------------------------------------------------------
# DocumentTypeRegistry — 싱글턴 레지스트리
# ---------------------------------------------------------------------------

class DocumentTypeRegistry:
    """DocumentType 플러그인 레지스트리 (싱글턴).

    등록 우선순위:
      1. 코드 등록 플러그인 (내장 타입)
      2. DB 기반 설정 (Admin UI 오버라이드)
      3. DefaultDocumentTypePlugin (폴백)

    테스트에서 reset()으로 격리 가능.
    """

    _instance: Optional["DocumentTypeRegistry"] = None
    _TYPE_NAME_PATTERN = re.compile(r'^[A-Z][A-Z0-9_]*$')

    def __init__(self) -> None:
        self._plugins: dict[str, DocumentTypePlugin] = {}

    @classmethod
    def instance(cls) -> "DocumentTypeRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """테스트용 레지스트리 초기화."""
        cls._instance = None

    def register(self, plugin: DocumentTypePlugin) -> None:
        """플러그인 등록. type_name 중복 등록 시 ValueError 발생."""
        type_name = plugin.get_type_name()
        if not self._TYPE_NAME_PATTERN.match(type_name):
            raise ValueError(
                f"Invalid type_name '{type_name}': 영문 대문자, 숫자, 밑줄만 허용됩니다."
            )
        if type_name in self._plugins:
            raise ValueError(f"플러그인이 이미 등록되어 있습니다: {type_name}")
        self._plugins[type_name] = plugin
        logger.debug("DocumentType 플러그인 등록: %s (%s)", type_name, plugin.get_display_name())

    def get(
        self,
        document_type: str,
        *,
        conn=None,
    ) -> DocumentTypePlugin:
        """플러그인 조회. 미등록 타입은 DB 설정 → 폴백 순서로 처리."""
        # 1. 코드 등록 플러그인
        if document_type in self._plugins:
            base = self._plugins[document_type]
            # DB 오버라이드가 있으면 병합
            if conn is not None:
                db_config = self._load_plugin_config_from_db(conn, document_type)
                if db_config:
                    db_config["_base_plugin"] = base
                    db_config["display_name"] = base.get_display_name()
                    return ConfigurableDocumentTypePlugin(document_type, db_config)
            return base

        # 2. DB 기반 설정
        if conn is not None:
            db_config = self._load_db_type(conn, document_type)
            if db_config:
                return ConfigurableDocumentTypePlugin(document_type, db_config)

        # 3. 폴백
        logger.debug(
            "DocumentType '%s' 플러그인 미등록 — DefaultDocumentTypePlugin 사용", document_type
        )
        return DefaultDocumentTypePlugin(document_type)

    def list_all(self) -> list[DocumentTypePlugin]:
        """등록된 모든 플러그인 목록 반환."""
        return list(self._plugins.values())

    def list_type_names(self) -> list[str]:
        """등록된 타입 코드 목록 반환."""
        return list(self._plugins.keys())

    def is_builtin(self, type_name: str) -> bool:
        """코드 등록(내장) 타입 여부."""
        return type_name in self._plugins

    # ------------------------------------------------------------------
    # 내부 DB 조회 헬퍼
    # ------------------------------------------------------------------

    def _load_plugin_config_from_db(self, conn, type_name: str) -> Optional[dict]:
        """document_types.plugin_config 서브키 반환 (내장 타입 오버라이드용)."""
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT plugin_config FROM document_types WHERE type_code = %s",
                    (type_name,),
                )
                row = cur.fetchone()
                if row and row.get("plugin_config"):
                    cfg = row["plugin_config"]
                    # sub-key 중 하나라도 있으면 오버라이드 적용
                    sub_keys = {"chunking_config", "rag_config", "search_config",
                                "metadata_schema", "editor_config", "renderer_config",
                                "workflow_config"}
                    if any(k in cfg for k in sub_keys):
                        return cfg
        except Exception as exc:
            logger.warning("plugin_config 오버라이드 조회 실패 (%s): %s", type_name, exc)
        return None

    def _load_db_type(self, conn, type_name: str) -> Optional[dict]:
        """document_types 테이블에서 타입 설정 전체 로드."""
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT display_name, description, plugin_config FROM document_types WHERE type_code = %s AND status = 'ACTIVE'",
                    (type_name,),
                )
                row = cur.fetchone()
                if row:
                    config = dict(row.get("plugin_config") or {})
                    config["display_name"] = row["display_name"]
                    config["description"] = row.get("description", "")
                    return config
        except Exception as exc:
            logger.warning("DB DocumentType 조회 실패 (%s): %s", type_name, exc)
        return None
