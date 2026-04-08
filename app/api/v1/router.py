"""
/api/v1 집계 router.

도메인별 router를 모아 v1 버전 API를 구성한다.
각 도메인 router는 얇게 유지하고, 비즈니스 로직은 service 계층으로 위임한다.

연결된 도메인:
  - system    : 운영성 endpoint (health, meta)
  - documents : 문서 리소스 (Task I-7에서 실제 구현 예정)
  - versions  : 버전 리소스 (Task I-8에서 실제 구현 예정)
  - nodes     : 노드 리소스 (Task I-8에서 실제 구현 예정)

TODO (향후 연결 예정):
  - admin      : 관리자 전용 API
  - operations : 비동기 장기 작업 API
  - webhooks   : 이벤트 구독/전달 API
  - retrieval  : AI/RAG 검색 API
"""
from fastapi import APIRouter

from app.api.v1 import admin, documents, nodes, operations, retrieval, system, versions, webhooks
from app.api.v1 import workflow

v1_router = APIRouter()

# 운영성 endpoint — 공개 접근 허용
v1_router.include_router(system.router, prefix="/system", tags=["system"])

# 핵심 도메인 리소스
v1_router.include_router(documents.router, prefix="/documents", tags=["documents"])
v1_router.include_router(versions.router, prefix="/versions", tags=["versions"])
v1_router.include_router(nodes.router, prefix="/versions", tags=["nodes"])

# Phase 5: Workflow Action API
# /documents/{document_id}/versions/{version_id}/workflow/...
v1_router.include_router(
    workflow.router,
    prefix="/documents/{document_id}/versions/{version_id}/workflow",
    tags=["workflow"],
)

# 확장 예정 — placeholder 수준
v1_router.include_router(admin.router, prefix="/admin", tags=["admin"])
v1_router.include_router(operations.router, prefix="/operations", tags=["operations"])
v1_router.include_router(webhooks.router, prefix="/webhooks", tags=["webhooks"])
v1_router.include_router(retrieval.router, prefix="/retrieval", tags=["retrieval"])
