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
