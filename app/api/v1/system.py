"""
System router — /api/v1/system

운영성 endpoint를 제공한다.
  - GET /api/v1/system/health        : 헬스체크 (공개, Tier 1)
  - GET /api/v1/system/info         : 서비스 메타 정보 (공개)
  - GET /api/v1/system/capabilities : 기능 가용성 조회 (인증 필요, Tier 2 — Task 0-8 3-tier 분리)
  - GET /api/v1/system/metrics      : Prometheus 메트릭 (Phase 13-3)
  - GET /api/v1/system/error-test   : 공통 오류 처리 검증용 stub (Task I-3)

보안 분리 (Task 0-8 패치):
  - Tier 1 (health): 인증 불필요. status + version만 노출
  - Tier 2 (capabilities): 인증 필요. rag_available, chunking_enabled만 노출
    → pgvector_enabled, supported_providers 등 내부 구성 정보는 Tier 3 (admin)으로 분리
  - Tier 3 (admin capabilities): Admin 전용. admin.py에서 제공
"""
import logging
import os
import threading
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from app.api.auth import ResourceRef, authorization_service, resolve_current_actor
from app.api.auth.models import ActorContext
from app.api.errors import (
    ApiAuthenticationError,
    ApiConflictError,
    ApiNotFoundError,
    ApiPermissionDeniedError,
    ApiValidationError,
)
from app.api.responses import SuccessResponse, success_response
from app.config import settings
from app.observability.metrics import generate_metrics_text
from app.utils.strings import normalize_lower
from app.utils.time import utcnow

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# capabilities 캐시 (5분 TTL, thread-safe)
# ---------------------------------------------------------------------------

_CAP_CACHE_TTL = timedelta(seconds=30)
_cap_cache: dict = {"data": None, "expires": datetime.min.replace(tzinfo=timezone.utc)}
_cap_lock = threading.Lock()


def _detect_pgvector() -> bool:
    """pgvector 확장 설치 여부를 감지한다.

    우선순위:
      1. PGVECTOR_ENABLED 환경변수 (명시적 override — 테스트 환경 포함)
      2. DB pg_extension 직접 조회 (런타임 자동 감지)
      3. 기본값 False (DB 연결 불가 시 안전한 폴백)
    """
    # 도서관 §1.4 BE-G1 (2026-04-25): normalize_lower (None 이 안 옴 → str)
    env_val = normalize_lower(os.environ.get("PGVECTOR_ENABLED", "")) or ""
    if env_val in ("true", "1", "yes"):
        return True
    if env_val in ("false", "0", "no"):
        return False
    # 환경변수 미설정 → DB 직접 확인
    try:
        from app.db.connection import get_db
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname='vector') AS exists"
                )
                row = cur.fetchone()
                return bool(row["exists"])
    except Exception as exc:
        logger.debug("pgvector DB 감지 실패 (기본값 False): %s", exc)
        return False


def _build_capabilities() -> dict:
    """capabilities 응답 dict를 빌드한다."""
    # ── DB에서 llm_providers 조회 ──
    llm_total = 0
    llm_active = 0
    default_llm_model: str | None = None
    default_embed_model: str | None = None
    try:
        from app.db.connection import get_db
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT type, model_name, status, is_default FROM llm_providers"
                )
                rows = cur.fetchall()
                for r in rows:
                    if r["type"] == "llm":
                        llm_total += 1
                        if r["status"] == "active":
                            llm_active += 1
                        if r["is_default"] and not default_llm_model:
                            default_llm_model = r["model_name"]
                    elif r["type"] == "embedding":
                        if r["is_default"] and not default_embed_model:
                            default_embed_model = r["model_name"]
    except Exception as exc:
        logger.debug("llm_providers 조회 실패: %s", exc)

    # ── Milvus 연결 확인 ──
    milvus_ok = False
    try:
        from app.db.milvus import get_milvus
        client = get_milvus()
        milvus_ok = client.is_available()
    except Exception:
        pass

    # ── FTS는 PostgreSQL 기반으로 항상 활성 ──
    fts_enabled = True

    # ── pgvector 확장 감지 (PGVECTOR_ENABLED env > pg_extension 조회) ──
    pgvector_enabled = _detect_pgvector()

    # ── RAG = pgvector + default LLM (embedding 모델은 settings 폴백 허용) ──
    # chunking_enabled 은 pgvector 기반이므로 pgvector 가 off 이면 chunking 도 off.
    has_llm = (default_llm_model is not None) or bool(settings.openai_api_key or settings.anthropic_api_key)
    has_embed = default_embed_model is not None
    rag_available = pgvector_enabled and has_llm

    # ── 저하 원인 수집 ──
    degraded_reasons: list[str] = []
    if not milvus_ok:
        degraded_reasons.append("벡터 스토어(Milvus)에 연결할 수 없습니다")
    if not has_llm:
        degraded_reasons.append("활성화된 기본 LLM 프로바이더가 없습니다")
    if not has_embed:
        degraded_reasons.append("기본 임베딩 프로바이더가 설정되지 않았습니다")

    providers: list[str] = []
    if settings.openai_api_key:
        providers.append("openai")
    if settings.anthropic_api_key:
        providers.append("anthropic")

    return {
        "version": settings.api_version,
        "pgvector_enabled": pgvector_enabled,
        "rag_available": rag_available,
        "chunking_enabled": pgvector_enabled,
        "supported_providers": providers,
        "mcp_spec_version": None,
        # 프론트엔드 SystemCapabilities 필드
        "embedding_model": default_embed_model,
        "llm_providers_count": llm_total,
        "active_llm_providers": llm_active,
        "vector_store": "milvus" if milvus_ok else None,
        "fts_enabled": fts_enabled,
        "degraded": len(degraded_reasons) > 0,
        "degraded_reasons": degraded_reasons,
    }


