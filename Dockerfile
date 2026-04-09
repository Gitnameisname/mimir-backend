# ============================================================
# Mimir Backend — Multi-stage Dockerfile
# ============================================================

# --- 빌드 단계 ---
FROM python:3.11-slim AS builder

WORKDIR /build

# 시스템 의존성 (psycopg2 컴파일용)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt


# --- 런타임 단계 ---
FROM python:3.11-slim AS runtime

# 비루트 사용자 생성 (보안 강화)
RUN groupadd -r mimir && useradd -r -g mimir -d /app -s /sbin/nologin mimir

# 런타임 의존성
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 빌더에서 설치된 패키지 복사
COPY --from=builder /root/.local /home/mimir/.local
ENV PATH="/home/mimir/.local/bin:$PATH"
ENV PYTHONPATH="/app"
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# 소스 코드 복사
COPY --chown=mimir:mimir app/ ./app/

USER mimir

EXPOSE 8000

# 헬스체크
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -sf http://localhost:8000/api/v1/system/health || exit 1

# 시작 명령
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
