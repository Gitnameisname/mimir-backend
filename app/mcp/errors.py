"""
MCP 오류 코드 정의 — Phase 4 FG4.3.

MCP 2025-11-25 스펙 호환 오류 응답 구조.
"""
from __future__ import annotations

from enum import Enum


class MCPErrorCode(str, Enum):
    UNAUTHORIZED = "UNAUTHORIZED"
    NOT_FOUND = "NOT_FOUND"
    INVALID_SCOPE = "INVALID_SCOPE"
    INVALID_CITATION = "INVALID_CITATION"
    RATE_LIMIT = "RATE_LIMIT"
    INVALID_REQUEST = "INVALID_REQUEST"
    AGENT_DISABLED = "AGENT_DISABLED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class MCPError(Exception):
    """MCP 오류 — HTTP 레이어에서 표준 envelope으로 변환된다."""

    def __init__(self, code: MCPErrorCode, message: str, http_status: int = 400) -> None:
        self.code = code
        self.message = message
        self.http_status = http_status
        super().__init__(message)


# 자주 쓰이는 팩토리
def unauthorized(msg: str = "인증이 필요합니다.") -> MCPError:
    return MCPError(MCPErrorCode.UNAUTHORIZED, msg, 401)


def forbidden(msg: str = "권한이 없습니다.") -> MCPError:
    return MCPError(MCPErrorCode.UNAUTHORIZED, msg, 403)


def not_found(msg: str = "리소스를 찾을 수 없습니다.") -> MCPError:
    return MCPError(MCPErrorCode.NOT_FOUND, msg, 404)


def invalid_scope(msg: str = "Scope 지정이 올바르지 않습니다.") -> MCPError:
    return MCPError(MCPErrorCode.INVALID_SCOPE, msg, 400)


def invalid_citation(msg: str = "Citation 검증에 실패했습니다.") -> MCPError:
    return MCPError(MCPErrorCode.INVALID_CITATION, msg, 400)


def agent_disabled(msg: str = "에이전트가 비활성화(킬스위치) 상태입니다.") -> MCPError:
    return MCPError(MCPErrorCode.AGENT_DISABLED, msg, 403)
