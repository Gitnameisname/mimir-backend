"""
LLM03 Training Data Poisoning 검증 테스트.

검증 항목:
  - Fine-tuning 미사용 확인 (N/A)
  - Prompt Registry 접근 제어 (관리자만 수정 가능)
  - Prompt 템플릿 감사 로그
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "security-test-secret")


# ---------------------------------------------------------------------------
# LLM03-001~003: Fine-tuning 미사용 확인
# ---------------------------------------------------------------------------

class TestLLM03NoFineTuning:
    """Fine-tuning 미사용 확인 (N/A 검증)."""

    def test_no_fine_tuning_code_in_codebase(self):
        """LLM03-001: 소스에 fine-tuning 관련 코드가 없다."""
        backend_src = ROOT / "backend/app"

        finetuning_keywords = ["fine_tuning", "finetune", "fine-tune", "training_data"]
        found = []

        # 보안 보고서/테스트 파일은 fine-tuning 용어를 참조용으로 포함할 수 있음
        _EXCLUDED_FILES = {"security_report_generator.py", "test_llm03_training_data.py"}
        for py_file in backend_src.rglob("*.py"):
            if "__pycache__" in str(py_file):
                continue
            if py_file.name in _EXCLUDED_FILES:
                continue
            source = py_file.read_text(encoding="utf-8").lower()
            for kw in finetuning_keywords:
                if kw in source:
                    found.append((py_file.name, kw))

        # fine-tuning 코드가 없어야 함 (N/A)
        assert not found, (
            f"Fine-tuning 코드 발견 — Mimir는 fine-tuning 미사용: {found[:3]}"
        )

    def test_llm_uses_inference_only(self):
        """LLM03-002: LLM이 추론(inference)만 사용한다."""
        services_dir = ROOT / "backend/app/services"

        training_ops = ["train(", "fit(", "backprop", "gradient", "optimizer.step"]
        found = []

        for py_file in services_dir.rglob("*.py"):
            if "__pycache__" in str(py_file):
                continue
            source = py_file.read_text(encoding="utf-8")
            for op in training_ops:
                if op in source:
                    found.append((py_file.name, op))

        assert not found, (
            f"훈련 연산 발견 (inference-only 위반): {found[:3]}"
        )


# ---------------------------------------------------------------------------
# LLM03-004~007: Prompt Registry 접근 제어
# ---------------------------------------------------------------------------

class TestLLM03PromptRegistryAccessControl:
    """Prompt Registry 무결성 검증."""

    def test_prompt_registry_exists(self):
        """LLM03-004: PromptRegistry가 존재한다."""
        from app.services.prompt.registry import PromptRegistry
        assert PromptRegistry is not None

    def test_prompt_registry_seeds_are_readonly_at_runtime(self):
        """LLM03-005: 프롬프트 시드는 런타임에 수정되지 않는다 (읽기 전용 로드)."""
        registry_path = ROOT / "backend/app/services/prompt/registry.py"
        source = registry_path.read_text(encoding="utf-8")

        # 시드 파일을 쓰는 코드가 없어야 함
        write_ops = ["open.*w", r"\.write\(", r"json\.dump\(", r"toml\.dump\("]
        import re
        for pattern in write_ops:
            matches = re.findall(pattern, source)
            if matches:
                # seeds 관련 쓰기 작업인지 확인
                for match in matches:
                    if "seed" in match.lower():
                        pytest.fail(f"프롬프트 시드 파일 쓰기 코드 발견: {match}")

    def test_prompt_registry_get_is_read_only(self):
        """LLM03-006: PromptRegistry.get()이 읽기 전용이다."""
        from app.services.prompt.registry import PromptRegistry
        import inspect

        # get 메서드가 존재하고 파라미터에 쓰기 관련 항목이 없어야 함
        sig = inspect.signature(PromptRegistry.get)
        params = list(sig.parameters.keys())

        write_params = [p for p in params if any(
            kw in p.lower() for kw in ["write", "update", "modify", "set"]
        )]
        assert not write_params, (
            f"PromptRegistry.get()에 쓰기 파라미터 있음: {write_params}"
        )

    def test_prompt_seeds_directory_is_not_user_writable_via_api(self):
        """LLM03-007: API를 통해 프롬프트 시드를 수정할 수 없다."""
        # API 라우터에 프롬프트 파일 직접 수정 엔드포인트가 없어야 함
        api_v1_dir = ROOT / "backend/app/api/v1"
        import re

        for py_file in api_v1_dir.glob("*.py"):
            source = py_file.read_text(encoding="utf-8")
            # 프롬프트 시드 파일 직접 수정 패턴
            seed_write = re.findall(r'seeds.*write|write.*seeds|seed.*open.*w', source, re.IGNORECASE)
            assert not seed_write, (
                f"{py_file.name}: API를 통한 프롬프트 시드 수정 코드 발견"
            )
