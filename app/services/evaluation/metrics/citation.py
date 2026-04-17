"""
Citation 형식 정의 및 감지 모듈 — Phase 7 FG7.2

지원 Citation 형식:
1. Structured: [CITE: entity_type=X, entity_value=Y, source_type=Z, source_id=W, chunk_id=V]
2. Markdown: [text](source://source_id#chunk_id)
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import List


class EntityType(str, Enum):
    DOCUMENT = "document"
    CHUNK = "chunk"
    PASSAGE = "passage"
    CLAIM = "claim"
    FACT = "fact"


class SourceType(str, Enum):
    FILE = "file"
    WEBPAGE = "webpage"
    DATABASE = "database"
    KNOWLEDGE_BASE = "knowledge_base"
    CUSTOM = "custom"


@dataclass
class Citation:
    entity_type: EntityType
    entity_value: str
    source_type: SourceType
    source_id: str
    chunk_id: str

    def to_dict(self) -> dict:
        return {
            "entity_type": self.entity_type.value,
            "entity_value": self.entity_value,
            "source_type": self.source_type.value,
            "source_id": self.source_id,
            "chunk_id": self.chunk_id,
        }

    def __str__(self) -> str:
        return (
            f"[CITE: entity_type={self.entity_type.value}, "
            f"entity_value={self.entity_value}, source_type={self.source_type.value}, "
            f"source_id={self.source_id}, chunk_id={self.chunk_id}]"
        )


class CitationParser(ABC):
    @abstractmethod
    def parse(self, text: str) -> List[Citation]:
        pass

    @abstractmethod
    def can_parse(self, text: str) -> bool:
        pass


class StructuredCitationParser(CitationParser):
    """Parses [CITE: key=val, ...] format."""

    PATTERN = re.compile(
        r"\[CITE:\s*"
        r"entity_type=([^,\]]+),\s*"
        r"entity_value=([^,\]]+),\s*"
        r"source_type=([^,\]]+),\s*"
        r"source_id=([^,\]]+),\s*"
        r"chunk_id=([^\]]+)"
        r"\]"
    )

    def can_parse(self, text: str) -> bool:
        return "[CITE:" in text

    def parse(self, text: str) -> List[Citation]:
        citations = []
        for match in self.PATTERN.finditer(text):
            try:
                et, ev, st, sid, cid = match.groups()
                citations.append(Citation(
                    entity_type=EntityType(et.strip()),
                    entity_value=ev.strip(),
                    source_type=SourceType(st.strip()),
                    source_id=sid.strip(),
                    chunk_id=cid.strip(),
                ))
            except (ValueError, AttributeError):
                continue
        return citations


class MarkdownCitationParser(CitationParser):
    """Parses [text](source_type://source_id#chunk_id) format."""

    PATTERN = re.compile(r"\[([^\]]+)\]\(([^:)]+)://([^#)]+)#([^)]+)\)")

    def can_parse(self, text: str) -> bool:
        return "](" in text

    def parse(self, text: str) -> List[Citation]:
        citations = []
        for match in self.PATTERN.finditer(text):
            try:
                ev, st, sid, cid = match.groups()
                citations.append(Citation(
                    entity_type=EntityType.CHUNK,
                    entity_value=ev.strip(),
                    source_type=SourceType(st.strip()),
                    source_id=sid.strip(),
                    chunk_id=cid.strip(),
                ))
            except (ValueError, IndexError):
                continue
        return citations


class CitationDetector:
    """Multi-format citation detector with deduplication."""

    def __init__(self) -> None:
        self.parsers: List[CitationParser] = [
            StructuredCitationParser(),
            MarkdownCitationParser(),
        ]

    def detect_citations(self, text: str) -> List[Citation]:
        seen: dict[tuple, Citation] = {}
        for parser in self.parsers:
            if parser.can_parse(text):
                for cite in parser.parse(text):
                    key = (cite.source_id, cite.chunk_id)
                    if key not in seen:
                        seen[key] = cite
        return list(seen.values())

    def has_citation(self, text: str) -> bool:
        return bool(self.detect_citations(text))
