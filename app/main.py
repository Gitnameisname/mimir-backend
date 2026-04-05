import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.context.middleware import RequestContextMiddleware
from app.api.errors.handlers import register_exception_handlers
from app.api.router import api_router
from app.config import settings

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Mimir Platform API",
        description="범용 문서/지식 플랫폼 API",
        version=settings.api_version,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

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
