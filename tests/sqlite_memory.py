"""会话和用户显式偏好的本地持久化。"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path


class MemoryStore:
    def __init__(self, path: str | Path):
        if str(path) != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS turns (
                id INTEGER PRIMARY KEY, session_id TEXT NOT NULL,
                question TEXT NOT NULL, answer TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS turns_session ON turns(session_id, id);
            CREATE TABLE IF NOT EXISTS memories (id TEXT PRIMARY KEY, content TEXT NOT NULL);
        ''')
        self.db.commit()

    def history(self, session_id: str) -> list[dict]:
        rows = self.db.execute(
            'SELECT question, answer FROM turns WHERE session_id = ? ORDER BY id DESC LIMIT 8',
            (session_id,),
        ).fetchall()
        return [item for question, answer in reversed(rows) for item in (
            {'role': 'user', 'content': question}, {'role': 'assistant', 'content': answer})]

    def sessions(self) -> list[dict]:
        ids = self.db.execute(
            'SELECT session_id FROM turns GROUP BY session_id ORDER BY MAX(id) DESC LIMIT 50'
        ).fetchall()
        result = []
        for (session_id,) in ids:
            rows = self.db.execute(
                'SELECT question, answer FROM turns WHERE session_id = ? ORDER BY id',
                (session_id,),
            ).fetchall()
            messages = [item for question, answer in rows for item in (
                {'role': 'user', 'content': question}, {'role': 'assistant', 'content': answer})]
            result.append({'id': session_id, 'messages': messages})
        return result

    def save_turn(self, session_id: str, question: str, answer: str) -> None:
        with self.db:
            self.db.execute('INSERT INTO turns(session_id, question, answer) VALUES (?, ?, ?)',
                            (session_id, question, answer))

    def session_page(self, limit: int, cursor: int | None, query: str) -> dict:
        from payback_agent.memory import session_page_result
        # 测试存储与 MySQL 使用同一聚合策略，搜索字符按字面匹配。
        needle = query.lower()
        rows = self.db.execute('''
            SELECT s.session_id, substr(f.question, 1, 24), s.last_id,
                   CASE WHEN ? = '' THEN '' ELSE substr(m.question || char(10) || m.answer,
                       max(1, instr(lower(m.question || char(10) || m.answer), ?) - 40), 160) END
            FROM (
                SELECT session_id, min(id) first_id, max(id) last_id,
                       min(CASE WHEN instr(lower(question || char(10) || answer), ?) > 0 THEN id END) match_id
                FROM turns GROUP BY session_id
                HAVING (? = '' OR match_id IS NOT NULL) AND (? IS NULL OR max(id) < ?)
            ) s JOIN turns f ON f.id = s.first_id LEFT JOIN turns m ON m.id = s.match_id
            ORDER BY s.last_id DESC LIMIT ?
        ''', (needle, needle, needle, needle, cursor, cursor, limit + 1)).fetchall()
        return session_page_result(rows, limit)

    def session_detail(self, session_id: str) -> dict | None:
        rows = self.db.execute('SELECT question, answer FROM turns WHERE session_id = ? ORDER BY id',
                               (session_id,)).fetchall()
        if not rows:
            return None
        return {'id': session_id, 'messages': [item for question, answer in rows for item in (
            {'role': 'user', 'content': question}, {'role': 'assistant', 'content': answer})]}

    def delete_session(self, session_id: str) -> bool:
        with self.db:
            return self.db.execute('DELETE FROM turns WHERE session_id = ?', (session_id,)).rowcount > 0

    def memories(self) -> list[dict]:
        return [{'id': row[0], 'content': row[1]} for row in
                self.db.execute('SELECT id, content FROM memories ORDER BY rowid')]

    def add_memory(self, content: str) -> dict:
        memory = {'id': uuid.uuid4().hex, 'content': content}
        with self.db:
            self.db.execute('INSERT INTO memories(id, content) VALUES (?, ?)',
                            (memory['id'], content))
        return memory

    def update_memory(self, memory_id: str, content: str) -> bool:
        with self.db:
            return self.db.execute('UPDATE memories SET content = ? WHERE id = ?',
                                   (content, memory_id)).rowcount > 0

    def delete_memory(self, memory_id: str) -> bool:
        with self.db:
            return self.db.execute('DELETE FROM memories WHERE id = ?', (memory_id,)).rowcount > 0
