"""
PII 감지 + 자동 만료 배치 단위 테스트 — Task 3-3.

테스트 범위:
  - PiiDetector: 이메일/전화번호/주민번호/신용카드 감지
  - PiiDetector.detect_fields(), annotate_turn()
  - ExpirationBatchJob: dry-run, 실제 만료 처리, 감사 로그 기록
  - BatchScheduler: start/stop, 환경변수 on/off
  - connection.py DDL: retention_policies 테이블 정의 포함 여부
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest


# ===========================================================================
# 1. PiiDetector 테스트
# ===========================================================================

class TestPiiDetectorEmail:
    def test_detect_simple_email(self):
        from app.services.pii_detector import PiiDetector

        detector = PiiDetector()
        result = detector.detect("연락처: user@example.com 입니다.")
        assert "email" in result
        assert "user@example.com" in result["email"]

    def test_detect_email_subdomains(self):
        from app.services.pii_detector import PiiDetector

        detector = PiiDetector()
        result = detector.detect("admin@mail.company.co.kr")
        assert "email" in result

    def test_no_email_in_plain_text(self):
        from app.services.pii_detector import PiiDetector

        detector = PiiDetector()
        result = detector.detect("일반 텍스트에는 이메일이 없습니다.")
        assert "email" not in result


class TestPiiDetectorPhone:
    def test_detect_korean_mobile(self):
        from app.services.pii_detector import PiiDetector

        detector = PiiDetector()
        result = detector.detect("전화번호: 010-1234-5678")
        assert "phone_kr" in result
        assert "010-1234-5678" in result["phone_kr"]

    def test_detect_various_prefixes(self):
        from app.services.pii_detector import PiiDetector

        detector = PiiDetector()
        for num in ("011-123-4567", "016-9876-5432", "017-111-2222"):
            result = detector.detect(f"번호: {num}")
            assert "phone_kr" in result, f"Phone {num} not detected"

    def test_detect_no_dash_mobile(self):
        from app.services.pii_detector import PiiDetector

        detector = PiiDetector()
        result = detector.detect("01012345678")
        assert "phone_kr_10" in result


class TestPiiDetectorSSN:
    def test_detect_rrn_pattern(self):
        from app.services.pii_detector import PiiDetector

        detector = PiiDetector()
        result = detector.detect("주민번호: 900101-1234567")
        assert "rrn_kr" in result

    def test_no_false_positive_for_regular_numbers(self):
        from app.services.pii_detector import PiiDetector

        detector = PiiDetector()
        result = detector.detect("주문번호: 123456-7890123")
        # 주문번호 형식은 주민번호와 다름 (7번째 자리 1-4 검사)
        assert "rrn_kr" not in result


class TestPiiDetectorCreditCard:
    def test_detect_credit_card_with_spaces(self):
        from app.services.pii_detector import PiiDetector

        detector = PiiDetector()
        result = detector.detect("카드번호: 1234 5678 9012 3456")
        assert "credit_card" in result

    def test_detect_credit_card_with_dashes(self):
        from app.services.pii_detector import PiiDetector

        detector = PiiDetector()
        result = detector.detect("카드번호: 1234-5678-9012-3456")
        assert "credit_card" in result


class TestPiiDetectorMultiple:
    def test_detect_multiple_types(self):
        from app.services.pii_detector import PiiDetector

        detector = PiiDetector()
        text = "이메일: user@test.com 전화: 010-1111-2222"
        result = detector.detect(text)
        assert "email" in result
        assert "phone_kr" in result

    def test_detect_empty_string(self):
        from app.services.pii_detector import PiiDetector

        detector = PiiDetector()
        result = detector.detect("")
        assert result == {}

    def test_detect_fields(self):
        from app.services.pii_detector import PiiDetector

        detector = PiiDetector()
        fields = {
            "user_message": "이메일: user@example.com",
            "assistant_response": "감사합니다.",
        }
        result = detector.detect_fields(fields)
        assert "user_message" in result
        assert "email" in result["user_message"]
        assert "assistant_response" not in result  # PII 없음

    def test_annotate_turn(self):
        from app.services.pii_detector import PiiDetector

        detector = PiiDetector()
        result = detector.annotate_turn(
            user_message="연락처: 010-5555-6666",
            assistant_response="확인했습니다.",
        )
        assert result["has_pii"] is True
        assert "phone_kr" in result["user_message"]
        assert result["assistant_response"] == []
        assert "detected_at" in result

    def test_has_pii_true(self):
        from app.services.pii_detector import PiiDetector

        detector = PiiDetector()
        assert detector.has_pii("user@example.com") is True

    def test_has_pii_false(self):
        from app.services.pii_detector import PiiDetector

        detector = PiiDetector()
        assert detector.has_pii("일반 텍스트") is False

    def test_extra_patterns(self):
        """사용자 지정 패턴 추가."""
        from app.services.pii_detector import PiiDetector

        detector = PiiDetector(extra_patterns={"employee_id": r"EMP-\d{6}"})
        result = detector.detect("사번: EMP-123456")
        assert "employee_id" in result


class TestPiiDetectionDisabled:
    def test_detection_disabled_via_env(self, monkeypatch):
        monkeypatch.setenv("PII_DETECTION_ENABLED", "false")
        # 모듈 재임포트하면 환경변수 반영
        import importlib
        import app.services.pii_detector as mod
        importlib.reload(mod)

        detector = mod.PiiDetector()
        result = detector.detect("user@example.com")
        assert result == {}

        # 복원
        monkeypatch.delenv("PII_DETECTION_ENABLED", raising=False)
        importlib.reload(mod)

    def test_detection_enabled_by_default(self):
        from app.services.pii_detector import PiiDetector

        detector = PiiDetector()
        result = detector.detect("user@example.com")
        assert "email" in result


# ===========================================================================
# 2. ExpirationBatchJob 테스트
# ===========================================================================

def _make_conv_expired(conv_id: str | None = None):
    """만료된 Conversation 객체 생성 헬퍼."""
    from app.models.conversation import Conversation

    now = datetime.now(timezone.utc)
    return Conversation(
        id=conv_id or str(uuid4()),
        owner_id=str(uuid4()),
        organization_id=str(uuid4()),
        title="만료 대화",
        status="active",
        metadata={},
        retention_days=1,
        access_level="private",
        created_at=now - timedelta(days=2),
        updated_at=now - timedelta(days=2),
        expires_at=now - timedelta(hours=1),
        deleted_at=None,
    )


class TestExpirationBatchJob:
    def test_dry_run_returns_count_without_db_change(self):
        from app.services.expiration_batch import ExpirationBatchJob

        expired_convs = [_make_conv_expired() for _ in range(3)]
        mock_conn = MagicMock()
        mock_repo = MagicMock()
        mock_repo.list_expired.return_value = expired_convs

        with patch("app.services.expiration_batch.ConversationRepository", return_value=mock_repo):
            job = ExpirationBatchJob(mock_conn)
            result = job.run(dry_run=True)

        assert result["status"] == "success"
        assert result["expired_count"] == 3
        assert result["dry_run"] is True
        mock_repo.mark_expired.assert_not_called()
        mock_conn.commit.assert_not_called()

    def test_actual_run_marks_expired_and_commits(self):
        from app.services.expiration_batch import ExpirationBatchJob

        conv = _make_conv_expired()
        mock_conn = MagicMock()
        mock_repo = MagicMock()
        mock_repo.list_expired.return_value = [conv]
        mock_repo.mark_expired.return_value = True

        with patch("app.services.expiration_batch.ConversationRepository", return_value=mock_repo), \
             patch("app.services.expiration_batch.audit_emitter") as mock_audit:
            job = ExpirationBatchJob(mock_conn)
            result = job.run(dry_run=False)

        assert result["status"] == "success"
        assert result["expired_count"] == 1
        assert result["failed_count"] == 0
        mock_conn.commit.assert_called_once()
        mock_audit.emit.assert_called_once()

    def test_audit_log_has_system_actor_type(self):
        from app.services.expiration_batch import ExpirationBatchJob

        conv = _make_conv_expired()
        mock_conn = MagicMock()
        mock_repo = MagicMock()
        mock_repo.list_expired.return_value = [conv]
        mock_repo.mark_expired.return_value = True

        with patch("app.services.expiration_batch.ConversationRepository", return_value=mock_repo), \
             patch("app.services.expiration_batch.audit_emitter") as mock_audit:
            job = ExpirationBatchJob(mock_conn)
            job.run(dry_run=False)

        emit_kwargs = mock_audit.emit.call_args[1]
        assert emit_kwargs["actor_type"] == "system"
        assert emit_kwargs["event_type"] == "conversation.expired"

    def test_item_failure_continues_batch(self):
        """개별 아이템 실패 시 배치 중단 없이 계속."""
        from app.services.expiration_batch import ExpirationBatchJob

        convs = [_make_conv_expired() for _ in range(3)]
        mock_conn = MagicMock()
        mock_repo = MagicMock()
        mock_repo.list_expired.return_value = convs
        # 첫 번째는 예외, 나머지는 성공
        mock_repo.mark_expired.side_effect = [Exception("DB error"), True, True]

        with patch("app.services.expiration_batch.ConversationRepository", return_value=mock_repo), \
             patch("app.services.expiration_batch.audit_emitter"):
            job = ExpirationBatchJob(mock_conn)
            result = job.run(dry_run=False)

        assert result["expired_count"] == 2
        assert result["failed_count"] == 1
        assert len(result["errors"]) == 1

    def test_db_failure_returns_error_status(self):
        """DB 전체 장애 시 error 상태 반환."""
        from app.services.expiration_batch import ExpirationBatchJob

        mock_conn = MagicMock()
        mock_repo = MagicMock()
        mock_repo.list_expired.side_effect = Exception("Connection refused")

        with patch("app.services.expiration_batch.ConversationRepository", return_value=mock_repo):
            job = ExpirationBatchJob(mock_conn)
            result = job.run(dry_run=False)

        assert result["status"] == "error"
        assert result["expired_count"] == 0

    def test_empty_list_no_op(self):
        """만료 대상 없으면 0건 성공."""
        from app.services.expiration_batch import ExpirationBatchJob

        mock_conn = MagicMock()
        mock_repo = MagicMock()
        mock_repo.list_expired.return_value = []

        with patch("app.services.expiration_batch.ConversationRepository", return_value=mock_repo), \
             patch("app.services.expiration_batch.audit_emitter"):
            job = ExpirationBatchJob(mock_conn)
            result = job.run(dry_run=False)

        assert result["status"] == "success"
        assert result["expired_count"] == 0
        mock_conn.commit.assert_called_once()


# ===========================================================================
# 3. BatchScheduler 테스트
# ===========================================================================

class TestBatchScheduler:
    def test_scheduler_starts_and_stops(self):
        from app.scheduler import BatchScheduler

        executed = []

        def mock_job(*, request_id=None):
            executed.append(request_id)

        # 1분마다 실행하는 스케줄 (테스트에서는 즉시 실행하지 않음)
        scheduler = BatchScheduler(schedule="* * * * *", job_fn=mock_job)
        scheduler.start()
        assert scheduler.is_running
        scheduler.stop(timeout=2.0)
        assert not scheduler.is_running

    def test_scheduler_not_double_started(self):
        from app.scheduler import BatchScheduler

        scheduler = BatchScheduler(schedule="* * * * *")
        scheduler.start()
        assert scheduler.is_running
        # 두 번 start 해도 스레드 하나만 유지
        scheduler.start()
        assert scheduler.is_running
        scheduler.stop(timeout=2.0)

    def test_auto_expiration_disabled_via_env(self, monkeypatch):
        monkeypatch.setenv("AUTO_EXPIRATION_ENABLED", "false")
        from app import scheduler as sched_mod
        import importlib
        importlib.reload(sched_mod)

        # start_scheduler 호출해도 스케줄러 미시작
        sched_mod.start_scheduler()
        # _scheduler가 None이거나 실행 중이 아님
        assert sched_mod._scheduler is None or not sched_mod._scheduler.is_running

        monkeypatch.delenv("AUTO_EXPIRATION_ENABLED", raising=False)
        importlib.reload(sched_mod)


# ===========================================================================
# 4. DDL 정적 검사
# ===========================================================================

class TestRetentionPoliciesDDL:
    def test_retention_policies_ddl_exists(self):
        from pathlib import Path
        conn_py = (Path(__file__).parents[3] / "backend/app/db/connection.py").read_text()
        assert "retention_policies" in conn_py

    def test_retention_policies_ddl_columns(self):
        from pathlib import Path
        conn_py = (Path(__file__).parents[3] / "backend/app/db/connection.py").read_text()
        assert "default_retention_days" in conn_py
        assert "auto_expire_enabled" in conn_py
        assert "batch_schedule" in conn_py

    def test_retention_policies_registered_in_init_db(self):
        from pathlib import Path
        conn_py = (Path(__file__).parents[3] / "backend/app/db/connection.py").read_text()
        # init_db() 함수 안에서 _RETENTION_POLICIES_DDL 이 호출되는지 확인
        init_section = conn_py.split("def init_db")[1]
        assert "_RETENTION_POLICIES_DDL" in init_section

    def test_scheduler_wired_in_main(self):
        from pathlib import Path
        main_py = (Path(__file__).parents[3] / "backend/app/main.py").read_text()
        assert "start_scheduler" in main_py
        assert "stop_scheduler" in main_py