@router.get(
    "/health",
    summary="헬스체크",
    description=(
        "서비스 가용 여부를 확인한다.\n\n"
        "**서브체크 (S3 P0 FG 0-2)**: `embedding_dim` 블록은 `EMBEDDING_DIM` 설정값과 "
        "`document_chunks.embedding` 컬럼 차원(pgvector) / Milvus collection 차원의 일치 여부를 포함한다.\n\n"
        "- `embedding_dim.match = true` : config·DB 모두 일치\n"
        "- `embedding_dim.match = false`: 불일치 (degraded 원인)\n"
        "- `embedding_dim.match = null` : 컬럼 부재 (현재 Milvus 중심 아키텍처 정합)\n\n"
        "불일치 감지 시 전체 `healthy = false`, `degraded = true` 로 반환된다 (BUG-04 재발 감지)."
    ),
    response_model=SuccessResponse,
)
def health_check() -> SuccessResponse:
    """Tier 1 헬스체크 — DB 도달 가능 시 embedding_dim 서브체크 포함.

    DB 도달 불가 시에도 본 엔드포인트는 200 을 반환한다 (앱 프로세스 자체 생존 확인용).
    embedding_dim 세부는 best-effort 로 채워지며 서비스 실패로 연결되지 않는다.
    """
    # S3 P0 FG 0-2: embedding_dim 서브체크 (best-effort)
    subcheck: dict | None = None
    healthy = True
    degraded = False

    try:
        from app.db import get_db  # noqa: WPS433
        from app.db import embedding_dim_check as _dim_check_mod  # noqa: WPS433

        # healthcheck 는 빠르게 반환되어야 하므로 짧은 read-only 트랜잭션.
        # 모듈 속성 접근으로 호출 — pytest monkeypatch 로 `check_embedding_dim` 대체 가능.
        with get_db() as conn:
            conn.rollback()  # 트랜잭션 상태 정리 — read-only
            result = _dim_check_mod.check_embedding_dim(conn, check_milvus=True)
            subcheck = result.to_dict()
            if not result.ok:
                healthy = False
                degraded = True
            # Milvus 불일치도 degraded 원인
            if result.milvus_match is False:
                degraded = True
                healthy = False
    except Exception as exc:  # pragma: no cover - DB down
        # DB 접속 실패는 health 에 영향을 주지만, embedding_dim 서브체크 자체 실패는 degrade 로만.
        logger.warning("embedding_dim 서브체크 실패 (무시): %s", exc)
        subcheck = {"error": str(exc), "match": None}

    data: dict = {"healthy": healthy}
    if degraded:
        data["degraded"] = True
    if subcheck is not None:
        data["embedding_dim"] = subcheck

    return success_response(data=data)


@router.get(
    "/info",
    summary="서비스 메타 정보",
    description="서비스 이름, API 버전, 실행 환경 등 기본 메타 정보를 반환한다.",
    response_model=SuccessResponse,
)
def service_info() -> SuccessResponse:
    return success_response(
        data={
            "service": settings.service_name,
            "api_version": settings.api_version,
            "environment": settings.environment,
        }
    )


def _get_full_capabilities() -> dict:
    """캐시를 경유하여 전체 capabilities dict를 반환한다.

    Tier 2, Tier 3 모두 이 함수를 거쳐 캐시된 데이터를 사용한다.
    """
    now = utcnow()
    with _cap_lock:
        if _cap_cache["data"] is None or now >= _cap_cache["expires"]:
            _cap_cache["data"] = _build_capabilities()
            _cap_cache["expires"] = now + _CAP_CACHE_TTL
        return _cap_cache["data"]


def invalidate_capabilities_cache() -> None:
    """프로바이더 변경 등으로 캐시를 즉시 만료시킨다."""
    with _cap_lock:
        _cap_cache["data"] = None
        _cap_cache["expires"] = datetime.min.replace(tzinfo=timezone.utc)


# Tier 2 응답에 포함할 필드 (내부 구성 정보 제외)
_TIER2_FIELDS = {"version", "rag_available", "chunking_enabled", "mcp_spec_version"}


