"""将旧 SQLite 记忆安全导入空 MySQL，保留原数据库文件。"""

import argparse
import os
import sys
from pathlib import Path

from . import config
from .memory import MemoryStore, import_sqlite


def main() -> int:
    parser = argparse.ArgumentParser(description='将 SQLite 会话和长期记忆导入空 MySQL')
    parser.add_argument('source', nargs='?', default=None, help='旧 SQLite 数据库路径')
    args = parser.parse_args()
    config.load_env()
    source = args.source or os.environ.get('AGENT_MEMORY_PATH') or config.REPO_ROOT / 'data' / 'memory.sqlite3'
    try:
        imported = import_sqlite(MemoryStore(), Path(source))
    except (config.ConfigError, ValueError) as exc:
        print(f'迁移失败（源文件未修改）：{exc}', file=sys.stderr)
        return 1
    except Exception as exc:
        print(f'迁移失败（源文件未修改）：{type(exc).__name__}', file=sys.stderr)
        return 1
    print('迁移完成，旧 SQLite 文件未修改' if imported else '此快照已迁移，未重复导入')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
