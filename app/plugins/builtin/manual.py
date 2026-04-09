"""
MANUAL (매뉴얼/절차서) DocumentType 플러그인.

특성:
  - 절차 단계 단위 청킹 (max_chunk_tokens=400)
  - 단계별 절차 안내 RAG 프롬프트
  - 대상 독자(target_audience), 버전 태그(version_tag) metadata
  - 승인 필요
"""

from app.plugins.base import (
    ChunkingConfig,
    ChunkingPlugin,
    DocumentTypePlugin,
    EditorPlugin,
    MetadataSchemaPlugin,
    PromptTemplate,
    RAGPlugin,
    RendererPlugin,
    SearchPlugin,
    WorkflowPlugin,
)


class ManualMetadataSchemaPlugin(MetadataSchemaPlugin):

    _SCHEMA = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": {
            "target_audience": {"type": "string", "title": "대상 독자"},
            "version_tag": {"type": "string", "title": "버전 태그"},
            "prerequisite": {"type": "string", "title": "선행 지식/작업"},
        },
    }

    _UI_SCHEMA = {
        "ui:order": ["target_audience", "version_tag", "prerequisite"],
        "target_audience": {"ui:placeholder": "예: 신입 직원, 시스템 관리자"},
        "version_tag": {"ui:placeholder": "예: v1.2.0"},
    }

    def get_schema(self) -> dict:
        return self._SCHEMA

    def get_ui_schema(self) -> dict:
        return self._UI_SCHEMA


class ManualEditorPlugin(EditorPlugin):

    def get_allowed_node_types(self) -> list[str]:
        return [
            "heading1", "heading2", "heading3",
            "paragraph", "step", "warning", "note",
            "ordered_list", "unordered_list", "image", "table",
        ]

    def get_default_structure(self) -> list[dict]:
        return [
            {"type": "heading1", "content": "개요"},
            {"type": "paragraph", "content": ""},
            {"type": "heading1", "content": "절차"},
            {"type": "step", "content": "1단계: "},
            {"type": "step", "content": "2단계: "},
        ]


class ManualRendererPlugin(RendererPlugin):

    def get_render_config(self) -> dict:
        return {
            "step": {
                "numbered": True,
                "prefix": "단계 ",
            },
            "warning": {
                "css_class": "warning-block",
                "icon": "⚠️",
            },
        }

    def get_toc_config(self) -> dict:
        return {"enabled": True, "depth": 3, "label": "목차"}


class ManualChunkingPlugin(ChunkingPlugin):

    def get_config(self) -> ChunkingConfig:
        return ChunkingConfig(
            strategy="node_based",
            max_chunk_tokens=400,
            min_chunk_tokens=30,
            overlap_tokens=30,
            include_parent_context=True,
            parent_context_depth=2,
            index_version_policy="published_only",
            exclude_node_types=["metadata"],
            merge_strategy="merge_siblings",
        )


class ManualPromptTemplate(PromptTemplate):

    _SYSTEM_TEMPLATE = """당신은 절차/매뉴얼 문서 전문 AI입니다.
아래 매뉴얼 내용을 바탕으로 절차를 단계별로 설명하세요.

규칙:
1. 절차는 번호가 있는 목록 형식으로 답변하세요.
2. 각 단계에서 주의사항이 있으면 강조하세요.
3. 선행 조건이 있으면 먼저 언급하세요.
4. 출처를 [1], [2] 형식으로 표시하세요.
5. 답변은 한국어로 작성하세요.

<document_context>
{context}
</document_context>"""


class ManualRAGPlugin(RAGPlugin):

    def get_prompt_template(self) -> PromptTemplate:
        return ManualPromptTemplate()

    def get_context_config(self) -> dict:
        return {"max_context_tokens": 5000, "top_n": 5}


class ManualSearchPlugin(SearchPlugin):

    def get_boost_config(self) -> dict:
        return {
            "title": 2.5,
            "content": 1.0,
            "metadata.version_tag": 2.0,
        }

    def get_searchable_node_types(self) -> list[str]:
        return ["heading1", "heading2", "heading3", "step", "paragraph"]

    def get_snippet_config(self) -> dict:
        return {"max_length": 250, "highlight": True}


class ManualWorkflowPlugin(WorkflowPlugin):

    def requires_approval(self) -> bool:
        return True

    def get_review_roles(self) -> list[str]:
        return []  # 모든 역할 검토 가능


class MANUALPlugin(DocumentTypePlugin):

    def get_type_name(self) -> str:
        return "MANUAL"

    def get_display_name(self) -> str:
        return "매뉴얼/절차서"

    def get_description(self) -> str:
        return "업무 절차, 운영 매뉴얼, 사용 안내서"

    def metadata_schema_plugin(self) -> MetadataSchemaPlugin:
        return ManualMetadataSchemaPlugin()

    def editor_plugin(self) -> EditorPlugin:
        return ManualEditorPlugin()

    def renderer_plugin(self) -> RendererPlugin:
        return ManualRendererPlugin()

    def chunking_plugin(self) -> ChunkingPlugin:
        return ManualChunkingPlugin()

    def rag_plugin(self) -> RAGPlugin:
        return ManualRAGPlugin()

    def search_plugin(self) -> SearchPlugin:
        return ManualSearchPlugin()

    def workflow_plugin(self) -> WorkflowPlugin:
        return ManualWorkflowPlugin()