@router.get(
    "/capabilities",
    summary="기능 가용성 조회 (인증 필요, Tier 2)",
    description=(
        "현재 플랫폼의 기능 가용성을 반환한다.\n\n"
        "**인증 필요** — 로그인된 사용자(VIEWER 이상) 접근 가능.\n\n"
        "내부 구성 정보(pgvector_enabled, supported_providers 등)는 "
        "Admin 전용 엔드포인트(`/api/v1/admin/system/capabilities`)에서만 노출된다.\n\n"
        "응답은 **5분간 캐시**된다(`Cache-Control: private, max-age=300`)."
    ),
    response_model=SuccessResponse,
    tags=["system"],
)
def get_capabilities(
    response: Response,
    actor: ActorContext = Depends(resolve_current_actor),
) -> SuccessResponse:
    authorization_service.authorize(
        actor, "system.read", ResourceRef(resource_type="system"),
    )

    full_data = _get_full_capabilities()
    # Tier 2: 내부 구성 정보를 제외한 필드만 반환
    tier2_data = {k: v for k, v in full_data.items() if k in _TIER2_FIELDS}

    response.headers["Cache-Control"] = "private, max-age=300"
    return success_response(data=tier2_data)


@router.get(
    "/metrics",
    summary="Prometheus 메트릭",
    description=(
        "Prometheus scrape 형식으로 HTTP 요청/오류/응답 시간 메트릭을 반환한다.\n\n"
        "production 환경에서는 내부망 또는 별도 인증 레이어(nginx basic auth 등)로 보호해야 한다."
    ),
    tags=["system"],
    response_class=PlainTextResponse,
    include_in_schema=False,
)
def prometheus_metrics(request: Request) -> PlainTextResponse:
    # production 환경에서는 루프백/내부 IP에서만 scrape를 허용한다.
    # 외부 요청은 nginx/gateway 레이어에서 필터링해야 하지만,
    # 방어 심층(defense-in-depth)으로 애플리케이션 레벨에서도 검사한다.
    if settings.environment == "production":
        client_ip = request.client.host if request.client else ""
        # VULN-P13-01 수정: X-Forwarded-For를 우선하면 공격자가 헤더를 위조하여
        # IP 검증을 우회할 수 있음. 실제 TCP 연결 IP(client_ip)를 우선 확인한다.
        # client_ip가 내부 IP(프록시)인 경우에만 XFF의 첫 번째 항목을 신뢰한다.
        _INTERNAL_PREFIXES = ("127.", "10.", "172.16.", "172.17.", "172.18.",
                               "172.19.", "172.20.", "192.168.", "::1")
        if any(client_ip.startswith(p) for p in _INTERNAL_PREFIXES):
            # 신뢰된 프록시 경유 — XFF 첫 번째 항목 사용
            forwarded_for = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            effective_ip = forwarded_for or client_ip
        else:
            # 직접 연결 — XFF 무시 (스푸핑 방지)
            effective_ip = client_ip

        if not any(effective_ip.startswith(p) for p in _INTERNAL_PREFIXES):
            logger.warning("metrics endpoint blocked for external IP: %s", effective_ip)
            return PlainTextResponse(content="403 Forbidden\n", status_code=403)

    text = generate_metrics_text()
    return PlainTextResponse(content=text, media_type="text/plain; version=0.0.4; charset=utf-8")


@router.get(
    "/error-test",
    summary="[Task I-3] 오류 처리 검증",
    description=(
        "공통 exception handler 동작을 확인하기 위한 stub endpoint.\n\n"
        "query param `kind`에 따라 각 예외를 강제로 발생시킨다:\n"
        "- `not_found` → 404\n"
        "- `validation` → 400 (직접 raise)\n"
        "- `auth` → 401\n"
        "- `permission` → 403\n"
        "- `conflict` → 409\n"
        "- `internal` → 500 (예상치 못한 예외)\n"
        "- 없거나 기타 → 200 (정상 응답)\n\n"
        "⚠️ 운영 환경에서는 이 endpoint를 비활성화하거나 보호해야 한다."
    ),
    tags=["system"],
)
def error_test(
    kind: str = Query(default="", description="발생시킬 오류 종류"),
) -> SuccessResponse:
    # VULN-014: debug=True 전용 — production에서 404 반환
    if not settings.debug:
        return JSONResponse(status_code=404, content={"detail": "Not found"})
    match kind:
        case "not_found":
            raise ApiNotFoundError("Test resource was not found")
        case "validation":
            raise ApiValidationError(
                "Validation test failed",
                details=[{"field": "kind", "reason": "test value is invalid"}],
            )
        case "auth":
            raise ApiAuthenticationError()
        case "permission":
            raise ApiPermissionDeniedError()
        case "conflict":
            raise ApiConflictError("Test resource already exists")
        case "internal":
            raise RuntimeError("Simulated unexpected internal error")
        case _:
            return success_response(data={"error_test": "ok", "kind": kind or "none"})
