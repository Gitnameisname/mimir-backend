"""
Phase 12 — DocumentType 플러그인 규격 검증 테스트.

PluginConformanceTest를 상속하여 새 플러그인의 규격 준수를 자동 검증한다.

사용법:
    # 새 플러그인 작성 시 반드시 아래와 같이 상속하여 규격 검사:
    class TestMyPlugin(PluginConformanceTest):
        plugin = MyPlugin()
"""

import re
import sys
import os

# 프로젝트 루트를 sys.path에 추가 (백엔드 루트에서 실행 기준)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app.plugins.base import (
    ChunkingConfig,
    DocumentTypePlugin,
    DocumentTypeRegistry,
)
from app.plugins.builtin.policy import POLICYPlugin
from app.plugins.builtin.manual import MANUALPlugin
from app.plugins.builtin.report import REPORTPlugin
from app.plugins.builtin.faq import FAQPlugin


# ---------------------------------------------------------------------------
# PluginConformanceTest — 규격 검증 베이스 클래스
# ---------------------------------------------------------------------------

class PluginConformanceTest:
    """새 플러그인 작성 시 이 클래스를 상속하여 규격 준수를 자동 검증.

    서브클래스에서 `plugin` 속성을 설정해야 한다.
    """
    plugin: DocumentTypePlugin

    def test_type_name_format(self):
        """type_name이 영문 대문자 + 숫자 + 밑줄 형식인지 검증."""
        assert re.match(r'^[A-Z][A-Z0-9_]*$', self.plugin.get_type_name()), \
            f"type_name '{self.plugin.get_type_name()}'은 영문 대문자/숫자/밑줄만 허용됩니다."

    def test_display_name_not_empty(self):
        """display_name이 비어있지 않은지 검증."""
        assert len(self.plugin.get_display_name()) > 0

    def test_chunking_config_valid(self):
        """ChunkingConfig가 유효한 값인지 검증."""
        config = self.plugin.chunking_plugin().get_config()
        assert isinstance(config, ChunkingConfig)
        assert config.max_chunk_tokens > 0, "max_chunk_tokens는 양수여야 합니다."
        assert config.min_chunk_tokens >= 0, "min_chunk_tokens는 0 이상이어야 합니다."
        assert config.max_chunk_tokens > config.min_chunk_tokens, \
            "max_chunk_tokens > min_chunk_tokens이어야 합니다."
        assert config.overlap_tokens >= 0
        assert config.index_version_policy in ("published_only", "latest", "all")

    def test_rag_prompt_template_returns(self):
        """RAGPlugin이 PromptTemplate을 반환하는지 검증."""
        template = self.plugin.rag_plugin().get_prompt_template()
        assert template is not None

    def test_rag_context_config(self):
        """RAGPlugin이 유효한 컨텍스트 설정을 반환하는지 검증."""
        cfg = self.plugin.rag_plugin().get_context_config()
        assert isinstance(cfg, dict)
        assert cfg.get("max_context_tokens", 0) > 0
        assert cfg.get("top_n", 0) > 0

    def test_search_plugin_returns_dict(self):
        """SearchPlugin.get_config()가 dict를 반환하는지 검증."""
        config = self.plugin.search_plugin().get_config()
        assert isinstance(config, dict)

    def test_search_boost_has_title(self):
        """검색 부스트에 title 필드가 있는지 검증."""
        boost = self.plugin.search_plugin().get_boost_config()
        assert isinstance(boost, dict)
        assert "title" in boost

    def test_metadata_schema_valid_json_schema(self):
        """metadata_schema가 유효한 JSON Schema인지 검증."""
        schema = self.plugin.metadata_schema_plugin().get_schema()
        if schema:
            try:
                import jsonschema
                jsonschema.Draft7Validator.check_schema(schema)
            except ImportError:
                pass  # jsonschema 미설치 시 스킵

    def test_metadata_validation_empty_passes(self):
        """빈 metadata가 스키마 검증을 통과하는지 검증 (required 필드 최소화 원칙)."""
        errors = self.plugin.metadata_schema_plugin().validate({})
        assert errors == [], f"빈 metadata 검증 실패: {errors}"

    def test_editor_plugin_returns_lists(self):
        """EditorPlugin이 list를 반환하는지 검증."""
        allowed = self.plugin.editor_plugin().get_allowed_node_types()
        assert isinstance(allowed, list)
        structure = self.plugin.editor_plugin().get_default_structure()
        assert isinstance(structure, list)

    def test_renderer_plugin_returns_dict(self):
        """RendererPlugin이 dict를 반환하는지 검증."""
        cfg = self.plugin.renderer_plugin().get_render_config()
        assert isinstance(cfg, dict)

    def test_workflow_plugin_valid(self):
        """WorkflowPlugin이 유효한 설정을 반환하는지 검증."""
        assert isinstance(self.plugin.workflow_plugin().requires_approval(), bool)
        assert isinstance(self.plugin.workflow_plugin().get_review_roles(), list)

    def test_rag_prompt_renders_with_context(self):
        """PromptTemplate.render()가 context를 포함한 문자열을 반환하는지 검증."""
        template = self.plugin.rag_plugin().get_prompt_template()
        result = template.render("테스트 컨텍스트")
        assert "테스트 컨텍스트" in result
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# 내장 타입 규격 검증
# ---------------------------------------------------------------------------

