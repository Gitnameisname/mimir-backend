"""
REPORT (보고서) DocumentType 플러그인.

특성:
  - 섹션 단위 청킹 (max_chunk_tokens=600, overlap_tokens=100)
  - 섹션 요약 중심 RAG 프롬프트
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


class ReportMetadataSchemaPlugin(MetadataSchemaPlugin):

    _SCHEMA = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": {
            "report_period": {"type": "string", "title": "보고 기간"},
            "author_department": {"type": "string", "title": "작성 부서"},
            "classification": {
                "type": "string",
                "enum": ["일반", "대외비", "기밀"],
                "title": "비밀 등급",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "title": "태그",
            },
        },
    }

    _UI_SCHEMA = {
        "ui:order": ["report_period", "author_department", "classification", "tags"],
        "classification": {"ui:widget": "select"},
    }

    def get_schema(self) -> dict:
        return self._SCHEMA

    def get_ui_schema(self) -> dict:
        return self._UI_SCHEMA


class ReportEditorPlugin(EditorPlugin):

    def get_allowed_node_types(self) -> list[str]:
        return [
            "heading1", "heading2", "heading3",
            "paragraph", "table", "chart", "list",
            "summary_box", "figure",
        ]

    def get_default_structure(self) -> list[dict]:
        return [
            {"type": "heading1", "content": "요약"},
            {"type": "summary_box", "content": ""},
            {"type": "heading1", "content": "1. 서론"},
            {"type": "paragraph", "content": ""},
            {"type": "heading1", "content": "2. 본론"},
            {"type": "paragraph", "content": ""},
            {"type": "heading1", "content": "3. 결론"},
            {"type": "paragraph", "content": ""},
        ]


class ReportRendererPlugin(RendererPlugin):

    def get_render_config(self) -> dict:
        return {
            "summary_box": {
                "css_class": "summary-highlight",
                "border": True,
            }
        }

    def get_toc_config(self) -> dict:
        return {"enabled": True, "depth": 2, "label": "목차"}


class ReportChunkingPlugin(ChunkingPlugin):

    def get_config(self) -> ChunkingConfig:
        return ChunkingConfig(
            strategy="node_based",
            max_chunk_tokens=600,
            min_chunk_tokens=50,
            overlap_tokens=100,
            include_parent_context=True,
            parent_context_depth=1,
            index_version_policy="published_only",
            exclude_node_types=["figure"],
            merge_strategy="merge_siblings",
        )


class ReportPromptTemplate(PromptTemplate):

    _SYSTEM_TEMPLATE = """당신은 보고서 분석 전문 AI입니다.
아래 보고서 내용을 바탕으로 질문에 답하세요.

규칙:
1. 보고서의 핵심 수치와 결론을 정확히 인용하세요.
2. 섹션 제목을 활용해 답변 구조를 명확히 하세요.
3. 수치 데이터는 원문 그대로 표기하세요.
4. 출처를 [1], [2] 형식으로 표시하세요.
5. 답변은 한국어로 작성하세요.

<document_context>
{context}
</document_context>"""


class ReportRAGPlugin(RAGPlugin):

    def get_prompt_template(self) -> PromptTemplate:
        return ReportPromptTemplate()

    def get_context_config(self) -> dict:
        return {"max_context_tokens": 6000, "top_n": 5}


class ReportSearchPlugin(SearchPlugin):

    def get_boost_config(self) -> dict:
        return {
            "title": 2.0,
            "content": 1.0,
            "metadata.report_period": 2.0,
        }

    def get_searchable_node_types(self) -> list[str]:
        return ["heading1", "heading2", "paragraph", "summary_box"]

    def get_snippet_config(self) -> dict:
        return {"max_length": 250, "highlight": True}


class ReportWorkflowPlugin(WorkflowPlugin):

    def requires_approval(self) -> bool:
        return True

    def get_review_roles(self) -> list[str]:
        return []


class REPORTPlugin(DocumentTypePlugin):

    def get_type_name(self) -> str:
        return "REPORT"

    def get_display_name(self) -> str:
        return "보고서"

    def get_description(self) -> str:
        return "분석 보고서, 현황 보고, 기획 문서"

    def metadata_schema_plugin(self) -> MetadataSchemaPlugin:
        return ReportMetadataSchemaPlugin()

    def editor_plugin(self) -> EditorPlugin:
        return ReportEditorPlugin()

    def renderer_plugin(self) -> RendererPlugin:
        return ReportRendererPlugin()

    def chunking_plugin(self) -> ChunkingPlugin:
        return ReportChunkingPlugin()

    def rag_plugin(self) -> RAGPlugin:
        return ReportRAGPlugin()

    def search_plugin(self) -> SearchPlugin:
        return ReportSearchPlugin()

    def workflow_plugin(self) -> WorkflowPlugin:
        return ReportWorkflowPlugin()
