# audit_events.event_type 레지스트리

> 본 문서는 `audit_events.event_type` 컬럼에 들어가는 문자열 literal 의 인벤토리다.
>
> DB 스키마가 enum 제약을 걸지 않으므로 (VARCHAR 100), 본 문서가 비공식 레지스트리 역할을 한다.
> 후속 라운드에서 enum strict 화 또는 typed Literal 통합 검토.
>
> **갱신 규약**: 신규 `event_type` 추가 시 본 문서에 1줄 등록 필수. PR review 게이트.

---

## 1. Phase 별 인벤토리 (실측 기준 — 2026-04-27)

총 distinct event_type: **115 종 + 1 (S3 Phase 3 신설 `document.viewed`)** = 116

본 레지스트리는 Contributors 패널 (FG 3-1) 이 의존하는 8 종을 **명시**하고, 그 외는 인벤토리 산출물 (`docs/개발문서/S3/phase3/산출물/FG3-1_audit이벤트_실측.md`) 참조.

---

## 2. FG 3-1 Contributors 패널 의존 이벤트

### 편집자 카테고리 (Editors)

| event_type | emit 위치 | 의미 | 도입 시기 |
|-----------|----------|-----|---------|
| `document.created` | `api/v1/documents.py` `create_document` | 문서 생성 | Phase 4 |
| `document.updated` | `api/v1/documents.py` `update_document` | 문서 메타 수정 | Phase 4 |
| `draft.updated` | `api/v1/documents.py` `save_draft` | draft 저장 | Phase 5 |
| `draft.nodes_saved` | `api/v1/documents.py` `save_draft_nodes` | draft 노드 저장 | Phase 5 |
| `draft.discarded` | `api/v1/documents.py` `discard_draft` | draft 폐기 | Phase 5 |
| `version.created` | `api/v1/documents.py` `create_document_version` | 새 버전 생성 | Phase 4 |
| `version.restored` | `api/v1/documents.py` `restore_version` | 이전 버전 복원 | Phase 9 |

### 승인자 카테고리 (Approvers)

`workflow_history.to_status='published'` 가 정본. audit_events 의 `document.published` 는 보조 신호.

| event_type | emit 위치 | 의미 | 도입 시기 |
|-----------|----------|-----|---------|
| `document.published` | `api/v1/documents.py` `publish_document` | 발행 액션 (보조) | Phase 5 |

### 열람자 카테고리 (Viewers) — S3 Phase 3 신설

| event_type | emit 위치 | 의미 | 도입 시기 |
|-----------|----------|-----|---------|
| `document.viewed` | `api/v1/documents.py` `get_document` | 인증 viewer 의 단건 조회 | **S3 Phase 3 (FG 3-1, 2026-04-27)** |

**throttle 정책**: `app.audit.viewed_throttle.should_emit_view` 가 per (actor_id, document_id) 5분 윈도우 dedup. 환경변수 `AUDIT_VIEWED_DEDUP_WINDOW_SEC` (기본 300) / `AUDIT_VIEWED_DEDUP_MAX_ENTRIES` (기본 5000) 으로 조정.

**actor 필터**: 인증된 user/agent 만 emit. anonymous 는 emit 없음. service actor 는 throttle helper 가 actor_id 가 None 인 경우 자동 skip.

---

## 3. 명명 규약

- snake_case + dot 구분: `<resource>.<action>` (예: `document.created`, `draft.nodes_saved`)
- 과거형 동사 권장 (이미 발생한 사실): `created`, `updated`, `published`, `viewed`
- 카테고리 prefix:
  - `document.` — Document 리소스
  - `draft.` — Draft 작업
  - `version.` — Version 리소스
  - `workflow.` — 워크플로 전이 (별도, workflow_history 가 정본)
  - `agent.` — Agent 액션
  - `admin.` — 관리자 액션

---

## 4. 변경 로그

| 날짜 | event_type | 변경 | PR/세션 |
|-----|-----------|-----|--------|
| 2026-04-27 | `document.viewed` | 신설 (S3 Phase 3 FG 3-1) | task3-1 Step 1 |
| 2026-04-27 | (기존 8 종) | 본 레지스트리에 등록 | task3-1 Step 1 |
| 2026-04-27 | `scope_profile.settings.changed` | 신설 (S3 Phase 3 FG 3-2). PATCH /admin/scope-profiles/{id} 가 settings 변경 시 emit. metadata: `{before, after}` | task3-2 Step 4 |
| 2026-04-27 | `annotation.created` / `annotation.updated` / `annotation.resolved` / `annotation.reopened` / `annotation.deleted` | 신설 (S3 Phase 3 FG 3-3). 주석 라이프사이클 5종. metadata: `{document_id, ...}` | task3-3 Step 2 |
| 2026-04-27 | `mcp.tool.read_annotations` | 신설 (S3 Phase 3 FG 3-3). MCP read_annotations Tool 호출 시 emit | task3-3 Step 6 |

---

## 5. 별 라운드 후속

- enum / Literal 강제 (현재는 자유 문자열) — 새 event_type 추가 시 IDE 경고 제공
- audit_events.event_type GROUP BY 으로 정기 인벤토리 cron (사용 안 되는 event_type 식별)
- event_type → resource_type / 권한 매트릭스 자동 점검

---

*작성: 2026-04-27 | S3 Phase 3 FG 3-1 (task3-1 Step 1)*
