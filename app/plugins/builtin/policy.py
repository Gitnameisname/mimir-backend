"""
POLICY (정책/규정) DocumentType 플러그인.

특성:
  - 조항 단위 청킹, 조항 번호 컨텍스트 포함
  - 원문 인용 강조 RAG 프롬프트
  - 정책 번호(policy_number) 검색 우선 부스트
  - 반드시 승인 필요, POLICY_REVIEWER/ADMIN만 검토 가능
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


# ---------------------------------------------------------------------------
# MetadataSchemaPlugin
# ---------------------------------------------------------------------------

class PolicyMetadataSchemaPlugin(MetadataSchemaPlugin):

    _SCHEMA = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": {
            "policy_number": {
                "type": "string",
                "title": "정책 번호",
                "description": "예: POL-2025-001",
            },
            "effective_date": {
                "type": "string",
                "format": "date",
                "title": "시행일",
            },
            "review_date": {
                "type": "string",
                "format": "date",
                "title": "검토 예정일",
            },
            "owner_department": {
                "type": "string",
                "title": "담당 부서",
            },
            "compliance_standards": {
                "type": "array",
                "items": {"type": "string"},
                "title": "준거 기준",
                "description": "예: ISO 27001, ISMS-P",
            },
        },
    }

    _UI_SCHEMA = {
        "ui:order": ["policy_number", "effective_date", "review_date", "owner_department", "compliance_standards"],
        "policy_number": {"ui:placeholder": "POL-2025-001", "ui:autofocus": True},
        "effective_date": {"ui:widget": "date"},
        "review_date": {"ui:widget": "date"},
        "compliance_standards": {
            "ui:widget": "checkboxes",
            "ui:options": ["ISO 27001", "ISMS-P", "SOC 2", "GDPR"],
        },
    }

    def get_schema(self) -> dict:
        return self._SCHEMA

    def get_ui_schema(self) -> dict:
        return self._UI_SCHEMA


# ---------------------------------------------------------------------------
# EditorPlugin
# ---------------------------------------------------------------------------

class PolicyEditorPlugin(EditorPlugin):

    def get_allowed_node_types(self) -> list[str]:
        return [
            "heading1", "heading2", "heading3",
            "paragraph", "article", "table", "list", "note",
        ]

    def get_default_structure(self) -> list[dict]:
        return [
            {"type": "heading1", "content": "1. 목적"},
            {"type": "paragraph", "content": ""},
            {"type": "heading1", "content": "2. 적용 범위"},
            {"type": "paragraph", "content": ""},
            {"type": "heading1", "content": "3. 정책 본문"},
            {"type": "article", "content": "제1조 (기본 원칙)"},
        ]

    def get_editor_config(self) -> dict:
        return {"show_article_numbering": True}


# ---------------------------------------------------------------------------
# RendererPlugin
# ---------------------------------------------------------------------------

class PolicyRendererPlugin(RendererPlugin):

    def get_render_config(self) -> dict:
        return {
            "article": {
                "prefix": "제",
                "suffix": "조",
                "numbered": True,
            }
        }

    def get_toc_config(self) -> dict:
        return {"enabled": True, "depth": 2, "label": "목차"}


# ---------------------------------------------------------------------------
# ChunkingPlugin
# ---------------------------------------------------------------------------

class PolicyChunkingPlugin(ChunkingPlugin):

    def get_config(self) -> ChunkingConfig:
        return ChunkingConfig(
            strategy="node_based",
            max_chunk_tokens=512,
            min_chunk_tokens=50,
            overlap_tokens=50,
            include_parent_context=True,
            parent_context_depth=2,
            index_version_policy="published_only",
            exclude_node_types=["metadata", "attachment"],
            merge_strategy="merge_siblings",
        )


# ---------------------------------------------------------------------------
# RAGPlugin
# ---------------------------------------------------------------------------

class PolicyPromptTemplate(PromptTemplate):

    _SYSTEM_TEMPLATE = """당신은 정책/규정 문서 전문 AI입니다.
아래 정책 문서 조항을 바탕으로 질문에 답하세요.

규칙:
1. 정책 조항의 원문을 가능한 한 그대로 인용하세요.
2. 조항 번호를 함께 언급하세요 (예: 제3조에 따르면...).
3. 모호한 경우 "정책 원문을 직접 확인하시기 바랍니다"라고 안내하세요.
4. 출처를 [1], [2] 형식으로 표시하세요.
5. 답변은 한국어로 작성하세요.

<document_context>
{context}
</document_context>"""


class PolicyRAGPlugin(RAGPlugin):

    def get_prompt_template(self) -> PromptTemplate:
        return PolicyPromptTemplate()

    def get_context_config(self) -> dict:
        return {"max_context_tokens": 8000, "top_n": 7}


# ---------------------------------------------------------------------------
# SearchPlugin
# ---------------------------------------------------------------------------

class PolicySearchPlugin(SearchPlugin):

    def get_boost_config(self) -> dict:
        return {
            "title": 3.0,
            "content": 1.0,
            "metadata.policy_number": 5.0,
        }

    def get_searchable_node_types(self) -> list[str]:
        return ["heading1", "heading2", "heading3", "article", "paragraph"]

    def get_snippet_config(self) -> dict:
        return {"max_length": 300, "highlight": True}


# ---------------------------------------------------------------------------
# WorkflowPlugin
# ---------------------------------------------------------------------------

class PolicyWorkflowPlugin(WorkflowPlugin):

    def requires_approval(self) -> bool:
        return True

    def get_review_roles(self) -> list[str]:
        return ["POLICY_REVIEWER", "ADMIN"]


# ---------------------------------------------------------------------------
# POLICYPlugin — 루트 플러그인
# ---------------------------------------------------------------------------

class POLICYPlugin(DocumentTypePlugin):

    def get_type_name(self) -> str:
        return "POLICY"

    def get_display_name(self) -> str:
        return "정책/규정"

    def get_description(self) -> str:
        return "조직 정책, 규정, 지침 문서"

    def metadata_schema_plugin(self) -> MetadataSchemaPlugin:
        return PolicyMetadataSchemaPlugin()

    def editor_plugin(self) -> EditorPlugin:
        return PolicyEditorPlugin()

    def renderer_plugin(self) -> RendererPlugin:
        return PolicyRendererPlugin()

    def chunking_plugin(self) -> ChunkingPlugin:
        return PolicyChunkingPlugin()

    def rag_plugin(self) -> RAGPlugin:
        return PolicyRAGPlugin()

    def search_plugin(self) -> SearchPlugin:
        return PolicySearchPlugin()

    def workflow_plugin(self) -> WorkflowPlugin:
        return PolicyWorkflowPlugin()