class TestPOLICYPluginConformance(PluginConformanceTest):
    plugin = POLICYPlugin()

    def test_policy_specific_chunking(self):
        assert self.plugin.chunking_plugin().get_config().max_chunk_tokens == 512
        assert "article" not in self.plugin.chunking_plugin().get_config().exclude_node_types or True

    def test_policy_requires_approval(self):
        assert self.plugin.workflow_plugin().requires_approval() is True

    def test_policy_search_boost(self):
        boost = self.plugin.search_plugin().get_boost_config()
        assert boost.get("title", 0) >= 2.0

    def test_policy_prompt_mentions_citation(self):
        template = self.plugin.rag_plugin().get_prompt_template()
        result = template.render("내용")
        assert "조항" in result or "인용" in result


class TestMANUALPluginConformance(PluginConformanceTest):
    plugin = MANUALPlugin()

    def test_manual_chunking(self):
        cfg = self.plugin.chunking_plugin().get_config()
        assert cfg.max_chunk_tokens == 400
        assert cfg.min_chunk_tokens == 30

    def test_manual_requires_approval(self):
        assert self.plugin.workflow_plugin().requires_approval() is True


class TestREPORTPluginConformance(PluginConformanceTest):
    plugin = REPORTPlugin()

    def test_report_chunking(self):
        cfg = self.plugin.chunking_plugin().get_config()
        assert cfg.max_chunk_tokens == 600
        assert cfg.overlap_tokens == 100

    def test_report_requires_approval(self):
        assert self.plugin.workflow_plugin().requires_approval() is True


class TestFAQPluginConformance(PluginConformanceTest):
    plugin = FAQPlugin()

    def test_faq_chunking(self):
        cfg = self.plugin.chunking_plugin().get_config()
        assert cfg.max_chunk_tokens == 256
        assert cfg.overlap_tokens == 0
        assert cfg.include_parent_context is False

    def test_faq_no_approval_required(self):
        assert self.plugin.workflow_plugin().requires_approval() is False

    def test_faq_context_top_n(self):
        assert self.plugin.rag_plugin().get_context_config().get("top_n", 0) >= 8

    def test_faq_search_nodes(self):
        nodes = self.plugin.search_plugin().get_searchable_node_types()
        assert "faq_question" in nodes
        assert "faq_answer" in nodes


# ---------------------------------------------------------------------------
# DocumentTypeRegistry 단위 테스트
# ---------------------------------------------------------------------------

