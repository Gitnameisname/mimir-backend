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

from app.api.v1 import admin, documents, nodes, operations, retrieval, search, system, versions, webhooks
from app.api.v1 import workflow
from app.api.v1 import diff
from app.api.v1 import vectorization
from app.api.v1 import rag
from app.api.v1 import auth_router as auth
from app.api.v1 import account_router as account
from app.api.v1 import citations  # Phase 2: Citation 역참조 API
from app.api.v1 import conversations  # Phase 3: Conversation Domain API
from app.api.v1 import mcp_router  # Phase 4: MCP 2025-11-25 Server
from app.api.v1 import scope_profiles  # Phase 4: Scope Profile + Agent CRUD
from app.api.v1 import agent_proposals  # S2 Phase 5 (FG5.1): 에이전트 Draft 제안
from app.api.v1 import proposal_queue  # S2 Phase 5 (FG5.2): 제안 큐 Admin/User API
from app.api.v1 import golden_sets  # S2 Phase 7 (FG7.1): Golden Set 도메인
from app.api.v1 import evaluations  # S2 Phase 7 (FG7.2): 평가 실행 API
from app.api.v1 import extraction_schemas  # S2 Phase 8 (FG8.1): 추출 스키마 CRUD
from app.api.v1 import extractions  # S2 Phase 8 (FG8.2): 추출 결과 검토 API
from app.api.v1 import batch_extractions  # S2 Phase 8 (Task 8-7): 배치 재추출 API
from app.api.v1 import extraction_evaluations  # S2 Phase 8 (FG8.3): 추출 품질 평가 API

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

# Phase 8: 검색 API
v1_router.include_router(search.router, prefix="/search", tags=["search"])

# Phase 9: Diff API
# /documents/{document_id}/versions/{v_id}/diff[/{v2_id}][/summary]
v1_router.include_router(
    diff.router,
    prefix="/documents/{document_id}/versions",
    tags=["diff"],
)

# Phase 10: 벡터화 파이프라인 API
v1_router.include_router(vectorization.router, prefix="/vectorization", tags=["vectorization"])

# Phase 11: RAG 질의응답 API
v1_router.include_router(rag.router, prefix="/rag", tags=["rag"])

# Phase 14: 인증 API
v1_router.include_router(auth.router, prefix="/auth", tags=["auth"])

# Phase 14-7: 계정 관리 API
v1_router.include_router(account.router, prefix="/account", tags=["account"])

# 확장 예정 — placeholder 수준
v1_router.include_router(admin.router, prefix="/admin", tags=["admin"])
v1_router.include_router(operations.router, prefix="/operations", tags=["operations"])
v1_router.include_router(webhooks.router, prefix="/webhooks", tags=["webhooks"])
v1_router.include_router(retrieval.router, prefix="/retrieval", tags=["retrieval"])

# Phase 2: Citation 역참조 API
v1_router.include_router(citations.router, tags=["citations"])

# Phase 3: Conversation Domain API
v1_router.include_router(conversations.router, prefix="/conversations", tags=["conversations"])

# Phase 4: MCP 2025-11-25 Server
v1_router.include_router(mcp_router.router, prefix="/mcp", tags=["mcp"])

# Phase 4: Scope Profile CRUD + Agent 관리 + Kill Switch (admin 하위)
v1_router.include_router(scope_profiles.router, prefix="/admin", tags=["admin", "agents"])

# S2 Phase 5 (FG5.1): 에이전트 Draft 제안 / 워크플로 전이 제안 / 승인·반려
v1_router.include_router(agent_proposals.router, tags=["agent-proposals"])

# S2 Phase 5 (FG5.2): 제안 큐 Admin/User API
v1_router.include_router(proposal_queue.router, tags=["proposals"])

# S2 Phase 7 (FG7.1): Golden Set 도메인 (RAG 품질 평가 기준 데이터)
v1_router.include_router(golden_sets.router, prefix="/golden-sets", tags=["golden-sets"])
# S2 Phase 7 (FG7.2): 평가 실행 API
v1_router.include_router(evaluations.router, prefix="/evaluations", tags=["evaluations"])

# S2 Phase 8 (FG8.1): 추출 스키마 CRUD + 버전 관리
v1_router.include_router(extraction_schemas.router, prefix="/extraction-schemas", tags=["extraction-schemas"])

# S2 Phase 8 (FG8.2): 추출 결과 검토 API (pending 큐 → approve/modify/reject)
v1_router.include_router(extractions.router, prefix="/extractions", tags=["extractions"])

# S2 Phase 8 (Task 8-7): 배치 재추출 API
v1_router.include_router(batch_extractions.router, prefix="/extractions", tags=["extractions"])

# S2 Phase 8 (FG8.3): 추출 품질 평가 API
v1_router.include_router(extraction_evaluations.router, prefix="/extraction-evaluations", tags=["extraction-evaluations"])
