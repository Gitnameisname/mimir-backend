"""
Rate Limiter 싱글톤 — slowapi + Valkey 백엔드.

main.py와 각 라우터에서 이 모듈을 공유 참조한다.
순환 임포트를 피하기 위해 main.py 밖에 분리.

SEC3-BE-005: trusted_proxy_count 설정으로 X-Forwarded-For 스푸핑 방어.
  - 0: 직접 연결 — X-Forwarded-For 신뢰하지 않음, REMOTE_ADDR 사용
  - N>0: N개 프록시 신뢰 — XFF 리스트 우측에서 N번째 값만 사용
"""

import logging
from fastapi import Request

from slowapi import Limiter
from slowapi.util import get_remote_address

logger = logging.getLogger(__name__)


def _get_client_ip(request: Request) -> str:
    """X-Forwarded-For 스푸핑 방어를 적용한 클라이언트 IP 추출.

    TRUSTED_PROXY_COUNT 설정에 따라 신뢰할 프록시 수를 결정한다.
    """
    try:
        from app.config import settings
        proxy_count = settings.trusted_proxy_count
    except Exception:
        proxy_count = 0

    if proxy_count <= 0:
        # 프록시 없음 — REMOTE_ADDR 사용 (XFF 무시)
        return get_remote_address(request)

    xff = request.headers.get("X-Forwarded-For", "")
    if not xff:
        return get_remote_address(request)

    # XFF: "client, proxy1, proxy2" — 우측에서 proxy_count번째가 실제 클라이언트
    addrs = [a.strip() for a in xff.split(",") if a.strip()]
    # 신뢰 프록시가 추가한 IP는 우측 proxy_count개이므로 그 왼쪽이 클라이언트
    idx = len(addrs) - proxy_count
    if idx < 0:
        idx = 0
    return addrs[idx] if addrs else get_remote_address(request)


def _build_limiter() -> Limiter:
    try:
        from app.config import settings
        if settings.environment == "test":
            return Limiter(key_func=_get_client_ip)
        return Limiter(
            key_func=_get_client_ip,
            storage_uri=settings.valkey_url,
        )
    except Exception as exc:
        logger.warning("Rate limiter Valkey init failed (%s), falling back to memory", exc)
        return Limiter(key_func=_get_client_ip)


limiter: Limiter = _build_limiter()
