"""
S3 Phase 0 / FG 0-2 — `EMBEDDING_DIM` 설정 ↔ DB 스키마 차원 일치 검증 헬퍼.

본 모듈은 Alembic revision 과 `/api/v1/system/health` 양측에서 재사용되는 순수 검증 함수들을 제공한다.

설계 원칙 (CLAUDE.md S1 ②③):
  - pgvector 컬럼 부재는 **허용** 한다 (현재 벡터는 Milvus 로 분리된 구조).
  - 컬럼이 존재하는 경우에만 차원 일치를 강제한다. 불일치는 `RuntimeError` 로 알린다.
  - Milvus collection 차원 검증은 best-effort (외부 서버 도달 불가 시 degrade).
  - 폐쇄망 대응 (S2 ⑦): Milvus 미연결/비활성 환경에서도 예외 없이 결과 반환.

BUG-04 재발 방지:
  - EMBEDDING_DIM=768 인데 `document_chunks.embedding VECTOR(1536)` 식으로 미스매치가
    생기면 INSERT 시점까지 버그가 전파된다. 본 검증은 revision 시점에 차단한다.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 결과 타입
# --------------------------------------------------------------------------- #


@dataclass
class DimCheckResult:
    """EMBEDDING_DIM 일치 검증 결과.

    healthcheck JSON 직렬화 및 Alembic revision 실패 메시지 양쪽에 쓰인다.
    """

    config_dim: int
    db_dim: Optional[int]          # pgvector 컬럼이 있을 때만 채워짐
    column_present: bool            # document_chunks.embedding 컬럼 존재 여부
    match: Optional[bool]          # None = column_present=False 일 때 판정 보류
    column_type: Optional[str] = None   # 'vector(768)' 같은 format_type 원문
    reason: str = ""                     # 사람이 읽는 요약

    # Milvus collection 차원 (best-effort, None = 확인 불가)
    milvus_dim: Optional[int] = None
    milvus_match: Optional[bool] = None
    milvus_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "config": self.config_dim,
            "db": self.db_dim,
            "match": self.match,
            "column_present": self.column_present,
            "reason": self.reason,
        }
        if self.column_type is not None:
            data["column_type"] = self.column_type
        # Milvus 서브체크는 명시적으로 시도했을 때만 포함
        if self.milvus_reason or self.milvus_dim is not None:
            data["milvus"] = {
                "dim": self.milvus_dim,
                "match": self.milvus_match,
                "reason": self.milvus_reason,
            }
        return data

    @property
    def ok(self) -> bool:
        """전체 결과가 '일치 혹은 검증 불가'인가.

        - column_present=False → 현재 아키텍처(Milvus 중심) 기대값이므로 ok.
        - column_present=True + match=True → ok.
        - column_present=True + match=False → NOT ok.
        """
        if not self.column_present:
            return True
        return bool(self.match)


# --------------------------------------------------------------------------- #
# pgvector 컬럼 차원 추출
# --------------------------------------------------------------------------- #


_VECTOR_DIM_RE = re.compile(r"vector\s*\(\s*(\d+)\s*\)", re.IGNORECASE)


def _parse_vector_dim(formatted_type: str) -> Optional[int]:
    """`format_type(a.atttypid, a.atttypmod)` 출력에서 차원 파싱.

    예: 'vector(768)' → 768. 'vector' (무차원) → None.
    """
    if not formatted_type:
        return None
    m = _VECTOR_DIM_RE.search(formatted_type)
    if not m:
        return None
    try:
        return int(m.group(1))
    except (TypeError, ValueError):
        return None


def get_db_embedding_column_info(conn) -> tuple[bool, Optional[int], Optional[str]]:
    """`document_chunks.embedding` 컬럼이 있으면 `(True, 차원, formatted_type)` 반환.

    없거나 타입이 vector 가 아니면 `(False, None, None)`.
    예외가 발생하면 `(False, None, None)` 로 안전 폴백 (healthcheck 견고성).
    """
    sql = """
        SELECT format_type(a.atttypid, a.atttypmod) AS formatted_type
        FROM pg_attribute a
        JOIN pg_class c ON a.attrelid = c.oid
        JOIN pg_namespace n ON c.relnamespace = n.oid
        WHERE n.nspname = 'public'
          AND c.relname = 'document_chunks'
          AND a.attname = 'embedding'
          AND a.attnum > 0
          AND NOT a.attisdropped
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("get_db_embedding_column_info query failed: %s", exc)
        return False, None, None

    if row is None:
        return False, None, None

    # psycopg2 RealDictCursor vs tuple cursor 양쪽 호환
    formatted = row["formatted_type"] if isinstance(row, dict) else row[0]
    if not formatted or "vector" not in formatted.lower():
        return False, None, formatted

    dim = _parse_vector_dim(formatted)
    if dim is None:
        # vector 타입이긴 하나 차원을 못 뽑아낸 비정상 케이스 — present 로 간주하되 dim 미상
        return True, None, formatted
    return True, dim, formatted


# --------------------------------------------------------------------------- #
# Milvus collection 차원 (best-effort)
# --------------------------------------------------------------------------- #


