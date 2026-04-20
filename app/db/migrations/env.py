"""
Alembic 실행 컨텍스트 (S2-5, 2026-04-20).

설계 원칙:
  - 프로젝트는 SQLAlchemy ORM 을 사용하지 않는다. target_metadata 는 None.
  - DB URL 해석 우선순위:
        1) ALEMBIC_DATABASE_URL        (OWNER 접속용 전체 URL)
        2) ALEMBIC_POSTGRES_USER/PASSWORD (+ 기본 HOST/PORT/DB)
        3) app.config.settings.database_url (런타임 POSTGRES_* 환경변수)
    alembic 은 DDL 을 실행하므로 통상 runtime 유저가 아닌 OWNER 권한 계정으로
    접속해야 한다. 1) 또는 2) 로 분리하면 .env 를 고치지 않고 migration 만
    OWNER 로 수행할 수 있다.
  - alembic.ini 의 sqlalchemy.url 값은 무시한다 (placeholder).
  - 각 마이그레이션은 op.execute() 로 raw SQL 을 실행한다.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# backend/ 디렉터리를 sys.path 에 추가 — app.config 를 import 하기 위함
_BACKEND_ROOT = Path(__file__).resolve().parents[3]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.config import settings  # noqa: E402


def _resolve_database_url() -> str:
    """Alembic 전용 URL 우선순위 해석.

    우선순위:
      1) ALEMBIC_DATABASE_URL
      2) ALEMBIC_POSTGRES_USER + ALEMBIC_POSTGRES_PASSWORD 로 조립
      3) settings.database_url (런타임 POSTGRES_* fallback)
    """
    override = os.environ.get("ALEMBIC_DATABASE_URL")
    if override:
        return override

    alembic_user = os.environ.get("ALEMBIC_POSTGRES_USER")
    alembic_pass = os.environ.get("ALEMBIC_POSTGRES_PASSWORD")
    if alembic_user and alembic_pass is not None:
        host = os.environ.get("ALEMBIC_POSTGRES_HOST", settings.postgres_host)
        port = os.environ.get("ALEMBIC_POSTGRES_PORT", str(settings.postgres_port))
        db = os.environ.get("ALEMBIC_POSTGRES_DB", settings.postgres_db)
        return f"postgresql://{alembic_user}:{alembic_pass}@{host}:{port}/{db}"

    return settings.database_url


config = context.config

# alembic.ini 의 [loggers] 섹션을 적용
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 런타임 DB URL 주입 (alembic.ini 의 placeholder 덮어쓰기)
config.set_main_option("sqlalchemy.url", _resolve_database_url())

# SQLAlchemy 모델 메타데이터가 없으므로 autogenerate 는 비활성 상태로 둔다.
target_metadata = None


def run_migrations_offline() -> None:
    """--sql 모드 (오프라인) 실행.

    DB 에 직접 연결하지 않고 마이그레이션 SQL 을 표준출력으로 내보낸다.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """실제 DB 연결 모드."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # 기본 compare_type=False — ORM 이 없으므로 autogenerate 비활성
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
