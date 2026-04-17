"""
LLM Judge 용 Prompt 템플릿 — Phase 7 FG7.2

Few-shot examples 포함, 다국어 확장 가능.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class PromptLanguage(str, Enum):
    ENGLISH = "en"
    KOREAN = "ko"


@dataclass
class JudgePromptTemplate:
    name: str
    description: str
    system_role: str
    instruction: str
    examples: List[Dict[str, str]]
    language: PromptLanguage

    def render(self, question: str, answer: str, contexts: List[str], **kwargs) -> str:
        context_text = "\n".join(
            f"[Context {i + 1}]\n{ctx}" for i, ctx in enumerate(contexts)
        )
        return (
            f"{self.system_role}\n\n"
            f"{self.instruction}\n\n"
            f"Question: {question}\n\n"
            f"Contexts:\n{context_text}\n\n"
            f"Answer: {answer}\n\n"
            f"{self._render_examples()}"
            "Evaluation:"
        )

    def _render_examples(self) -> str:
        if not self.examples:
            return ""
        lines = ["Examples:"]
        for i, ex in enumerate(self.examples, 1):
            lines.append(f"\nExample {i}:\n{ex.get('prompt', '')}")
            lines.append(f"Expected output: {ex.get('output', '')}")
        lines.append("")
        return "\n".join(lines) + "\n"


FAITHFULNESS_JUDGE_PROMPTS: Dict[str, JudgePromptTemplate] = {
    "en": JudgePromptTemplate(
        name="faithfulness_judge_v1",
        description="Evaluate whether the answer is grounded in the provided contexts",
        system_role=(
            "You are an expert evaluator assessing the faithfulness of a generated answer. "
            "Determine whether each claim in the answer is supported by the provided contexts."
        ),
        instruction=(
            "Analyze the answer and identify the key claims. For each claim determine:\n"
            "1. Supported by contexts (score = 1)\n"
            "2. Not supported or contradicted (score = 0)\n\n"
            "Respond as JSON:\n"
            '{"claims": [{"text": "...", "score": 0|1, "reasoning": "..."}], '
            '"overall_faithfulness": 0.0..1.0}'
        ),
        examples=[
            {
                "prompt": (
                    "Q: What is the capital of France?\n"
                    "Context: France's capital is Paris.\n"
                    "A: Paris is the capital."
                ),
                "output": (
                    '{"claims": [{"text": "Paris is the capital", "score": 1, "reasoning": "Supported"}], '
                    '"overall_faithfulness": 1.0}'
                ),
            },
        ],
        language=PromptLanguage.ENGLISH,
    ),
    "ko": JudgePromptTemplate(
        name="faithfulness_judge_ko",
        description="생성된 답변이 제공된 컨텍스트에 근거하고 있는지 평가",
        system_role=(
            "당신은 생성된 답변의 신뢰도를 평가하는 전문가입니다. "
            "답변의 각 주장이 컨텍스트로 뒷받침되는지 판단합니다."
        ),
        instruction=(
            "답변을 분석하고 핵심 주장을 식별하세요. 각 주장에 대해:\n"
            "1. 컨텍스트로 뒷받침됨 (점수 = 1)\n"
            "2. 뒷받침되지 않거나 모순됨 (점수 = 0)\n\n"
            "JSON으로 응답:\n"
            '{"claims": [{"text": "...", "score": 0|1, "reasoning": "..."}], '
            '"overall_faithfulness": 0.0~1.0}'
        ),
        examples=[],
        language=PromptLanguage.KOREAN,
    ),
}

CONTEXT_PRECISION_JUDGE_PROMPTS: Dict[str, JudgePromptTemplate] = {
    "en": JudgePromptTemplate(
        name="context_precision_judge_v1",
        description="Evaluate whether each retrieved context is relevant to the question",
        system_role=(
            "You are an expert evaluator assessing retrieval precision. "
            "Score each context based on its relevance to answering the question."
        ),
        instruction=(
            "Score each context:\n"
            "- 1: Highly relevant, directly supports the answer\n"
            "- 0.5: Somewhat relevant\n"
            "- 0: Not relevant\n\n"
            "Respond as JSON:\n"
            '{"context_scores": [{"index": 0, "score": 0..1, "reasoning": "..."}], '
            '"average_precision": 0.0..1.0}'
        ),
        examples=[],
        language=PromptLanguage.ENGLISH,
    ),
}


def get_faithfulness_prompt(
    language: PromptLanguage = PromptLanguage.ENGLISH,
) -> Optional[JudgePromptTemplate]:
    return FAITHFULNESS_JUDGE_PROMPTS.get(language.value)


def get_context_precision_prompt(
    language: PromptLanguage = PromptLanguage.ENGLISH,
) -> Optional[JudgePromptTemplate]:
    return CONTEXT_PRECISION_JUDGE_PROMPTS.get(language.value)
