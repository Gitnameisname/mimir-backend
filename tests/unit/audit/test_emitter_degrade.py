"""AuditEmitter 폐쇄망 degrade 회귀 가드 (R-D1, 2026-04-25).

검증 대상:
    - DB INSERT 실패 시 emit() 이 예외를 던지지 않고 logging fallback 으로 동작.
    - 외부 sink 0 건 정책 — emitter 가 HTTP/SaaS/Slack 호출을 하지 않음 (정적 검증).
    - 폐쇄망 환경에서도 핵심 emit 시퀀스가 작동.

도서관 §1.7-extension (R9) 의 폐쇄망 정책 명문화에 대한 회귀 가드.
"""
from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from app.audit.emitter import AuditEmitter, _audit_logger


class TestDbFailureFallback:
    """DB INSERT 실패가 emit() 을 깨뜨리지 않음을 검증."""

    def test_db_failure_does_not_raise(self, caplog):
        """DB 연결 실패 → emit() 정상 반환 + error 로그."""
        emitter = AuditEmitter()

        # get_db 가 예외 던지도록 mock
        def _broken_get_db():
            raise ConnectionError("simulated DB outage")

        with patch("app.db.connection.get_db", side_effect=_broken_get_db):
            with caplog.at_level(logging.ERROR, logger="mimir.audit"):
                # 예외 던지면 안 됨
                emitter.emit(
                    event_type="test.event",
                    action="test.action",
                    actor_id="u1",
                    actor_type="user",
                    resource_type="document",
                    result="success",
                )
        # error 로그가 남았는지 확인
        assert any(
            "audit_persist_failed" in rec.message for rec in caplog.records
        ), "DB 실패 시 error 로그 미기록"

    def test_db_failure_still_emits_structured_log(self, caplog):
        """DB 실패해도 structured log (1차 채널) 는 작동."""
        emitter = AuditEmitter()

        def _broken_get_db():
            raise ConnectionError("simulated")

        with patch("app.db.connection.get_db", side_effect=_broken_get_db):
            with caplog.at_level(logging.INFO, logger="mimir.audit"):
                emitter.emit(
                    event_type="test.success_path",
                    action="test.action",
                    actor_id="u1",
                    actor_type="user",
                    resource_type="document",
                    result="success",
                )
        # AUDIT 구조화 로그 + audit_persist_failed 두 라인 모두 있어야 함
        assert any(
            "test.success_path" in rec.message for rec in caplog.records
        ), "structured AUDIT 로그 미기록"


class TestExternalSinkPolicy:
    """외부 sink 0 정책 — emitter 가 HTTP / Slack / Email 호출을 하지 않음을 정적 검증."""

    def test_no_external_http_imports(self):
        """emitter.py 가 외부 HTTP 클라이언트 import 안 함."""
        from pathlib import Path

        emitter_path = Path(__file__).parent.parent.parent.parent / "app/audit/emitter.py"
        src = emitter_path.read_text(encoding="utf-8")
        forbidden = ["import requests", "import httpx", "import urllib.request",
                     "import smtplib", "import slack_sdk"]
        for pattern in forbidden:
            assert pattern not in src, (
                f"emitter.py 가 {pattern!r} 를 import 함 — 새 외부 sink 추가 시 "
                f"폐쇄망 정책 (모듈 docstring) 의 환경변수 가드 + FileSink fallback "
                f"+ 회귀 테스트 의무를 충족했는지 확인"
            )

    def test_no_external_socket_calls(self):
        """emitter.py 가 socket / urllib / 외부 HTTP 클라이언트 호출 안 함.

        주의: docstring/주석 안의 예시 텍스트 (`PagerDuty webhook` 등) 는 정상이며
        false positive 회피를 위해 ast 로 토큰화한 후 실제 호출/import 패턴만 검사.
        """
        import ast
        from pathlib import Path

        emitter_path = Path(__file__).parent.parent.parent.parent / "app/audit/emitter.py"
        src = emitter_path.read_text(encoding="utf-8")
        tree = ast.parse(src)

        # 1. import 검사 (정확한 모듈명)
        forbidden_imports = {
            "requests", "httpx", "urllib3", "smtplib", "slack_sdk",
            "pagerduty", "webhook", "aiohttp",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name not in forbidden_imports, (
                        f"emitter.py 가 외부 HTTP/SaaS 모듈 {alias.name!r} 를 import — "
                        "폐쇄망 정책 위반. docstring 의 환경변수 가드 + FileSink fallback 의무 확인."
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.split(".")[0] in forbidden_imports:
                    raise AssertionError(
                        f"emitter.py 가 {node.module!r} 에서 import — "
                        "폐쇄망 정책 위반. docstring 정책 확인."
                    )

        # 2. 호출 검사 (urllib.request.urlopen 같은 직접 네트워크 호출)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                # 예: urllib.request.urlopen(...) / socket.create_connection(...)
                attr_chain = []
                cur = node.func
                while isinstance(cur, ast.Attribute):
                    attr_chain.insert(0, cur.attr)
                    cur = cur.value
                if isinstance(cur, ast.Name):
                    attr_chain.insert(0, cur.id)
                full = ".".join(attr_chain)
                forbidden_calls = {"urlopen", "socket.create_connection",
                                   "urllib.request.urlopen"}
                for fc in forbidden_calls:
                    assert fc not in full, (
                        f"emitter.py 가 외부 네트워크 호출 {full!r} 사용 — "
                        "폐쇄망 정책 위반."
                    )


class TestSuccessPath:
    """폐쇄망에서도 정상 동작하는 happy path 회귀 가드."""

    def test_emit_in_disconnected_environment_works(self, caplog):
        """외부 sink 0 → 폐쇄망에서 emit 이 그대로 작동 (DB 만 사용)."""
        emitter = AuditEmitter()
        # _persist 무력화 (DB 의존성 없이 logger 만 검증)
        with patch.object(emitter, "_persist", lambda **kw: None):
            with caplog.at_level(logging.INFO, logger="mimir.audit"):
                emitter.emit(
                    event_type="closed_network.test",
                    action="test.action",
                    actor_id="u1",
                    actor_type="user",
                    resource_type="document",
                    result="success",
                )
        assert any("closed_network.test" in rec.message for rec in caplog.records)
