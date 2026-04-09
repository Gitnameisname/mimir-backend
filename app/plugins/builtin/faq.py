"""
FAQ DocumentType 플러그인.

특성:
  - Q&A 쌍 단위 청킹 (max_chunk_tokens=256, overlap_tokens=0)
  - Q&A 형식 RAG 프롬프트
  - 본문 가중치 높은 검색
  - 승인 불필요 (requires_approval=False)
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


class FAQMetadataSchemaPlugin(MetadataSchemaPlugin):

    _SCHEMA = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": {
            "category": {"type": "string", "title": "FAQ 카테고리"},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "title": "태그",
            },
        },
    }

    _UI_SCHEMA = {
        "ui:order": ["category", "tags"],
        "category": {"ui:placeholder": "예: 인사, IT 지원, 경비"},
        "tags": {"ui:widget": "tags"},
    }

    def get_schema(self) -> dict:
        return self._SCHEMA

    def get_ui_schema(self) -> dict:
        return self._UI_SCHEMA


class FAQEditorPlugin(EditorPlugin):

    def get_allowed_node_types(self) -> list[str]:
        return ["faq_question", "faq_answer", "paragraph"]

    def get_default_structure(self) -> list[dict]:
        return [
            {"type": "faq_question", "content": "Q: 질문을 입력하세요"},
            {"type": "faq_answer", "content": "A: 답변을 입력하세요"},
        ]

    def get_editor_config(self) -> dict:
        return {"pair_mode": True, "auto_link_qa": True}


class FAQRendererPlugin(RendererPlugin):

    def get_render_config(self) -> dict:
        return {
            "faq_question": {
                "css_class": "faq-question",
                "prefix": "Q. ",
                "bold": True,
            },
            "faq_answer": {
                "css_class": "faq-answer",
                "prefix": "A. ",
            },
        }

    def get_toc_config(self) -> dict:
        return {"enabled": False}


class FAQChunkingPlugin(ChunkingPlugin):

    def get_config(self) -> ChunkingConfig:
        return ChunkingConfig(
            strategy="node_based",
            max_chunk_tokens=256,
            min_chunk_tokens=20,
            overlap_tokens=0,
            include_parent_context=False,
            parent_context_depth=0,
            index_version_policy="published_only",
            exclude_node_types=[],
            # merge_qa_pair → Phase 10에서 미구현 시 merge_siblings 폴백
            merge_strategy="merge_siblings",
        )


class FAQPromptTemplate(PromptTemplate):

    _SYSTEM_TEMPLATE = """당신은 FAQ 문서를 바탕으로 질문에 답하는 AI입니다.
관련 Q&A 항목을 참조하여 명확하고 친절하게 답변하세요.

규칙:
1. 관련 FAQ 항목을 직접 인용하세요.
2. 여러 FAQ 항목이 관련 있으면 모두 안내하세요.
3. FAQ에 없는 내용은 "FAQ에서 해당 항목을 찾을 수 없습니다"라고 안내하세요.
4. 출처를 [1], [2] 형식으로 표시하세요.
5. 답변은 한국어로 작성하세요.

<document_context>
{context}
</document_context>"""


class FAQRAGPlugin(RAGPlugin):

    def get_prompt_template(self) -> PromptTemplate:
        return FAQPromptTemplate()

    def get_context_config(self) -> dict:
        return {"max_context_tokens": 3000, "top_n": 10}


class FAQSearchPlugin(SearchPlugin):

    def get_boost_config(self) -> dict:
        return {
            "title": 2.0,
            "content": 1.5,
        }

    def get_searchable_node_types(self) -> list[str]:
        return ["faq_question", "faq_answer"]

    def get_snippet_config(self) -> dict:
        return {"max_length": 150, "highlight": True}


class FAQWorkflowPlugin(WorkflowPlugin):

    def requires_approval(self) -> bool:
        return False  # FAQ는 승인 없이 직접 게시 가능

    def get_review_roles(self) -> list[str]:
        return []


class FAQPlugin(DocumentTypePlugin):

    def get_type_name(self) -> str:
        return "FAQ"

    def get_display_name(self) -> str:
        return "FAQ"

    def get_description(self) -> str:
        return "자주 묻는 질문과 답변"

    def metadata_schema_plugin(self) -> MetadataSchemaPlugin:
        return FAQMetadataSchemaPlugin()

    def editor_plugin(self) -> EditorPlugin:
        return FAQEditorPlugin()

    def renderer_plugin(self) -> RendererPlugin:
        return FAQRendererPlugin()

    def chunking_plugin(self) -> ChunkingPlugin:
        return FAQChunkingPlugin()

    def rag_plugin(self) -> RAGPlugin:
        return FAQRAGPlugin()

    def search_plugin(self) -> SearchPlugin:
        return FAQSearchPlugin()

    def workflow_plugin(self) -> WorkflowPlugin:
        return FAQWorkflowPlugin()
