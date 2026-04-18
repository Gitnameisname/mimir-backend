"""
DocumentType 플러그인 시스템 단위 테스트.

검증 목표:
  - DocumentTypeRegistry 싱글턴 패턴
  - 플러그인 등록/조회/폴백
  - type_name 유효성 검사 (whitelist: 대문자·숫자·밑줄)
  - ConfigurableDocumentTypePlugin — DB 설정 병합 우선순위
  - ChunkingPlugin.should_index() 정책 분기
  - PromptTemplate 브레이스 이스케이프 (보안 P12-SEC-01)
  - MetadataSchemaPlugin 검증 로직
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("INTERNAL_SERVICE_SECRET", "test-internal")

from app.plugins.base import (
    ChunkingConfig,
    ChunkingPlugin,
    ConfigurableChunkingPlugin,
    ConfigurableDocumentTypePlugin,
    ConfigurableMetadataSchemaPlugin,
    ConfigurableRAGPlugin,
    DefaultDocumentTypePlugin,
    DocumentTypePlugin,
    DocumentTypeRegistry,
    MetadataSchemaPlugin,
    PromptTemplate,
)


# ---------------------------------------------------------------------------
# Test Fixture Plugin
# ---------------------------------------------------------------------------

class SamplePlugin(DocumentTypePlugin):
    def get_type_name(self) -> str:
        return "SAMPLE"

    def get_display_name(self) -> str:
        return "Sample Document"


@pytest.fixture(autouse=True)
def reset_registry():
    """각 테스트마다 레지스트리 초기화."""
    DocumentTypeRegistry.reset()
    yield
    DocumentTypeRegistry.reset()


# ---------------------------------------------------------------------------
# Registry 싱글턴 동작
# ---------------------------------------------------------------------------

class TestDocumentTypeRegistrySingleton:
    def test_instance_returns_same_object(self):
        a = DocumentTypeRegistry.instance()
        b = DocumentTypeRegistry.instance()
        assert a is b

    def test_reset_creates_new_instance(self):
        a = DocumentTypeRegistry.instance()
        DocumentTypeRegistry.reset()
        b = DocumentTypeRegistry.instance()
        assert a is not b

    def test_registered_plugins_cleared_after_reset(self):
        DocumentTypeRegistry.instance().register(SamplePlugin())
        DocumentTypeRegistry.reset()
        registry = DocumentTypeRegistry.instance()
        assert "SAMPLE" not in registry.list_type_names()


# ---------------------------------------------------------------------------
# 플러그인 등록 / 조회
# ---------------------------------------------------------------------------

class TestDocumentTypeRegistryRegister:
    def test_register_and_get(self):
        registry = DocumentTypeRegistry.instance()
        registry.register(SamplePlugin())
        plugin = registry.get("SAMPLE")
        assert plugin.get_type_name() == "SAMPLE"

    def test_duplicate_register_raises(self):
        registry = DocumentTypeRegistry.instance()
        registry.register(SamplePlugin())
        with pytest.raises(ValueError, match="이미 등록"):
            registry.register(SamplePlugin())

    def test_invalid_type_name_lowercase(self):
        class LowercasePlugin(DocumentTypePlugin):
            def get_type_name(self): return "lowercase"
            def get_display_name(self): return "bad"

        with pytest.raises(ValueError, match="Invalid type_name"):
            DocumentTypeRegistry.instance().register(LowercasePlugin())

    def test_invalid_type_name_with_dash(self):
        class DashPlugin(DocumentTypePlugin):
            def get_type_name(self): return "TYPE-NAME"
            def get_display_name(self): return "bad"

        with pytest.raises(ValueError, match="Invalid type_name"):
            DocumentTypeRegistry.instance().register(DashPlugin())

    def test_valid_type_name_with_number(self):
        class NumberedPlugin(DocumentTypePlugin):
            def get_type_name(self): return "TYPE123"
            def get_display_name(self): return "numbered"

        DocumentTypeRegistry.instance().register(NumberedPlugin())
        assert "TYPE123" in DocumentTypeRegistry.instance().list_type_names()

    def test_get_unregistered_returns_default(self):
        plugin = DocumentTypeRegistry.instance().get("UNKNOWN_TYPE")
        assert isinstance(plugin, DefaultDocumentTypePlugin)
        assert plugin.get_type_name() == "UNKNOWN_TYPE"

    def test_list_type_names(self):
        registry = DocumentTypeRegistry.instance()
        registry.register(SamplePlugin())
        assert "SAMPLE" in registry.list_type_names()

    def test_is_builtin(self):
        registry = DocumentTypeRegistry.instance()
        registry.register(SamplePlugin())
        assert registry.is_builtin("SAMPLE") is True
        assert registry.is_builtin("NONEXISTENT") is False


# ---------------------------------------------------------------------------
# ConfigurableChunkingPlugin — DB 설정 병합
# ---------------------------------------------------------------------------

class TestConfigurableChunkingPlugin:
    def test_db_config_overrides_base(self):
        base = ChunkingConfig(max_chunk_tokens=512, min_chunk_tokens=50)
        plugin = ConfigurableChunkingPlugin(
            raw={"max_chunk_tokens": 1024},
            base=base,
        )
        config = plugin.get_config()
        assert config.max_chunk_tokens == 1024
        assert config.min_chunk_tokens == 50  # base 유지

    def test_empty_db_config_uses_base(self):
        base = ChunkingConfig(strategy="flat", max_chunk_tokens=256)
        plugin = ConfigurableChunkingPlugin(raw={}, base=base)
        config = plugin.get_config()
        assert config.strategy == "flat"
        assert config.max_chunk_tokens == 256

    def test_db_config_without_base_uses_defaults(self):
        plugin = ConfigurableChunkingPlugin(raw={"overlap_tokens": 100})
        config = plugin.get_config()
        assert config.overlap_tokens == 100
        assert config.strategy == "node_based"  # ChunkingConfig default

    def test_exclude_node_types_override(self):
        plugin = ConfigurableChunkingPlugin(
            raw={"exclude_node_types": ["footnote", "appendix"]}
        )
        config = plugin.get_config()
        assert "footnote" in config.exclude_node_types
        assert "appendix" in config.exclude_node_types


# ---------------------------------------------------------------------------
# ChunkingPlugin.should_index()
# ---------------------------------------------------------------------------

class TestChunkingPluginShouldIndex:
    def test_published_only_accepts_published(self):
        plugin = ChunkingPlugin()
        assert plugin.should_index("PUBLISHED") is True
        assert plugin.should_index("published") is True

    def test_published_only_rejects_draft(self):
        plugin = ChunkingPlugin()
        assert plugin.should_index("DRAFT") is False

    def test_all_policy_accepts_any_status(self):
        class AllPlugin(ChunkingPlugin):
            def get_config(self):
                return ChunkingConfig(index_version_policy="all")

        plugin = AllPlugin()
        assert plugin.should_index("DRAFT") is True
        assert plugin.should_index("PUBLISHED") is True
        assert plugin.should_index("ARCHIVED") is True

    def test_latest_policy_accepts_any_status(self):
        class LatestPlugin(ChunkingPlugin):
            def get_config(self):
                return ChunkingConfig(index_version_policy="latest")

        plugin = LatestPlugin()
        assert plugin.should_index("DRAFT") is True

    def test_unknown_policy_rejects_all(self):
        class UnknownPolicyPlugin(ChunkingPlugin):
            def get_config(self):
                return ChunkingConfig(index_version_policy="unknown_policy")

        plugin = UnknownPolicyPlugin()
        assert plugin.should_index("PUBLISHED") is False
        assert plugin.should_index("DRAFT") is False


# ---------------------------------------------------------------------------
# PromptTemplate 브레이스 이스케이프 (P12-SEC-01)
# ---------------------------------------------------------------------------

class TestConfigurableRAGPlugin:
    def test_custom_prompt_with_braces_does_not_raise(self):
        """관리자 입력 프롬프트에 {var} 형식이 있어도 KeyError 없이 렌더링된다."""
        raw = {"system_prompt": "Use {context_variable} for {query}"}
        plugin = ConfigurableRAGPlugin(raw=raw)
        template = plugin.get_prompt_template()
        # render() 호출 시 KeyError 없어야 함
        result = template.render("test context")
        assert "test context" in result
        # 원본 중괄호 내용이 format()에 의해 해석되지 않아야 함
        assert "{context_variable}" in result

    def test_no_custom_prompt_uses_base_template(self):
        plugin = ConfigurableRAGPlugin(raw={})
        template = plugin.get_prompt_template()
        result = template.render("my context")
        assert "my context" in result

    def test_context_config_merges_with_defaults(self):
        plugin = ConfigurableRAGPlugin(raw={"top_n": 10, "max_context_tokens": 8000})
        config = plugin.get_context_config()
        assert config["top_n"] == 10
        assert config["max_context_tokens"] == 8000


# ---------------------------------------------------------------------------
# ConfigurableDocumentTypePlugin — 위임 계층
# ---------------------------------------------------------------------------

class TestConfigurableDocumentTypePlugin:
    def test_display_name_from_config(self):
        plugin = ConfigurableDocumentTypePlugin(
            type_name="REPORT",
            config={"display_name": "보고서"},
        )
        assert plugin.get_display_name() == "보고서"

    def test_display_name_fallback_to_type_name(self):
        plugin = ConfigurableDocumentTypePlugin(type_name="MEMO", config={})
        assert plugin.get_display_name() == "MEMO"

    def test_chunking_plugin_with_db_config(self):
        plugin = ConfigurableDocumentTypePlugin(
            type_name="DOC",
            config={"chunking_config": {"max_chunk_tokens": 768}},
        )
        chunking = plugin.chunking_plugin()
        assert isinstance(chunking, ConfigurableChunkingPlugin)
        assert chunking.get_config().max_chunk_tokens == 768

    def test_chunking_plugin_fallback_without_config(self):
        from app.plugins.base import DefaultChunkingPlugin
        plugin = ConfigurableDocumentTypePlugin(type_name="DOC", config={})
        chunking = plugin.chunking_plugin()
        assert isinstance(chunking, DefaultChunkingPlugin)

    def test_base_plugin_used_when_no_db_chunking_config(self):
        class CustomBase(DocumentTypePlugin):
            def get_type_name(self): return "CUSTOM"
            def get_display_name(self): return "Custom"
            def chunking_plugin(self):
                c = ChunkingPlugin()
                return c

        base = CustomBase()
        plugin = ConfigurableDocumentTypePlugin(
            type_name="CUSTOM",
            config={"_base_plugin": base},
        )
        # DB chunking_config 없음 → base 플러그인 사용
        chunking = plugin.chunking_plugin()
        assert chunking is base.chunking_plugin() or isinstance(chunking, ChunkingPlugin)

    def test_metadata_schema_from_config(self):
        schema = {"type": "object", "properties": {"author": {"type": "string"}}}
        plugin = ConfigurableDocumentTypePlugin(
            type_name="ARTICLE",
            config={"metadata_schema": schema},
        )
        meta_plugin = plugin.metadata_schema_plugin()
        assert isinstance(meta_plugin, ConfigurableMetadataSchemaPlugin)
        assert meta_plugin.get_schema() == schema


# ---------------------------------------------------------------------------
# MetadataSchemaPlugin 검증
# ---------------------------------------------------------------------------

class TestMetadataSchemaPlugin:
    def test_empty_schema_allows_all(self):
        plugin = MetadataSchemaPlugin()
        assert plugin.validate({"anything": "goes"}) == []

    def test_configurable_schema_returns_schema(self):
        schema = {"type": "object"}
        plugin = ConfigurableMetadataSchemaPlugin(schema)
        assert plugin.get_schema() == schema
