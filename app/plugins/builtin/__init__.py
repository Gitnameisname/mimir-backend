"""
내장 DocumentType 플러그인 패키지.

시스템 부팅 시 register_builtin_plugins()를 호출해 4개 타입을 자동 등록한다.
"""

from app.plugins.builtin.faq import FAQPlugin
from app.plugins.builtin.manual import MANUALPlugin
from app.plugins.builtin.policy import POLICYPlugin
from app.plugins.builtin.report import REPORTPlugin

BUILTIN_PLUGINS = [
    POLICYPlugin(),
    MANUALPlugin(),
    REPORTPlugin(),
    FAQPlugin(),
]


def register_builtin_plugins() -> None:
    """내장 플러그인 4개를 레지스트리에 등록한다. 시스템 부팅 시 1회 호출."""
    from app.plugins.base import DocumentTypeRegistry
    registry = DocumentTypeRegistry.instance()
    for plugin in BUILTIN_PLUGINS:
        try:
            registry.register(plugin)
        except ValueError:
            # 이미 등록된 경우(재시작 등) 무시
            pass


__all__ = [
    "POLICYPlugin",
    "MANUALPlugin",
    "REPORTPlugin",
    "FAQPlugin",
    "BUILTIN_PLUGINS",
    "register_builtin_plugins",
]
