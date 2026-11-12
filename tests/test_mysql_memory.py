"""Agent MySQL 存储的配置及 SQL 边界测试。"""

import pytest

from payback_agent import config


def test_memory_database_requires_dedicated_credentials(monkeypatch):
    for name in ('HOST', 'PORT', 'USER', 'PASSWORD', 'NAME'):
        monkeypatch.delenv('AGENT_MEMORY_MYSQL_' + name, raising=False)
    with pytest.raises(config.ConfigError, match='AGENT_MEMORY_MYSQL_'):
        config.get_memory_mysql_config()


def test_memory_database_configuration(monkeypatch):
    for key, value in {'HOST': 'localhost', 'PORT': '3306', 'USER': 'writer',
                       'PASSWORD': 'secret', 'NAME': 'payback'}.items():
        monkeypatch.setenv('AGENT_MEMORY_MYSQL_' + key, value)
    db = config.get_memory_mysql_config()
    assert (db.host, db.port, db.user, db.database) == ('localhost', 3306, 'writer', 'payback')
    assert 'secret' not in repr(db)


@pytest.fixture
def mysql_config():
    import os
    import uuid
    import pymysql
    if not os.getenv('TEST_MYSQL_USER'):
        pytest.skip('未配置隔离 MySQL 集成测试账号')
    name = 'payback_agent_test_' + uuid.uuid4().hex[:12]
    conn = pymysql.connect(host=os.environ.get('TEST_MYSQL_HOST', '127.0.0.1'),
                           port=int(os.environ.get('TEST_MYSQL_PORT', '3306')),
                           user=os.environ['TEST_MYSQL_USER'],
                           password=os.environ['TEST_MYSQL_PASSWORD'], autocommit=True)
    with conn.cursor() as cursor:
        cursor.execute('CREATE DATABASE ' + name + ' CHARACTER SET utf8mb4')
    try:
        yield config.MemoryMysqlConfig(os.environ.get('TEST_MYSQL_HOST', '127.0.0.1'),
                                       int(os.environ.get('TEST_MYSQL_PORT', '3306')),
                                       os.environ['TEST_MYSQL_USER'], os.environ['TEST_MYSQL_PASSWORD'], name)
    finally:
        with conn.cursor() as cursor:
            cursor.execute('DROP DATABASE ' + name)
        conn.close()


def test_mysql_persists_sessions_and_memories(mysql_config):
    from payback_agent.memory import MemoryStore
    first = MemoryStore(mysql_config)
    first.save_turn('会话', '问题', '答案')
    saved = first.add_memory('使用表格')
    second = MemoryStore(mysql_config)
    assert second.history('会话') == [{'role': 'user', 'content': '问题'},
                                     {'role': 'assistant', 'content': '答案'}]
    assert second.sessions()[0]['id'] == '会话'
    assert second.memories() == [saved]
    assert second.update_memory(saved['id'], '先看风险')
    assert second.memories()[0]['content'] == '先看风险'
    assert second.delete_memory(saved['id'])
    assert second.delete_session('会话')
    assert second.history('会话') == []


def test_sqlite_migration_is_repeatable_and_preserves_source(mysql_config, tmp_path):
    from sqlite_memory import MemoryStore as OldStore
    from payback_agent.memory import MemoryStore, import_sqlite
    path = tmp_path / 'old.sqlite3'
    old = OldStore(path)
    old.save_turn('session-1', '问题', '答案')
    item = old.add_memory('使用表格')
    old.db.close()
    mysql = MemoryStore(mysql_config)
    assert import_sqlite(mysql, path) is True
    assert import_sqlite(mysql, path) is False
    assert mysql.history('session-1')[-1]['content'] == '答案'
    assert mysql.memories() == [item]
    assert path.exists()


def test_sqlite_migration_rejects_nonempty_mysql(mysql_config, tmp_path):
    from sqlite_memory import MemoryStore as OldStore
    from payback_agent.memory import MemoryStore, import_sqlite
    path = tmp_path / 'old.sqlite3'
    old = OldStore(path)
    old.save_turn('old', '旧问题', '旧答案')
    old.db.close()
    mysql = MemoryStore(mysql_config)
    mysql.save_turn('existing', '已有', '保留')
    with pytest.raises(ValueError, match='已有数据'):
        import_sqlite(mysql, path)
    assert mysql.history('old') == []
    assert mysql.history('existing')[-1]['content'] == '保留'


def test_mysql_replays_only_latest_eight_turns(mysql_config):
    from payback_agent.memory import MemoryStore
    store = MemoryStore(mysql_config)
    for i in range(10):
        store.save_turn('session', 'question ' + str(i), 'answer ' + str(i))
    history = MemoryStore(mysql_config).history('session')
    assert len(history) == 16
    assert history[0]['content'] == 'question 2'
    assert history[-1]['content'] == 'answer 9'


def test_mysql_summary_pagination_and_literal_search(mysql_config):
    from payback_agent.memory import MemoryStore
    store = MemoryStore(mysql_config)
    store.save_turn('old', '标题', '正文 %_\\关键词')
    store.save_turn('new', '新标题', '另一段正文')
    page = store.session_page(1, None, '')
    assert page['items'][0]['id'] == 'new'
    assert store.session_page(1, int(page['nextCursor']), '')['items'][0]['id'] == 'old'
    assert store.session_page(30, None, '%_\\')['items'][0]['id'] == 'old'
    assert store.session_detail('old')['messages'][-1]['content'] == '正文 %_\\关键词'
    assert store.session_detail('missing') is None
