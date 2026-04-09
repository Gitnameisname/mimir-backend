"""
Rate Limiter 싱글톤 — slowapi + Valkey 백엔드.

main.py와 각 라우터에서 이 모듈을 공유 참조한다.
순환 임포트를 피하기 위해 main.py 밖에 분리.
"""

import logging

from slowapi import Limiter
from slowapi.util import get_remote_address

logger = logging.getLogger(__name__)


def _build_limiter() -> Limiter:
    try:
        from app.config import settings
        return Limiter(
            key_func=get_remote_address,
            storage_uri=settings.valkey_url,
        )
    except Exception as exc:
        logger.warning("Rate limiter Valkey init failed (%s), falling back to memory", exc)
        return Limiter(key_func=get_remote_address)


limiter: Limiter = _build_limiter()
