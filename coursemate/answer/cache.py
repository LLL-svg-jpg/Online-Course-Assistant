"""SQLite 本地题库缓存。

存在的理由很实际：同一门课的弹题在不同章节、不同次播放中高度重复。
没有缓存，每刷一遍课就要为同样的题重复付一次 AI 调用费用；
有了缓存，第二遍几乎零成本，而且离线也能答。
"""
from __future__ import annotations

import json
import re
import sqlite3
import tempfile
import threading
from pathlib import Path

from .base import AnswerResult, Question, normalize

_TEXT_VIEW_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS answers (
    fingerprint TEXT PRIMARY KEY,
    stem        TEXT NOT NULL,
    qtype       TEXT NOT NULL,
    option_keys TEXT NOT NULL,
    option_texts TEXT NOT NULL,
    answer_text TEXT NOT NULL DEFAULT '',
    confidence  REAL NOT NULL DEFAULT 0,
    reasoning   TEXT NOT NULL DEFAULT '',
    hits        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_answers_stem ON answers(stem);
CREATE TABLE IF NOT EXISTS cache_migrations (name TEXT PRIMARY KEY);
"""


class AnswerCache:
    def __init__(self, path: str | Path | None = None, enabled: bool = True):
        from ..paths import app_dir

        self.enabled = enabled
        self.path = Path(path) if path else app_dir() / "runtime" / "answers.db"
        self._conn: sqlite3.Connection | None = None
        if self.enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.path)
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            self._import_knowledge_feedback()
            self._sync_text_view()

    def refresh_text_view(self) -> Path:
        """固定的记事本查看副本；修改副本不会写回数据库。"""
        with _TEXT_VIEW_LOCK:
            path = self.path.with_name("题库查看.txt")
            rows = self._conn.execute(
                "SELECT fingerprint,stem,qtype,option_keys,option_texts,answer_text "
                "FROM answers ORDER BY created_at,fingerprint"
            ).fetchall()
            lines = ["Online Course Assistant 本地题库", f"答案记录：{len(rows)} 条",
                     "此文件随题库自动更新；修改此文件不会改变软件题库。",
                     "历史记录缺少的题干，遇到原题后补全；未保存的完整选项无法恢复。", ""]
            types = {"single": "单选题", "multiple": "多选题", "judge": "判断题", "unknown": "待补全"}
            for index, (fingerprint, stem, qtype, keys, texts, answer) in enumerate(rows, 1):
                lines.extend([f"========== 第{index}条 ==========",
                              f"题干：{stem or '【历史记录未保存题干，遇到原题后补全】'}",
                              f"题型：{types.get(qtype, qtype)}"])
                if keys := json.loads(keys):
                    lines.append("答案选项：" + "、".join(keys))
                if texts := json.loads(texts):
                    lines.extend(["正确答案内容：", *["  " + text for text in texts]])
                if answer:
                    lines.append("答案文本：" + answer)
                lines.extend([f"题目索引：{fingerprint}", ""])
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig", newline="\r\n",
                                                 dir=path.parent, prefix=".answers-view-", suffix=".tmp",
                                                 delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write("\n".join(lines))
                temporary.replace(path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return path

    def _sync_text_view(self) -> None:
        try:
            self.refresh_text_view()
        except OSError as exc:
            from ..logger import Logger

            Logger().warn(f"题库数据已保留，但题库查看文件未更新：{exc}")

    @staticmethod
    def _knowledge_key(question: Question) -> str:
        from ..zhihuishu_knowledge import KnowledgeFeedback

        return "knowledge:" + KnowledgeFeedback.identity(question)

    def _import_knowledge_feedback(self) -> None:
        """旧反馈没有题干，仅迁入真实题目哈希与判对答案；遇到原题再补全。"""
        if self._conn.execute("SELECT 1 FROM cache_migrations WHERE name=?",
                              ("knowledge-feedback-v1",)).fetchone():
            return
        path = self.path.with_name("knowledge-feedback.json")
        items = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        with self._conn:
            for identity, item in items.items():
                texts = item.get("correct", [])
                if (not re.fullmatch(r"[0-9a-f]{64}", identity) or not texts
                        or not all(isinstance(text, str) and text for text in texts)):
                    continue
                self._conn.execute(
                    "INSERT OR IGNORE INTO answers "
                    "(fingerprint,stem,qtype,option_keys,option_texts,confidence,reasoning) "
                    "VALUES (?,?,?,?,?,?,?)",
                    ("knowledge:" + identity, "", "unknown", "[]",
                     json.dumps(texts, ensure_ascii=False), 1.0, "历史知识点：网站判对，题干待补全"),
                )
            self._conn.execute("INSERT INTO cache_migrations VALUES (?)", ("knowledge-feedback-v1",))

    def get(self, question: Question) -> AnswerResult | None:
        if not self._conn:
            return None
        fingerprint = question.fingerprint
        query = ("SELECT option_keys, option_texts, answer_text, confidence, reasoning "
                 "FROM answers WHERE fingerprint = ?")
        row = self._conn.execute(query, (fingerprint,)).fetchone()
        if not row and question.is_choice:
            fingerprint = self._knowledge_key(question)
            row = self._conn.execute(query, (fingerprint,)).fetchone()
        if not row:
            return None
        keys, texts, answer_text, confidence, reasoning = row
        if fingerprint.startswith("knowledge:"):
            correct = [normalize(text) for text in json.loads(texts)]
            mapped = [option.key for option in question.options if normalize(option.text) in correct]
            if len(mapped) != len(correct) or (question.qtype != "multiple" and len(mapped) != 1):
                return None
            keys = json.dumps(mapped)
            self._conn.execute("UPDATE answers SET stem=?,qtype=?,option_keys=? WHERE fingerprint=?",
                               (question.stem[:500], question.qtype, keys, fingerprint))
        self._conn.execute(
            "UPDATE answers SET hits = hits + 1 WHERE fingerprint = ?",
            (fingerprint,),
        )
        self._conn.commit()
        if fingerprint.startswith("knowledge:"):
            self._sync_text_view()
        return AnswerResult(
            option_keys=json.loads(keys),
            option_texts=json.loads(texts),
            text=answer_text,
            confidence=float(confidence),
            reasoning=reasoning,
            source="cache",
        )

    def put(self, question: Question, result: AnswerResult) -> None:
        # 空答案不入库：否则一次网络故障会被永久缓存成"这题答不出来"
        if not self._conn or result.empty:
            return
        fingerprint = question.fingerprint
        if question.is_choice:
            legacy = self._knowledge_key(question)
            if self._conn.execute("SELECT 1 FROM answers WHERE fingerprint=?", (legacy,)).fetchone():
                fingerprint = legacy
        self._conn.execute(
            "INSERT OR REPLACE INTO answers "
            "(fingerprint, stem, qtype, option_keys, option_texts, answer_text, "
            " confidence, reasoning, hits) "
            "VALUES (?,?,?,?,?,?,?,?, COALESCE((SELECT hits FROM answers WHERE fingerprint=?),0))",
            (
                fingerprint,
                question.stem[:500],
                question.qtype,
                json.dumps(result.option_keys, ensure_ascii=False),
                json.dumps(result.option_texts or [o.text for o in question.options
                                                   if o.key in result.option_keys], ensure_ascii=False),
                result.text,
                result.confidence,
                result.reasoning[:500],
                fingerprint,
            ),
        )
        self._conn.commit()
        self._sync_text_view()

    def stats(self) -> tuple[int, int]:
        """返回 (题目总数, 累计命中次数)。"""
        if not self._conn:
            return 0, 0
        row = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(hits), 0) FROM answers"
        ).fetchone()
        return int(row[0]), int(row[1])

    def clear(self) -> None:
        if self._conn:
            with self._conn:
                self._conn.execute("DELETE FROM answers")
            self._sync_text_view()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
