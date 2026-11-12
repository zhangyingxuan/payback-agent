"""使用独立 MySQL 账号持久化会话和用户显式偏好。"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Iterator

import pymysql

from . import config


class MemoryStore:
    def __init__(self, db_config: config.MemoryMysqlConfig | None = None):
        self._config = db_config
        self._initialized = False

    @contextmanager
    def connection(self) -> Iterator:
        # 每次操作使用新连接，避免长时间空闲连接失效及不同请求复用事务。
        cfg = self._config or config.get_memory_mysql_config()
        conn = pymysql.connect(host=cfg.host, port=cfg.port, user=cfg.user,
                               password=cfg.password, database=cfg.database,
                               charset='utf8mb4', connect_timeout=5, read_timeout=10,
                               write_timeout=10, autocommit=False)
        try:
            if not self._initialized:
                with conn.cursor() as cursor:
                    cursor.execute('''CREATE TABLE IF NOT EXISTS agent_conversation_turn (
                        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                        session_id VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
                        question MEDIUMTEXT NOT NULL, answer MEDIUMTEXT NOT NULL,
                        INDEX idx_agent_turn_session (session_id, id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')
                    cursor.execute('''CREATE TABLE IF NOT EXISTS agent_long_term_memory (
                        seq BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                        id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL UNIQUE,
                        content TEXT NOT NULL
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')
                    cursor.execute('''CREATE TABLE IF NOT EXISTS agent_memory_import (
                        source_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')
                conn.commit()
                self._initialized = True
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def history(self, session_id: str) -> list[dict]:
        # 只向模型回放最近八轮，完整记录仍可通过会话列表读取。
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute('SELECT question, answer FROM agent_conversation_turn '
                           'WHERE session_id = %s ORDER BY id DESC LIMIT 8', (session_id,))
            rows = cursor.fetchall()
        return [item for question, answer in reversed(rows) for item in (
            {'role': 'user', 'content': question}, {'role': 'assistant', 'content': answer})]

    def sessions(self) -> list[dict]:
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute('SELECT session_id FROM agent_conversation_turn '
                           'GROUP BY session_id ORDER BY MAX(id) DESC LIMIT 50')
            ids = cursor.fetchall()
            result = []
            for (session_id,) in ids:
                cursor.execute('SELECT question, answer FROM agent_conversation_turn '
                               'WHERE session_id = %s ORDER BY id', (session_id,))
                messages = [item for question, answer in cursor.fetchall() for item in (
                    {'role': 'user', 'content': question}, {'role': 'assistant', 'content': answer})]
                result.append({'id': session_id, 'messages': messages})
            return result

    def save_turn(self, session_id: str, question: str, answer: str) -> None:
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute('INSERT INTO agent_conversation_turn(session_id, question, answer) '
                           'VALUES (%s, %s, %s)', (session_id, question, answer))

    def session_page(self, limit: int, cursor: int | None, query: str) -> dict:
        # 摘要通过一次聚合查询获取；游标用消息 ID，不加载正文也不依赖缺失的时间列。
        needle = query.lower()
        # 空查询只聚合索引字段；搜索词通过参数绑定，按字面匹配而非 LIKE 通配符。
        # shortcut: 正文搜索扫描历史文本，历史规模导致延迟明显时升级全文索引。
        match_sql = "MIN(CASE WHEN LOCATE(%s, LOWER(CONCAT(question, '\n', answer))) > 0 THEN id END)" if needle else 'NULL'
        match_params = (needle,) if needle else ()
        with self.connection() as conn, conn.cursor() as sql:
            sql.execute('''
                SELECT s.session_id, LEFT(first_turn.question, 24), s.last_id,
                       CASE WHEN %s = '' THEN '' ELSE SUBSTRING(
                           CONCAT(matched.question, '\n', matched.answer),
                           GREATEST(1, LOCATE(%s, LOWER(CONCAT(matched.question, '\n', matched.answer))) - 40), 160)
                       END
                FROM (
                    SELECT session_id, MIN(id) AS first_id, MAX(id) AS last_id,
                           ''' + match_sql + ''' AS match_id
                    FROM agent_conversation_turn
                    GROUP BY session_id
                    HAVING (%s = '' OR match_id IS NOT NULL)
                       AND (%s IS NULL OR last_id < %s)
                    ORDER BY last_id DESC LIMIT %s
                ) s
                JOIN agent_conversation_turn first_turn ON first_turn.id = s.first_id
                LEFT JOIN agent_conversation_turn matched ON matched.id = s.match_id
                ORDER BY s.last_id DESC
            ''', (needle, needle, *match_params, needle, cursor, cursor, limit + 1))
            return session_page_result(sql.fetchall(), limit)

    def session_detail(self, session_id: str) -> dict | None:
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute('SELECT question, answer FROM agent_conversation_turn '
                           'WHERE session_id = %s ORDER BY id', (session_id,))
            rows = cursor.fetchall()
        if not rows:
            return None
        return {'id': session_id, 'messages': [item for question, answer in rows for item in (
            {'role': 'user', 'content': question}, {'role': 'assistant', 'content': answer})]}

    def delete_session(self, session_id: str) -> bool:
        with self.connection() as conn, conn.cursor() as cursor:
            return bool(cursor.execute('DELETE FROM agent_conversation_turn WHERE session_id = %s',
                                       (session_id,)))

    def memories(self) -> list[dict]:
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute('SELECT id, content FROM agent_long_term_memory ORDER BY seq')
            return [{'id': row[0], 'content': row[1]} for row in cursor.fetchall()]

    def add_memory(self, content: str) -> dict:
        memory = {'id': uuid.uuid4().hex, 'content': content}
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute('INSERT INTO agent_long_term_memory(id, content) VALUES (%s, %s)',
                           (memory['id'], content))
        return memory

    def update_memory(self, memory_id: str, content: str) -> bool:
        with self.connection() as conn, conn.cursor() as cursor:
            # 内容未改变时 MySQL 影响行数可能为零，先确认记录是否存在。
            cursor.execute('SELECT id FROM agent_long_term_memory WHERE id = %s', (memory_id,))
            if cursor.fetchone() is None:
                return False
            cursor.execute('UPDATE agent_long_term_memory SET content = %s WHERE id = %s',
                           (content, memory_id))
            return True

    def delete_memory(self, memory_id: str) -> bool:
        with self.connection() as conn, conn.cursor() as cursor:
            return bool(cursor.execute('DELETE FROM agent_long_term_memory WHERE id = %s',
                                       (memory_id,)))


def session_page_result(rows, limit: int) -> dict:
    # BIGINT 作为字符串传输，防止浏览器丢失游标精度。
    items = [{'id': row[0], 'title': row[1].strip() or '新对话',
              'lastTurnId': str(row[2]), 'snippet': row[3] or ''} for row in rows[:limit]]
    return {'items': items, 'nextCursor': items[-1]['lastTurnId'] if len(rows) > limit else None}


def import_sqlite(store: MemoryStore, source) -> bool:
    """一次性导入旧库；只允许空目标，重复执行不会产生重复消息。"""
    import hashlib
    import json
    import sqlite3
    from pathlib import Path

    path = Path(source).resolve(strict=True)
    # 只读打开旧库，保留原始文件供校验和回滚。
    old = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        old.execute('BEGIN')
        turns = old.execute('SELECT session_id, question, answer FROM turns ORDER BY id').fetchall()
        memories = old.execute('SELECT id, content FROM memories ORDER BY rowid').fetchall()
    finally:
        old.close()
    data = json.dumps([turns, memories], ensure_ascii=False, separators=(',', ':')).encode('utf8')
    digest = hashlib.sha256(data).hexdigest()
    with store.connection() as conn, conn.cursor() as cursor:
        cursor.execute('SELECT source_hash FROM agent_memory_import WHERE source_hash = %s', (digest,))
        if cursor.fetchone():
            return False
        cursor.execute('SELECT COUNT(*) FROM agent_conversation_turn')
        has_turns = cursor.fetchone()[0] > 0
        cursor.execute('SELECT COUNT(*) FROM agent_long_term_memory')
        # 目标已有记录时拒绝自动合并，避免重复导入或覆盖用户数据。
        if has_turns or cursor.fetchone()[0] > 0:
            raise ValueError('MySQL 目标已有数据，请先备份并人工处理，未执行导入')
        cursor.executemany('INSERT INTO agent_conversation_turn(session_id, question, answer) '
                           'VALUES (%s, %s, %s)', turns)
        cursor.executemany('INSERT INTO agent_long_term_memory(id, content) VALUES (%s, %s)', memories)
        cursor.execute('INSERT INTO agent_memory_import(source_hash) VALUES (%s)', (digest,))
    return True
