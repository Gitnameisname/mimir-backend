"""
문장 분할 유틸리티 — Phase 7 FG7.2

약자 예외 처리, 줄바꿈 분할, 최소 길이 필터링 지원.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass
class Sentence:
    text: str
    start_idx: int
    end_idx: int

    def __len__(self) -> int:
        return len(self.text)

    def strip(self) -> str:
        return self.text.strip()


class SentenceSplitter:
    ABBREVIATIONS = frozenset({
        "U.S.A", "U.S", "e.g", "i.e", "etc", "vs", "Dr", "Mr", "Mrs",
        "Ms", "Prof", "Sr", "Jr", "Inc", "Ltd", "Co", "Corp",
    })

    def __init__(
        self,
        min_length: int = 5,
        split_by_newline: bool = True,
        language: str = "en",
    ) -> None:
        self.min_length = min_length
        self.split_by_newline = split_by_newline
        self.language = language

    def _is_sentence_end(self, text: str, pos: int) -> bool:
        if pos >= len(text):
            return False
        char = text[pos]
        if char not in ".!?":
            return False

        if char == ".":
            word_start = pos - 1
            while word_start >= 0 and text[word_start].isalpha():
                word_start -= 1
            word = text[word_start + 1 : pos]
            if word in self.ABBREVIATIONS:
                return False

        if pos + 1 < len(text):
            next_char = text[pos + 1]
            return next_char in ' \n\r"\''
        return True

    def split(self, text: str) -> List[Sentence]:
        if not text or not text.strip():
            return []

        if self.split_by_newline:
            sentences: List[Sentence] = []
            current_start = 0
            for line in text.split("\n"):
                stripped = line.strip()
                if stripped and len(stripped) >= self.min_length:
                    sentences.append(Sentence(
                        text=stripped,
                        start_idx=current_start,
                        end_idx=current_start + len(line),
                    ))
                current_start += len(line) + 1
            return sentences

        sentences = []
        i = 0
        current_start = 0
        while i < len(text):
            if self._is_sentence_end(text, i):
                sentence_text = text[current_start : i + 1].strip()
                if len(sentence_text) >= self.min_length:
                    sentences.append(Sentence(
                        text=sentence_text,
                        start_idx=current_start,
                        end_idx=i + 1,
                    ))
                current_start = i + 1
                while current_start < len(text) and text[current_start] in " \n\r":
                    current_start += 1
            i += 1

        remaining = text[current_start:].strip()
        if len(remaining) >= self.min_length:
            sentences.append(Sentence(
                text=remaining,
                start_idx=current_start,
                end_idx=len(text),
            ))
        return sentences


DEFAULT_SPLITTER = SentenceSplitter()


def split_sentences(text: str) -> List[str]:
    return [s.strip() for s in DEFAULT_SPLITTER.split(text)]