def _probe_milvus_dim(settings) -> tuple[Optional[int], str]:
    """Milvus collection `document_chunks` 의 embedding 필드 차원을 best-effort 조회.

    실패 시 `(None, "<이유>")` 반환. 예외로 전파하지 않는다 (S2 ⑦).
    """
    host = (settings.milvus_host or "").strip()
    if not host:
        return None, "milvus_host 미설정 (비활성)"
    try:
        from app.db.milvus import get_milvus  # noqa: WPS433
        client = get_milvus()
        # client 는 NullClient 폴백일 수 있음 — 속성 존재 여부로 판정
        if hasattr(client, "describe_collection"):
            info = client.describe_collection("document_chunks")
        elif hasattr(client, "describe"):
            info = client.describe("document_chunks")
        else:
            return None, f"Milvus 클라이언트가 describe 메서드 없음: {type(client).__name__}"
    except Exception as exc:  # pragma: no cover - env
        return None, f"Milvus 조회 실패: {exc!r}"

    # pymilvus 2.x describe_collection 은 list/dict 을 반환 — 표준화 시도
    try:
        fields = (
            info.get("fields") if isinstance(info, dict)
            else getattr(info, "fields", None)
        )
        if not fields:
            return None, "collection.fields 없음"
        for f in fields:
            name = f.get("name") if isinstance(f, dict) else getattr(f, "name", None)
            if name != "embedding":
                continue
            params = (
                f.get("params") if isinstance(f, dict) else getattr(f, "params", None)
            ) or {}
            dim = params.get("dim") if isinstance(params, dict) else getattr(params, "dim", None)
            if dim is None and hasattr(f, "dim"):
                dim = f.dim
            try:
                return int(dim), ""
            except (TypeError, ValueError):
                return None, f"embedding 필드 dim 파싱 실패: {dim!r}"
        return None, "embedding 필드 미존재"
    except Exception as exc:
        return None, f"schema 해석 실패: {exc!r}"


# --------------------------------------------------------------------------- #
# 공용 진입점
# --------------------------------------------------------------------------- #


def _get_configured_dim() -> int:
    """EMBEDDING_DIM 설정값. 환경변수 우선, app.config.settings 폴백."""
    env_val = os.environ.get("EMBEDDING_DIM")
    if env_val and env_val.strip():
        try:
            return int(env_val.strip())
        except ValueError:
            logger.warning("EMBEDDING_DIM env 값이 정수 아님: %r — settings 로 폴백", env_val)

    # settings import 는 환경변수 체크 이후 수행 (테스트에서 monkeypatch 편의)
    from app.config import settings  # noqa: WPS433
    return int(settings.embedding_dim or 0)


def check_embedding_dim(conn, *, check_milvus: bool = False) -> DimCheckResult:
    """pgvector 컬럼 및 (선택) Milvus collection 차원을 EMBEDDING_DIM 과 비교.

    인자:
      conn          : psycopg2 연결
      check_milvus  : True 면 Milvus collection 차원도 best-effort 로 확인

    반환:
      DimCheckResult (ok 속성으로 최종 판정)
    """
    configured = _get_configured_dim()

    column_present, db_dim, formatted = get_db_embedding_column_info(conn)

    if not column_present:
        reason = (
            "document_chunks.embedding 컬럼 부재 — 현재 아키텍처(Milvus 중심)와 정합. "
            "BUG-04 재발 방지 가드는 컬럼 생성 이후 revision 에서 강제된다."
        )
        match: Optional[bool] = None
    elif db_dim is None:
        reason = (
            f"document_chunks.embedding 컬럼 존재하나 차원 파싱 실패 (type={formatted!r}). "
            "vector 확장 버전 불일치 가능성 — 수동 점검 필요."
        )
        match = False
    elif db_dim == configured:
        reason = f"일치 (config={configured}, db={db_dim})"
        match = True
    else:
        reason = (
            f"불일치: EMBEDDING_DIM={configured} vs document_chunks.embedding={formatted}. "
            "BUG-04 — 재벡터화 혹은 revision 정합 필요."
        )
        match = False

    result = DimCheckResult(
        config_dim=configured,
        db_dim=db_dim,
        column_present=column_present,
        match=match,
        column_type=formatted,
        reason=reason,
    )

    if check_milvus:
        from app.config import settings  # noqa: WPS433
        milvus_dim, milvus_reason = _probe_milvus_dim(settings)
        result.milvus_dim = milvus_dim
        result.milvus_reason = milvus_reason
        if milvus_dim is None:
            result.milvus_match = None
        else:
            result.milvus_match = (milvus_dim == configured)
            if not result.milvus_match:
                result.milvus_reason = (
                    f"Milvus 불일치: EMBEDDING_DIM={configured} vs milvus.dim={milvus_dim}. "
                    "collection 재생성 + 재벡터화 필요."
                )

    return result


class EmbeddingDimMismatchError(RuntimeError):
    """Alembic revision 에서 불일치 감지 시 던지는 예외.

    RuntimeError 를 상속해 Alembic 이 일반 실패로 처리하도록 한다.
    """
