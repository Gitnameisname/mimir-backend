"""쿼리 재작성 품질 평가 — 수동 검수 데이터 기반.

평가 항목:
  1. 정보 보존: 원본 질의의 핵심 정보가 재작성에 남아있는가
  2. 명확성 증가: 재작성 후 자립적인 쿼리가 되었는가
  3. 맥락 통합: 대화 맥락이 적절히 포함되었는가

각 케이스는 LLM 없이 Mock으로 실행하여 구조 검증만 수행.
실제 LLM 품질 평가는 AI품질평가보고서에서 별도 진행.

MULTITURN_SAMPLES: 새 샘플 추가 시 expected_keywords 필드 필수 포함.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.schemas.conversation import ConversationMessage, MessageRole
from app.services.retrieval.query_rewriter import QueryRewriter


MULTITURN_SAMPLES = [
    {
        "id": 1,
        "history": [
            ("user", "Kubernetes에 대해 알려줘"),
            ("assistant", "Kubernetes는 컨테이너 오케스트레이션 플랫폼입니다."),
        ],
        "original": "이게 뭐야?",
        "expected_keywords": ["Kubernetes"],
        "description": "지시어 '이게' → 이전 주제 연결",
    },
    {
        "id": 2,
        "history": [
            ("user", "Python 비동기 프로그래밍이 뭐야?"),
            ("assistant", "Python asyncio를 사용한 비동기 처리 방식입니다."),
            ("user", "asyncio와 threading의 차이는?"),
            ("assistant", "asyncio는 단일 스레드 이벤트 루프, threading은 멀티스레드입니다."),
        ],
        "original": "더 자세히 설명해줄래?",
        "expected_keywords": ["asyncio", "threading"],
        "description": "4턴 후 '더 자세히' → 직전 주제 연결",
    },
    {
        "id": 3,
        "history": [
            ("user", "FastAPI 라우터 설정 방법"),
            ("assistant", "FastAPI에서 APIRouter를 사용하여 라우터를 구성합니다."),
        ],
        "original": "예시 코드 보여줘",
        "expected_keywords": ["FastAPI", "APIRouter"],
        "description": "코드 예시 요청 → 이전 주제 연결",
    },
    {
        "id": 4,
        "history": [
            ("user", "PostgreSQL 인덱스 종류는?"),
            ("assistant", "B-tree, Hash, GIN, GiST, BRIN 등이 있습니다."),
            ("user", "GIN 인덱스가 뭐야?"),
            ("assistant", "GIN(Generalized Inverted Index)은 배열, JSONB, FTS에 최적화된 인덱스입니다."),
            ("user", "BRIN은?"),
            ("assistant", "BRIN(Block Range INdex)은 큰 테이블의 물리적 순서가 있는 데이터에 효율적입니다."),
        ],
        "original": "언제 쓰는 게 좋아?",
        "expected_keywords": ["BRIN"],
        "description": "5턴 후 '언제 쓰는 게' → 직전 주제 BRIN 연결",
    },
    {
        "id": 5,
        "history": [
            ("user", "Docker 컨테이너와 가상머신의 차이"),
            ("assistant", "Docker는 OS 레벨 가상화, VM은 하드웨어 레벨 가상화입니다."),
        ],
        "original": "어떤 게 더 빠름?",
        "expected_keywords": ["Docker", "가상머신"],
        "description": "비교 질문 → 이전 대상들 연결",
    },
    {
        "id": 6,
        "history": [
            ("user", "RESTful API 설계 원칙"),
            ("assistant", "REST는 상태 비저장, 계층화 시스템, 균일 인터페이스 등의 원칙을 따릅니다."),
            ("user", "HATEOAS가 뭐야?"),
            ("assistant", "HATEOAS는 API 응답에 관련 링크를 포함시키는 REST 제약 조건입니다."),
        ],
        "original": "실제로 많이 쓰여?",
        "expected_keywords": ["HATEOAS", "REST"],
        "description": "사용 빈도 질문 → 이전 주제 연결",
    },
    {
        "id": 7,
        "history": [
            ("user", "JWT 토큰 인증 방식"),
            ("assistant", "JWT는 header.payload.signature 구조로 사용자 정보를 인코딩합니다."),
        ],
        "original": "보안 문제는 없어?",
        "expected_keywords": ["JWT"],
        "description": "보안 질문 → 이전 주제 연결",
    },
    {
        "id": 8,
        "history": [
            ("user", "Redis와 Memcached 비교"),
            ("assistant", "Redis는 영속성 지원, Memcached는 단순 캐시에 특화되어 있습니다."),
            ("user", "Redis에서 데이터 타입은 뭐가 있어?"),
            ("assistant", "String, List, Set, Sorted Set, Hash, Stream 등이 있습니다."),
        ],
        "original": "Sorted Set은 뭐야?",
        "expected_keywords": ["Redis", "Sorted Set"],
        "description": "자료구조 세부 질문 → 이전 컨텍스트 유지",
    },
    {
        "id": 9,
        "history": [
            ("user", "마이크로서비스 아키텍처의 장점"),
            ("assistant", "독립 배포, 기술 다양성, 장애 격리 등의 장점이 있습니다."),
            ("user", "단점은?"),
            ("assistant", "복잡한 서비스 간 통신, 분산 트랜잭션 관리의 어려움이 있습니다."),
        ],
        "original": "어떻게 해결해?",
        "expected_keywords": ["마이크로서비스", "서비스 간 통신"],
        "description": "해결 방법 질문 → 이전 단점 맥락 연결",
    },
    {
        "id": 10,
        "history": [
            ("user", "pgvector란 무엇인가요?"),
            ("assistant", "pgvector는 PostgreSQL에서 벡터 유사도 검색을 지원하는 확장 모듈입니다."),
        ],
        "original": "어떻게 설치해?",
        "expected_keywords": ["pgvector", "PostgreSQL"],
        "description": "설치 질문 → 이전 주제(pgvector) 연결",
    },
]


def _make_history(pairs):
    messages = []
    for role, content in pairs:
        messages.append(ConversationMessage(
            role=MessageRole(role), content=content
        ))
    return messages


@pytest.mark.parametrize("sample", MULTITURN_SAMPLES, ids=[s["id"] for s in MULTITURN_SAMPLES])
@pytest.mark.asyncio
async def test_rewrite_structure(sample):
    """Mock LLM으로 쿼리 재작성 구조 검증 (LLM 호출 여부 및 결과 형식)."""
    # Mock LLM이 expected_keywords를 포함한 재작성 쿼리를 반환
    expected_rewrite = f"{' '.join(sample['expected_keywords'])} 관련 {sample['original']}"
    llm = MagicMock()
    llm.complete = AsyncMock(return_value=(expected_rewrite, 30))

    rewriter = QueryRewriter(llm)
    history = _make_history(sample["history"])
    result = await rewriter.rewrite_query(sample["original"], history)

    # LLM이 호출되어야 함 (대화 이력이 있으므로)
    llm.complete.assert_called_once()
    # 결과가 비어있지 않아야 함
    assert len(result) > 0, f"[Sample {sample['id']}] 재작성 결과가 비어있음"
    # 재작성된 결과여야 함
    assert result == expected_rewrite, (
        f"[Sample {sample['id']}] expected={expected_rewrite!r}, got={result!r}"
    )


@pytest.mark.asyncio
async def test_rewrite_first_turn_no_llm_call():
    """첫 번째 턴 (대화 이력 없음) — LLM 미호출, 원본 반환."""
    llm = MagicMock()
    llm.complete = AsyncMock()
    rewriter = QueryRewriter(llm)
    result = await rewriter.rewrite_query("첫 번째 질의입니다", [])
    assert result == "첫 번째 질의입니다"
    llm.complete.assert_not_called()


@pytest.mark.asyncio
async def test_rewrite_fallback_preserves_original():
    """LLM 실패 시에도 원본 쿼리가 반환되어야 함 (서비스 중단 없음)."""
    llm = MagicMock()
    llm.complete = AsyncMock(side_effect=Exception("LLM timeout"))
    history = [ConversationMessage(role=MessageRole.USER, content="이전 메시지")]
    rewriter = QueryRewriter(llm)
    result = await rewriter.rewrite_query("원본 쿼리", history)
    assert result == "원본 쿼리"
