# backend/scripts

운영/개발 보조 스크립트 모음.

## seed_users.py

초기 관리자 계정과 테스트용 사용자 계정을 생성한다.

### 실행

```bash
cd backend
python -m scripts.seed_users
```

### 기본 생성 계정

| 구분 | Email | Password | Role |
|---|---|---|---|
| 관리자 | `admin@mimir.local` | `Admin!2345` | `SUPER_ADMIN` |
| 테스트 사용자 | `user@mimir.local`  | `User!2345`  | `AUTHOR` |

두 계정 모두 `email_verified=True`, `status=ACTIVE`, `auth_provider=local` 로 생성된다.

### 환경변수로 재정의

운영 환경에서는 반드시 환경변수로 강력한 비밀번호를 주입하여 실행할 것.

```bash
SEED_ADMIN_EMAIL=ops@example.com \
SEED_ADMIN_PASSWORD='S0me-Str0ng-Pass!' \
SEED_ADMIN_NAME='플랫폼 관리자' \
SEED_USER_EMAIL=qa@example.com \
SEED_USER_PASSWORD='Qa-Str0ng-Pass!' \
SEED_USER_NAME='QA 테스터' \
SEED_USER_ROLE=REVIEWER \
python -m scripts.seed_users
```

### 멱등성

- 이미 존재하는 이메일은 새로 만들지 않는다.
- 기존 계정이 있을 때 `role_name` 이 다르거나 `email_verified=False` 인 경우에만 해당 필드만 보정한다.
- **비밀번호는 절대 덮어쓰지 않는다** — 사용자가 변경했을 가능성을 보호하기 위함.

### 보안 주의

- 기본 비밀번호(`Admin!2345`, `User!2345`)는 **개발/테스트 전용**이다.
- 운영 환경에 기본 비밀번호가 배포되지 않도록 주의한다.
- 실행 직후 로그인하여 비밀번호를 즉시 교체하는 것을 권장한다.

## Alembic DB 마이그레이션 (S2-5, 2026-04-20 도입)

`users.scope_profile_id` 추가처럼 OWNER 권한이 필요한 스키마 변경은 런타임
DB 유저로는 적용할 수 없다 (`init_db()` 가 `must be owner of table users`
로 skip). 2026-04-20 부터 Alembic 을 도입해 OWNER 권한 유저로 1회 실행하는
방식으로 전환했다.

### 구조

- `backend/alembic.ini` — Alembic 설정. `sqlalchemy.url` 은 placeholder 이고,
  실제 DB URL 은 `env.py` 에서 `app.config.settings.database_url` 로 주입.
- `backend/app/db/migrations/env.py` — 실행 컨텍스트 (online/offline 지원).
- `backend/app/db/migrations/versions/` — 각 revision 파일 (raw SQL 기반).

### 실행

```bash
cd backend

# 필요 패키지 1회 설치 (alembic, SQLAlchemy 2.x)
pip install -r requirements.txt

# (권장) migration 만 OWNER 권한 DB 유저로 — .env 의 런타임 계정을 건드리지 않는다
ALEMBIC_POSTGRES_USER=<owner> \
ALEMBIC_POSTGRES_PASSWORD='<owner_password>' \
python -m alembic upgrade head

# 또는 URL 전체를 주고 싶으면:
ALEMBIC_DATABASE_URL='postgresql://<owner>:<pw>@<host>:<port>/<db>' \
python -m alembic upgrade head

# 런타임 계정이 OWNER 인 환경이면 override 생략:
python -m alembic upgrade head

# 현재 적용된 revision 확인
python -m alembic current

# --sql 모드 (연결하지 않고 SQL 출력만) — DBA 검토용
python -m alembic upgrade head --sql
```

### DB 계정 우선순위

`env.py` 는 다음 순서로 접속 정보를 해석한다.

1. `ALEMBIC_DATABASE_URL` (전체 URL)
2. `ALEMBIC_POSTGRES_USER` + `ALEMBIC_POSTGRES_PASSWORD` (+ 선택 HOST/PORT/DB)
3. `app.config.settings.database_url` — 즉 런타임 `POSTGRES_*` 값

런타임 유저는 보통 OWNER 가 아니므로 1) 또는 2) 를 쓴다. 환경변수로만 분리
하므로 `.env` 를 건드리지 않아도 된다.

### 원칙

- 런타임 유저(FastAPI 프로세스가 사용하는 DB 계정)로는 실행 금지. OWNER
  권한 유저로만 실행한다. S1 ③ 원칙(스키마 통제) 유지.
- ORM 을 쓰지 않으므로 각 revision 은 `op.execute(<raw SQL>)` 로 작성한다.
- 모든 SQL 은 idempotent (`IF NOT EXISTS`, `WHERE NOT EXISTS`, `IS NULL` 가드)
  로 작성해 재적용 안전성을 보장한다.
- 신규 스키마 변경은 `init_db()` 가 아니라 새 revision 파일로 추가한다.

## s2_5_users_scope_migration.sql (DEPRECATED)

Alembic 도입 전 작성한 비상용 수동 SQL 이다. 현재는 **사용하지 않는 것을 권장**
하며 동등한 동작을 `alembic upgrade head` 로 수행할 수 있다. 파일 자체는
Alembic 을 사용할 수 없는 환경(예: psql 만 사용 가능한 고립된 운영 접근 경로)
을 위한 fallback 으로 남겨두었다.