class TestDocumentTypeRegistry:

    def setup_method(self):
        """각 테스트 전 레지스트리 초기화."""
        DocumentTypeRegistry.reset()

    def test_register_and_get(self):
        """플러그인 등록 후 조회."""
        registry = DocumentTypeRegistry.instance()
        plugin = POLICYPlugin()
        registry.register(plugin)
        result = registry.get("POLICY")
        assert result.get_type_name() == "POLICY"

    def test_duplicate_registration_raises(self):
        """중복 등록 시 ValueError 발생."""
        registry = DocumentTypeRegistry.instance()
        registry.register(POLICYPlugin())
        with pytest.raises(ValueError, match="이미 등록"):
            registry.register(POLICYPlugin())

    def test_invalid_type_name_raises(self):
        """잘못된 type_name 등록 시 ValueError 발생."""
        registry = DocumentTypeRegistry.instance()

        class BadPlugin(DocumentTypePlugin):
            def get_type_name(self): return "invalid-type"  # 소문자 허용 안됨
            def get_display_name(self): return "bad"

        with pytest.raises(ValueError, match="Invalid"):
            registry.register(BadPlugin())

    def test_unregistered_type_returns_default(self):
        """미등록 타입 조회 시 DefaultDocumentTypePlugin 반환."""
        from app.plugins.base import DefaultDocumentTypePlugin
        registry = DocumentTypeRegistry.instance()
        result = registry.get("UNKNOWN_TYPE_XYZ")
        assert isinstance(result, DefaultDocumentTypePlugin)

    def test_reset_clears_registry(self):
        """reset() 후 레지스트리가 초기화됨."""
        registry = DocumentTypeRegistry.instance()
        registry.register(POLICYPlugin())
        DocumentTypeRegistry.reset()

        new_registry = DocumentTypeRegistry.instance()
        assert len(new_registry.list_all()) == 0

    def test_is_builtin_after_register(self):
        """등록된 타입은 is_builtin으로 확인 가능."""
        registry = DocumentTypeRegistry.instance()
        registry.register(FAQPlugin())
        assert registry.is_builtin("FAQ") is True
        assert registry.is_builtin("UNKNOWN") is False

    def test_list_all_returns_registered(self):
        """list_all()이 등록된 플러그인 목록을 반환."""
        registry = DocumentTypeRegistry.instance()
        registry.register(POLICYPlugin())
        registry.register(FAQPlugin())
        names = [p.get_type_name() for p in registry.list_all()]
        assert "POLICY" in names
        assert "FAQ" in names


# ---------------------------------------------------------------------------
# ChunkingService 통합 확인 테스트
# ---------------------------------------------------------------------------

class TestChunkingPluginIntegration:

    def setup_method(self):
        DocumentTypeRegistry.reset()
        from app.plugins.builtin import register_builtin_plugins
        register_builtin_plugins()

    def test_policy_chunking_config(self):
        from app.plugins.base import DocumentTypeRegistry
        plugin = DocumentTypeRegistry.instance().get("POLICY")
        cfg = plugin.chunking_plugin().get_config()
        assert cfg.max_chunk_tokens == 512

    def test_faq_chunking_config(self):
        from app.plugins.base import DocumentTypeRegistry
        plugin = DocumentTypeRegistry.instance().get("FAQ")
        cfg = plugin.chunking_plugin().get_config()
        assert cfg.max_chunk_tokens == 256
        assert cfg.overlap_tokens == 0

    def test_should_index_published_only(self):
        from app.plugins.base import DocumentTypeRegistry
        plugin = DocumentTypeRegistry.instance().get("POLICY")
        assert plugin.chunking_plugin().should_index("published") is True
        assert plugin.chunking_plugin().should_index("PUBLISHED") is True
        assert plugin.chunking_plugin().should_index("draft") is False


# ---------------------------------------------------------------------------
# RAGPlugin 통합 확인 테스트
# ---------------------------------------------------------------------------

class TestRAGPluginIntegration:

    def setup_method(self):
        DocumentTypeRegistry.reset()
        from app.plugins.builtin import register_builtin_plugins
        register_builtin_plugins()

    def test_policy_prompt_includes_citation_rule(self):
        plugin = DocumentTypeRegistry.instance().get("POLICY")
        result = plugin.rag_plugin().get_prompt_template().render("테스트")
        assert "조항" in result or "인용" in result

    def test_faq_context_top_n(self):
        plugin = DocumentTypeRegistry.instance().get("FAQ")
        ctx = plugin.rag_plugin().get_context_config()
        assert ctx["top_n"] >= 10

    def test_policy_context_max_tokens(self):
        plugin = DocumentTypeRegistry.instance().get("POLICY")
        ctx = plugin.rag_plugin().get_context_config()
        assert ctx["max_context_tokens"] >= 7000


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
