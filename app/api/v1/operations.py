"""
Operations router — /api/v1/operations

비동기 장기 작업(long-running async operation) API 경계.
현재는 패키지 경계 확보 목적의 placeholder이며, 실제 구현은 이후 Phase에서 추가된다.

TODO (향후 구현 예정):
  - 작업 상태 조회 API
  - 작업 취소 API
  - 202 Accepted 응답 패턴과 연결
  - Task I-11에서 async operation 본격 구현 예정
"""
from fastapi import APIRouter

router = APIRouter()

# TODO: 비동기 작업 endpoint 구현 예정
