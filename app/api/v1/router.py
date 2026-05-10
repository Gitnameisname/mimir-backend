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
from app.api.v1 import admin_extraction_results  # S2 Phase 8 (B 스코프): 관리자 검토 큐 API
from app.api.v1 import batch_extractions  # S2 Phase 8 (Task 8-7): 배치 재추출 API
from app.api.v1 import extraction_evaluations  # S2 Phase 8 (FG8.3): 추출 품질 평가 API
from app.api.v1 import collections as collections_router  # S3 Phase 2 (FG 2-1)
from app.api.v1 import folders as folders_router  # S3 Phase 2 (FG 2-1)
from app.api.v1 import tags as tags_router  # S3 Phase 2 (FG 2-2)
from app.api.v1 import contributors as contributors_router  # S3 Phase 3 (FG 3-1)
from app.api.v1 import annotations as annotations_router_module  # S3 Phase 3 (FG 3-3)
from app.api.v1 import notifications as notifications_router_module  # S3 Phase 3 (FG 3-3)
from app.api.v1 import document_links as document_links_router_module  # S3 Phase 2 (FG 2-3, 2026-05-10)
from app.api.v1 import document_graph as document_graph_router_module  # S3 Phase 2 (FG 2-4, 2026-05-10)
from app.api.v1 import saved_views as saved_views_router_module  # S3 Phase 2 (FG 2-5, 2026-05-10)
from app.api.v1 import vault_imports as vault_imports_router_module  # S3 Phase 2 (FG 2-6, 2026-05-11)

v1_router = APIRouter()

# 운영성 endpoint — 공개 접근 허용
v1_router.include_router(system.router, prefix="/system", tags=["system"])

# 핵심 도메인 리소스
# S3 Phase 2 (FG 2-3, 2026-05-10): 백링크 / 자동완성. /documents prefix 공유.
# `/resolve` 가 documents 라우터의 `/{document_id}` 보다 **먼저 매칭**되어야 하므로
# documents 보다 앞에 include. 본 라우터는 `/resolve` / `/{document_id}/backlinks` /
# `/{document_id}/links` 만 가지며, 일반 `/{document_id}` 패턴이 없어 documents 라우터의
# 동작에는 영향을 주지 않는다.
v1_router.include_router(
    document_links_router_module.router, prefix="/documents", tags=["wikilinks"]
)
# S3 Phase 2 (FG 2-4, 2026-05-10): 그래프 데이터. /graph 도 documents `/{document_id}` 보다
# 먼저 매칭되어야 함. document_links 와 동일 패턴.
v1_router.include_router(
    document_graph_router_module.router, prefix="/documents", tags=["graph"]
)
v1_router.include_router(documents.router, prefix="/documents", tags=["documents"])

# S3 Phase 2 (FG 2-5, 2026-05-10): 사용자 저장 뷰 (필터+정렬+레이아웃 + 공유 URL)
v1_router.include_router(
    saved_views_router_module.router, prefix="/saved-views", tags=["saved-views"],
)

# S3 Phase 2 (FG 2-6, 2026-05-11): 옵시디언 vault zip import
v1_router.include_router(
    vault_imports_router_module.router, prefix="/vault-imports", tags=["vault-imports"],
)
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

# S2 Phase 8 (B 스코프, 2026-04-22): 관리자 검토 큐 전용 API
# /admin/extraction-results 경로로 프론트 `/admin/extraction-queue` 가 직접 호출.
v1_router.include_router(
    admin_extraction_results.router,
    prefix="/admin/extraction-results",
    tags=["admin", "extractions"],
)

# S2 Phase 8 (Task 8-7): 배치 재추출 API
v1_router.include_router(batch_extractions.router, prefix="/extractions", tags=["extractions"])

# S2 Phase 8 (FG8.3): 추출 품질 평가 API
v1_router.include_router(extraction_evaluations.router, prefix="/extraction-evaluations", tags=["extraction-evaluations"])

# S3 Phase 2 (FG 2-1): 수동 컬렉션 + 계층 폴더 (뷰 레이어, ACL 무영향)
v1_router.include_router(collections_router.router, prefix="/collections", tags=["collections"])
v1_router.include_router(folders_router.router, prefix="/folders", tags=["folders"])

# S3 Phase 2 (FG 2-2): 태그 동적 그룹 (서버 파서가 정본, 뷰 레이어)
v1_router.include_router(tags_router.router, prefix="/tags", tags=["tags"])

# S3 Phase 3 (FG 3-1): Contributors 패널 (작성자/편집자/승인자/열람자 4 카테고리)
# /api/v1/documents/{document_id}/contributors
v1_router.include_router(
    contributors_router.router,
    prefix="/documents",
    tags=["documents", "contributors"],
)

# S3 Phase 3 (FG 3-3): 인라인 주석
# - /api/v1/documents/{document_id}/annotations  (목록 / 생성)
# - /api/v1/annotations/{annotation_id}          (단건 / 수정 / 해결 / 재오픈 / 삭제)
v1_router.include_router(
    annotations_router_module.documents_annotations_router,
    prefix="/documents",
    tags=["annotations"],
)
v1_router.include_router(
    annotations_router_module.annotations_router,
    prefix="/annotations",
    tags=["annotations"],
)

# S3 Phase 3 (FG 3-3): In-app 알림 (멘션 등)
v1_router.include_router(
    notifications_router_module.router,
    prefix="/notifications",
    tags=["notifications"],
)
