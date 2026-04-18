"""
Webhooks router — /api/v1/webhooks

이벤트 구독 및 전달 API 경계.
현재는 패키지 경계 확보 목적의 placeholder이며, 실제 구현은 이후 Phase에서 추가된다.

TODO (향후 구현 예정):
  - webhook 구독 등록/해제 API
  - 이벤트 발행 이력 조회 API
  - 전달 상태 확인 API
  - 중복 방지(idempotency) 연계
"""
import ipaddress
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException

router = APIRouter()

# SSRF 방어: 웹훅 URL 허용 목록 (allowlist) 기반 검증
_ALLOWED_URL_SCHEMES = {"https"}


def validate_webhook_url(url: str) -> str:
    """웹훅 대상 URL을 검증한다.

    - https 스킴만 허용 (SSRF 방어)
    - 로컬호스트/내부 IP 차단 (IPv4 + IPv6)
    - 빈 URL 거부
    """
    if not url:
        raise HTTPException(status_code=400, detail="웹훅 URL이 비어 있습니다.")
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_URL_SCHEMES:
        raise HTTPException(status_code=400, detail="웹훅 URL은 https만 허용됩니다.")
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise HTTPException(status_code=400, detail="웹훅 URL의 호스트를 확인할 수 없습니다.")
    if hostname == "localhost":
        raise HTTPException(status_code=400, detail="내부 네트워크 URL은 허용되지 않습니다.")
    try:
        addr = ipaddress.ip_address(hostname)
        if addr.is_loopback or addr.is_private or addr.is_link_local or addr.is_multicast:
            raise HTTPException(status_code=400, detail="내부 네트워크 URL은 허용되지 않습니다.")
    except ValueError:
        pass  # 도메인명 — 정상
    return url


# TODO: 웹훅 구독/전달 endpoint 구현 예정
