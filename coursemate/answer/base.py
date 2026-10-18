"""题目与答案的数据模型。

设计要点：AI 返回的选项字母可能与页面实际顺序错位（尤其是选项被打乱时），
所以 AnswerResult 同时保留 option_keys 和 option_texts，
填答案时优先按 key，失败则退回按文本匹配。这个双保险思路来自 OCS 的 answer-wrapper。
"""
from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal

QuestionType = Literal["single", "multiple", "judge", "fill", "unknown"]

_WS = re.compile(r"\s+")
# 题干里常见的干扰前缀：序号、题型标注、分值
_NOISE = re.compile(r"^[\s\d\.、,，)）]*(?:（?[单多]选题?）?|判断题|填空题|简答题)?[\s:：]*")


def normalize(text: str) -> str:
    """归一化文本，用于缓存键和选项匹配。

    去除空白差异、全角半角标点差异，避免同一道题因为渲染差异反复调用 AI。
    """
    if not text:
        return ""
    text = _WS.sub(" ", text).strip()
    trans = str.maketrans("（）［］｛｝，。；：？！“”‘’", "()[]{},.;:?!\"\"''")
    text = text.translate(trans)
    return text.strip().lower()


@dataclass
class Option:
    key: str      # A / B / C / D
    text: str

    def __str__(self) -> str:
        return f"{self.key}. {self.text}"


@dataclass
class Question:
    stem: str
    options: list[Option] = field(default_factory=list)
    qtype: QuestionType = "unknown"
    # popup 是视频内弹题；chapter 是独立章节测验。
    # 章节测验不能套用视频弹题的“逐个试到对”策略。
    context: str = "popup"
    index: int = -1
    # 学习通章节测验使用防复制字体，DOM 文本会乱码；截图保留真实渲染文字。
    image_data_url: str = ""
    # 独立章节测验重新进入时可能已保存答案。已有选择只记录、绝不覆盖。
    selected_keys: list[str] = field(default_factory=list)
    # 网站已给出的错题反馈，仅作为 AI 重答依据，不参与题目身份。
    feedback: str = ""

    @property
    def fingerprint(self) -> str:
        """缓存键：题干 + 选项文本的哈希。

        只用题干不够 —— 同一题干在不同课程可能有不同选项集。
        """
        payload = normalize(_NOISE.sub("", self.stem))
        payload += "||" + "|".join(normalize(o.text) for o in self.options)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    @property
    def is_choice(self) -> bool:
        return self.qtype in ("single", "multiple", "judge") and bool(self.options)

    def describe(self) -> str:
        head = self.stem.strip()[:60].replace("\n", " ")
        return f"[{self.qtype}] {head}{'...' if len(self.stem) > 60 else ''}"


@dataclass
class AnswerResult:
    option_keys: list[str] = field(default_factory=list)
    option_texts: list[str] = field(default_factory=list)
    text: str = ""              # 填空/简答题的答案
    confidence: float = 0.0     # 0~1，低置信度会在日志里显著标注
    reasoning: str = ""
    source: str = "none"        # cache / ai / none

    @property
    def empty(self) -> bool:
        return not self.option_keys and not self.text

    def describe(self) -> str:
        if self.option_keys:
            body = ",".join(self.option_keys)
        else:
            body = self.text[:40]
        return f"{body} (置信度 {self.confidence:.0%}, 来源 {self.source})"


class AnswerProvider(ABC):
    """答案来源抽象。AI、题库 API、人工都可以实现这个接口。"""

    name: str = "base"

    @abstractmethod
    async def solve(self, question: Question) -> AnswerResult:
        """解答一道题。无法作答时返回 empty 的 AnswerResult，不要抛异常。"""

    async def aclose(self) -> None:
        """释放连接等资源。默认无操作。"""
        return None
