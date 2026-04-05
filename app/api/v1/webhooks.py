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
from fastapi import APIRouter

router = APIRouter()

# TODO: 웹훅 구독/전달 endpoint 구현 예정
