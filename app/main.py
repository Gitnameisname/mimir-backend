import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api.context.middleware import RequestContextMiddleware
from app.api.errors.handlers import register_exception_handlers
from app.api.rate_limit import limiter
from app.api.router import api_router
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

    # DB 초기화: documents 테이블 생성 (idempotent)
    @app.on_event("startup")
    def on_startup() -> None:
        from app.db import init_db
        try:
            init_db()
        except Exception as exc:
            logger.warning("DB init skipped (connection unavailable): %s", exc)

    return app


app = create_app()
