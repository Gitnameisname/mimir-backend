"""
최상위 API router.

/api prefix 아래에서 버전별 router를 집계한다.
현재: /api/v1
향후: /api/v2, /api/v3 등 확장 가능
"""
from fastapi import APIRouter

from app.api.v1.router import v1_router

api_router = APIRouter()

# /api/v1
api_router.include_router(v1_router, prefix="/v1")
