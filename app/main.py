import logging

from app.observability.log_config import configure_logging

# Phase 13-2: 애플리케이션 시작 시 구조화 JSON 로깅 초기화
configure_logging()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api.context.middleware import RequestContextMiddleware
from app.api.errors.handlers import register_exception_handlers
from app.api.rate_limit import limiter
from app.api.router import api_router
from app.api.security.headers import SecurityHeadersMiddleware
from app.api.security.input_validation import RequestSizeLimitMiddleware
from app.observability.metrics import PrometheusMiddleware
from app.config import settings

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    # VULN-015: production에서 /docs, /redoc 비활성화
    is_production = settings.environment == "production"
    app = FastAPI(
        title="Mimir Platform API",
        description="범용 문서/지식 플랫폼 API",
        version=settings.api_version,
        docs_url=None if is_production else "/docs",
        redoc_url=None if is_production else "/redoc",
        openapi_url=None if is_production else "/openapi.json",
    )

    # CORS — VULN-012/013: 와일드카드 제거, 명시적 method/header 목록 지정
    _ALLOWED_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    _ALLOWED_HEADERS = [
        "Authorization", "Content-Type", "X-Request-Id", "X-Trace-Id",
        "X-Actor-Id", "X-Actor-Role", "X-Service-Token", "X-API-Key",
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=_ALLOWED_METHODS,
        allow_headers=_ALLOWED_HEADERS,
    )

    # Phase 13-3: Prometheus 메트릭 수집 미들웨어
    app.add_middleware(PrometheusMiddleware)

    # Phase 13-1: 보안 헤더 미들웨어 (OWASP Top 10 A05/A07 대응)
    app.add_middleware(SecurityHeadersMiddleware)

    # Phase 13-1: 요청 크기 제한 (OWASP A05 - 10 MB)
    app.add_middleware(RequestSizeLimitMiddleware)

    # Rate limiter 상태 주입 및 429 핸들러 등록
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # RequestContext 미들웨어 (Task I-4/I-5)
    # request_id / trace_id 생성 및 request.state.context 초기화
    # actor는 anonymous로 초기화되며, resolve_current_actor dependency가 갱신한다
    app.add_middleware(RequestContextMiddleware)

    # 공통 exception handler 등록 (Task I-3)
    # RequestValidationError / HTTPException / ApiError / Exception 모두 처리
    register_exception_handlers(app)

    # API router 등록: /api prefix
    app.include_router(api_router, prefix="/api")

    # Phase 4 (S2): .well-known/mimir-mcp 메타데이터 엔드포인트
    from fastapi.responses import JSONResponse
    from app.schemas.mcp import TOOL_SCHEMAS, MIMIR_EXTENSIONS

    @app.get("/.well-known/mimir-mcp", include_in_schema=False)
    def well_known_mcp():
        return JSONResponse({
            "mcp_version": "2025-11-25",
            "server_id": "mimir-s2",
            "capabilities": {
                "tools": [t["name"] for t in TOOL_SCHEMAS],
                "resources": True,
                "prompts": True,
                "tasks": False,
            },
            "authentication": "oauth2_client_credentials",
            "scope_profile_required": True,
            "extensions": MIMIR_EXTENSIONS,
            "documentation_url": "/docs",
        })

    # DB 초기화: documents 테이블 생성 (idempotent)
    @app.on_event("startup")
    def on_startup() -> None:
        from app.db import get_db, init_db
        try:
            init_db()
        except Exception as exc:
            logger.warning("DB init skipped (connection unavailable): %s", exc)

        # RAG-007: RAG 테이블 초기화를 startup에서 1회만 실행 (라우터 레이어 제거)
        try:
            from app.repositories.rag_repository import ensure_tables
            with get_db() as conn:
                ensure_tables(conn)
                conn.commit()
        except Exception as exc:
            logger.warning("RAG tables init skipped (connection unavailable): %s", exc)

        # Phase 12: 내장 DocumentType 플러그인 자동 등록
        try:
            from app.plugins.builtin import register_builtin_plugins
            register_builtin_plugins()
            logger.info("DocumentType 내장 플러그인 등록 완료 (POLICY, MANUAL, REPORT, FAQ)")
        except Exception as exc:
            logger.warning("DocumentType 플러그인 등록 실패: %s", exc)

        # Phase 3 (S2): 대화 자동 만료 배치 스케줄러 시작
        try:
            from app.scheduler import start_scheduler
            start_scheduler()
        except Exception as exc:
            logger.warning("BatchScheduler 시작 실패 (서비스 계속): %s", exc)

    @app.on_event("shutdown")
    def on_shutdown() -> None:
        try:
            from app.scheduler import stop_scheduler
            stop_scheduler()
        except Exception as exc:
            logger.warning("BatchScheduler 종료 실패: %s", exc)

    return app


app = create_app()
