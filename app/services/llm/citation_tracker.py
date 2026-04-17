"""Citation tracking for LLM responses — LLM09 Overreliance 대응."""
from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# fuzzy match 임계값
_FUZZY_THRESHOLD = 0.80


@dataclass
class TrackedCitation:
    """LLM 응답에서 추적된 인용 정보."""
    document_id: str
    chunk_id: str
    content_hash: str
    quoted_text: str
    version: int = 1


class CitationTracker:
    """LLM 응답과 소스 문서 청크를 비교해 인용 목록을 생성한다."""

    def extract_citations(
        self,
        llm_response: str,
        source_documents: list[dict],
    ) -> list[TrackedCitation]:
        """소스 문서 청크 중 LLM 응답에 포함된 텍스트를 인용으로 추출한다."""
        citations: list[TrackedCitation] = []

        for doc in source_documents:
            doc_id = str(doc.get("id", ""))
            for chunk in doc.get("chunks", []):
                chunk_text = chunk.get("content", "")
                if not chunk_text:
                    continue
                if self._text_in_response(chunk_text, llm_response):
                    citations.append(
                        TrackedCitation(
                            document_id=doc_id,
                            chunk_id=str(chunk.get("id", "")),
                            content_hash=str(chunk.get("hash", "")),
                            quoted_text=chunk_text,
                            version=int(chunk.get("version", 1)),
                        )
                    )

        return citations

    def calculate_citation_present_rate(
        self,
        llm_response: str,
        citations: list[TrackedCitation],
    ) -> float:
        """응답 문장 중 citation으로 뒷받침되는 비율을 반환한다."""
        sentences = [s.strip() for s in llm_response.split(".") if s.strip()]
        if not sentences:
            return 0.0

        cited = sum(
            1
            for sent in sentences
            if any(c.quoted_text in sent or sent in c.quoted_text for c in citations)
        )
        return cited / len(sentences)

    # ------------------------------------------------------------------

    def _text_in_response(self, source_text: str, response: str) -> bool:
        if source_text in response:
            return True
        # fuzzy window search
        src_len = len(source_text)
        for i in range(max(0, len(response) - src_len + 1)):
            snippet = response[i : i + src_len]
            ratio = difflib.SequenceMatcher(None, source_text, snippet).ratio()
            if ratio >= _FUZZY_THRESHOLD:
                return True
        return False
