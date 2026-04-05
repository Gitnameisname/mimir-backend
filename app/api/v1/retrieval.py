"""
Retrieval router — /api/v1/retrieval

AI/RAG 기반 지식 검색 API 경계.
현재는 패키지 경계 확보 목적의 placeholder이며, 실제 구현은 이후 Phase에서 추가된다.

TODO (향후 구현 예정):
  - 의미 기반 검색(vector search) API
  - RAG 질의응답 API
  - citation 기반 근거 제공 API
  - Task I-12에서 AI/RAG read-model과 연계 예정
"""
from fastapi import APIRouter

router = APIRouter()

# TODO: AI/RAG 검색 endpoint 구현 예정
