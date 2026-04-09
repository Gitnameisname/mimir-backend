"""
DocumentType 플러그인 시스템 패키지.

Phase 12: CLAUDE.md "문서 타입은 하드코딩 금지 — 구조는 generic + config 기반"
원칙을 프레임워크 수준에서 구현한다.

사용법:
    from app.plugins import DocumentTypeRegistry

    registry = DocumentTypeRegistry.instance()
    plugin = registry.get("POLICY")
    chunking_config = plugin.chunking_plugin().get_config()
"""

from app.plugins.base import (
    ChunkingConfig,
    ChunkingPlugin,
    DefaultChunkingPlugin,
    DefaultDocumentTypePlugin,
    DefaultEditorPlugin,
    DefaultMetadataSchemaPlugin,
    DefaultRAGPlugin,
    DefaultRendererPlugin,
    DefaultSearchPlugin,
    DefaultWorkflowPlugin,
    DocumentTypePlugin,
    DocumentTypeRegistry,
    EditorPlugin,
    MetadataSchemaPlugin,
    RAGPlugin,
    RendererPlugin,
    SearchPlugin,
    WorkflowPlugin,
)

__all__ = [
    "DocumentTypePlugin",
    "DocumentTypeRegistry",
    "MetadataSchemaPlugin",
    "EditorPlugin",
    "RendererPlugin",
    "ChunkingPlugin",
    "ChunkingConfig",
    "RAGPlugin",
    "SearchPlugin",
    "WorkflowPlugin",
    "DefaultDocumentTypePlugin",
    "DefaultMetadataSchemaPlugin",
    "DefaultEditorPlugin",
    "DefaultRendererPlugin",
    "DefaultChunkingPlugin",
    "DefaultRAGPlugin",
    "DefaultSearchPlugin",
    "DefaultWorkflowPlugin",
]
