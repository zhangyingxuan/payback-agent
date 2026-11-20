"""独立有界研究队列和脱敏证据存储，不占用行情 MCP 的串行 worker。"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .research import ResearchPolicyError
from .research_http import ResearchNetworkError


def _json(value: object) -> str:
    """固定序列化形式用于摘要和原子落盘，不接受非有限数。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class ResearchJobs:
    """可恢复的单写者队列，最多四个活动任务、64 份产物、24 小时留存。"""

    def __init__(self, root: Path, run: Callable, *, sandbox=None, cancellable_run=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._owner = (self.root / '.writer.lock').open('a+b')
        try:
            if self._owner.tell() == 0:
                self._owner.write(b'0')
                self._owner.flush()
            self._owner.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self._owner.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._owner.close()
            raise ValueError('研究目录已被其他运行实例占用，请等待其退出') from None
        self.run = run
        self.sandbox = sandbox
        self.cancellable_run = cancellable_run
        self._lock = threading.RLock()
        self._tasks: dict[str, dict] = {}
        self._pending: queue.Queue = queue.Queue(maxsize=4)
        self._stopped = threading.Event()
        self._closed = False
        try:
            self._prune(reserve=0)
            self._restore()
        except BaseException:
            self._owner.close()
            raise
        self._worker = threading.Thread(target=self._work, name='payback-research', daemon=True)
        self._worker.start()

    def _work(self) -> None:
        """物理队列也有界，反复提交后取消不能积累无限后台工作项。"""
        try:
            while not self._stopped.is_set():
                try:
                    item = self._pending.get(timeout=.1)
                except queue.Empty:
                    continue
                try:
                    self._execute(*item)
                except Exception:
                    print('[research] 单任务异常已隔离，研究 worker 继续运行', file=sys.stderr)
                finally:
                    self._pending.task_done()
        finally:
            # 最后一项执行结束才释放目录锁，防止旧线程与新进程同时写证据。
            self._owner.close()

    def _restore(self) -> None:
        """恢复已完成证据；未完成请求只标记中断，绝不自动重发或重复计费。"""
        for path in self.root.iterdir():
            if not re.fullmatch(r'[0-9a-f]{32}', path.name) or path.is_symlink() or not path.is_dir():
                continue
            try:
                manifest = path / 'manifest.json'
                if manifest.stat().st_size > 16384:
                    continue
                record = json.loads(manifest.read_text(encoding='utf-8'))
                if (record['task_id'] != path.name or record['kind'] not in ('fetch', 'search', 'request', 'python')
                        or record['status'] not in ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'interrupted')
                        or not isinstance(record['created_at'], (int, float))
                        or not re.fullmatch(r'[0-9a-f]{64}', record['input_sha256'])):
                    continue
                if record['status'] in ('queued', 'running'):
                    record.update(status='interrupted', completed_at=time.time(),
                                  error='任务因进程中断未完成，显式恢复将重新校验权限并创建新任务')
                    self._write(path.name, 'manifest.json', record)
                self._tasks[path.name] = {'record': record, 'cancelled': threading.Event()}
            except (OSError, ValueError, KeyError, TypeError):
                continue  # 损坏记录不影响其余证据；保留原文件供管理员检查。

    def _prune(self, keep: set[str] | None = None, *, reserve: int = 1) -> None:
        """只清理专用目录内带 manifest 的随机任务目录，保留所有活动任务。"""
        keep = set(keep or ())
        for task_id, task in self._tasks.items():
            if task['record']['kind'] == 'python' and task['record']['status'] in ('queued', 'running'):
                try:
                    references = self._request(task_id).get('inputs', {})
                    if isinstance(references, dict):
                        keep.update(value for value in references.values() if isinstance(value, str))
                except (OSError, ValueError, TypeError):
                    pass
        candidates = []
        for path in self.root.iterdir():
            if (not re.fullmatch(r'[0-9a-f]{32}', path.name) or path.is_symlink()
                    or not path.is_dir() or not (path / 'manifest.json').is_file()):
                continue
            task = self._tasks.get(path.name)
            if task and task['record']['status'] in ('queued', 'running'):
                continue
            candidates.append((path.stat().st_mtime, path))
        candidates.sort(key=lambda item: item[0])
        total = len(candidates) + sum(task['record']['status'] in ('queued', 'running')
                                      for task in self._tasks.values())
        for modified, path in candidates:
            if path.name in keep:
                continue
            if time.time() - modified < 86400 and total <= 64 - reserve:
                continue
            # 删除前重新确认绝对路径仍在专用根目录内，禁止跟随目录链接。
            if path.resolve().parent != self.root or path.is_symlink():
                continue
            shutil.rmtree(path)
            self._tasks.pop(path.name, None)
            total -= 1

    def _get(self, task_id: str) -> dict:
        """随机 ID 不是用户授权；当前进程内所有权仅适用于可信单用户入口。"""
        if not isinstance(task_id, str) or task_id not in self._tasks:
            raise ValueError('任务不存在、记录损坏或已过期')
        return self._tasks[task_id]

    def _write(self, task_id: str, name: str, value: dict) -> None:
        """先写同目录临时文件再替换，避免读取半份证据。"""
        path = self.root / task_id / name
        temporary = path.with_suffix('.new')
        try:
            temporary.write_text(_json(value), encoding='utf-8')
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def start(self, kind: str, arguments: dict, *, _resumed_from: str | None = None) -> dict:
        """请求和代码明文保存以便显式恢复；不保存宿主注入的供应商凭据。"""
        if kind not in ('fetch', 'search', 'request', 'python') or not isinstance(arguments, dict):
            raise ValueError('研究类型或参数无效')
        if kind == 'python' and self.sandbox is None:
            raise ValueError('未配置固定摘要 Python 沙箱，禁止宿主执行')
        if kind == 'python':
            self._validate_python(arguments)
        try:
            encoded = _json(arguments).encode('utf-8')
        except (TypeError, ValueError):
            raise ValueError('研究参数必须为有效 JSON') from None
        if len(encoded) > (65536 if kind == 'python' else 8192):
            raise ValueError('研究请求超过大小限制')
        with self._lock:
            if self._closed:
                raise ValueError('研究队列已关闭')
            if self._pending.full() or sum(task['record']['status'] in ('queued', 'running')
                                           for task in self._tasks.values()) >= 4:
                raise ValueError('研究队列已满，最多四个活动任务')
            references = arguments.get('inputs', {}) if kind == 'python' else {}
            keep = {value for value in references.values() if isinstance(value, str)} if isinstance(references, dict) else set()
            self._prune(keep)
            task_id = uuid.uuid4().hex
            record = {'task_id': task_id, 'kind': kind, 'status': 'queued',
                      'created_at': time.time(), 'input_sha256': hashlib.sha256(encoded).hexdigest()}
            if _resumed_from:
                record['resumed_from'] = _resumed_from
            (self.root / task_id).mkdir(mode=0o700)
            self._write(task_id, 'request.json', arguments)
            self._write(task_id, 'manifest.json', record)
            task = {'record': record, 'cancelled': threading.Event()}
            self._tasks[task_id] = task
            # 传递 JSON 副本，避免调用方在校验后修改排队参数。
            self._pending.put_nowait((task_id, kind, json.loads(encoded)))
            return dict(record)

    def resume(self, task_id: str) -> dict:
        """显式恢复只重用已校验的输入，供应商权限由执行器重新读取。"""
        with self._lock:
            record = self._get(task_id)['record']
            if record['status'] not in ('failed', 'cancelled', 'interrupted'):
                raise ValueError('仅失败、取消或中断的任务可显式恢复')
            arguments = self._request(task_id)
            return self.start(record['kind'], arguments, _resumed_from=task_id)

    def _request(self, task_id: str) -> dict:
        """恢复与草案导出共用输入摘要校验，不信任磁盘上被改写的代码或参数。"""
        record = self._get(task_id)['record']
        path = self.root / task_id / 'request.json'
        if path.stat().st_size > 65536:
            raise ValueError('任务请求过大')
        arguments = json.loads(path.read_text(encoding='utf-8'))
        if hashlib.sha256(_json(arguments).encode('utf-8')).hexdigest() != record['input_sha256']:
            raise ValueError('任务请求摘要不匹配')
        return arguments

    def _evidence(self, task_id: str) -> dict:
        """计算必须使用完整证据而非聊天分段，读取前校验状态、大小与摘要。"""
        record = self._get(task_id)['record']
        if record['status'] != 'succeeded':
            raise ValueError('任务未成功完成，不能读取结果')
        path = self.root / task_id / 'result.json'
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError('研究产物无效')
        outcome = json.loads(path.read_text(encoding='utf-8'))
        if (record.get('artifact_sha256') and
                hashlib.sha256(_json(outcome).encode('utf-8')).hexdigest() != record['artifact_sha256']):
            raise ValueError('研究产物或来源元数据摘要不匹配')
        if hashlib.sha256(outcome['text'].encode('utf-8')).hexdigest() != record['result_sha256']:
            raise ValueError('研究产物摘要不匹配')
        return outcome

    @staticmethod
    def _validate_python(arguments: dict) -> None:
        """入队前限制依赖数量，避免无效计算请求绕过产物保留上限。"""
        if (set(arguments) != {'code', 'inputs'} or not isinstance(arguments['inputs'], dict)
                or len(arguments['inputs']) > 4 or not isinstance(arguments['code'], str)
                or not arguments['code'].strip() or len(arguments['code'].encode('utf-8')) > 32768):
            raise ResearchPolicyError('Python 需要非空 code（最多32 KiB）及最多四份 inputs 证据')
        for alias, source_id in arguments['inputs'].items():
            if (not isinstance(alias, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,31}', alias)
                    or not isinstance(source_id, str) or not re.fullmatch(r'[0-9a-f]{32}', source_id)):
                raise ResearchPolicyError('证据别名或任务 ID 无效')

    def _compute(self, arguments: dict, cancelled: threading.Event) -> dict:
        """仅允许引用本队列中成功的证据任务，不接受任意宿主路径。"""
        self._validate_python(arguments)
        inputs, references = {}, {}
        with self._lock:
            for alias, source_id in arguments['inputs'].items():
                inputs[alias] = self._evidence(source_id)['text']
                references[alias] = {'task_id': source_id,
                                    'sha256': self._get(source_id)['record']['result_sha256']}
        outcome = self.sandbox.run(arguments['code'], inputs, cancelled)
        outcome['input_evidence'] = references
        return outcome

    def export_tool(self, task_id: str, name: str) -> dict:
        """只导出待审草案，不写正式工具目录、不注册或自动发布。"""
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', name):
            raise ValueError('工具草案名称无效')
        with self._lock:
            record = self._get(task_id)['record']
            if record['kind'] != 'python' or record['status'] != 'succeeded':
                raise ValueError('仅成功的 Python 任务可导出待审草案')
            request, outcome = self._request(task_id), self._evidence(task_id)
            draft = {'status': 'review_required', 'name': name, 'code': request['code'],
                     'input_aliases': list(request['inputs']), 'image': outcome.get('image'),
                     'evidence': outcome.get('input_evidence', {}), 'source_task_id': task_id,
                     'output_sha256': record['result_sha256'], 'domain_verified': False}
            self._write(task_id, 'tool-draft.json', draft)
            return draft

    def _execute(self, task_id: str, kind: str, arguments: dict) -> None:
        """取数在锁外执行，取消状态优先于任何成功结果提交。"""
        try:
            with self._lock:
                task = self._get(task_id)
                if task['cancelled'].is_set():
                    return
                task['record']['status'] = 'running'
                self._write(task_id, 'manifest.json', task['record'])
            if kind == 'python':
                outcome = self._compute(arguments, task['cancelled'])
            elif self.cancellable_run is not None:
                outcome = self.cancellable_run(kind, arguments, task['cancelled'])
            else:
                outcome = self.run(kind, arguments)
            if not isinstance(outcome, dict) or not isinstance(outcome.get('text'), str):
                raise ValueError('研究执行器返回无效结果')
            # 传输或代码执行成功不能自行授予领域验证状态，统一由宿主保持不可信标记。
            outcome = {**outcome, 'domain_verified': False, 'trusted_instructions': False}
            text = outcome['text']
            if len(_json(outcome).encode('utf-8')) > 2 * 1024 * 1024:
                raise ValueError('研究产物过大')
            with self._lock:
                if task['cancelled'].is_set():
                    return
                self._write(task_id, 'result.json', outcome)
                task['record'].update(status='succeeded', completed_at=time.time(),
                    characters=len(text), result_sha256=hashlib.sha256(text.encode('utf-8')).hexdigest(),
                    artifact_sha256=hashlib.sha256(_json(outcome).encode('utf-8')).hexdigest(),
                    domain_verified=False, trusted_instructions=False,
                    source=outcome.get('source'), url=outcome.get('url'))
                self._write(task_id, 'manifest.json', task['record'])
        except Exception as exc:
            with self._lock:
                task = self._tasks.get(task_id)
                if task and not task['cancelled'].is_set():
                    task['record'].update(status='failed', completed_at=time.time(),
                        error=str(exc) if isinstance(exc, (ResearchPolicyError, ResearchNetworkError))
                        else '研究执行失败，请检查配置、网络或证据目录权限')
                    try:
                        (self.root / task_id / 'result.json').unlink(missing_ok=True)
                        self._write(task_id, 'manifest.json', task['record'])
                    except OSError:
                        pass  # 磁盘失败仍保留内存错误状态，不伪称证据已保存。

    def status(self, task_id: str) -> dict:
        """状态不包含大段数据，避免反复轮询把原始结果塞进模型上下文。"""
        with self._lock:
            return dict(self._get(task_id)['record'])

    def result(self, task_id: str, offset: int = 0, limit: int = 6000) -> dict:
        """分段读取校验过摘要的脱敏产物，拒绝未知任务和越界参数。"""
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 6000:
            raise ValueError('结果读取 offset/limit 无效')
        with self._lock:
            record = self._get(task_id)['record']
            outcome = self._evidence(task_id)
            text = outcome['text']
            if offset > len(text):
                raise ValueError('结果 offset 超出范围')
            return {**outcome, 'text': text[offset:offset + limit], 'task_id': task_id,
                    'domain_verified': False, 'trusted_instructions': False,
                    'fetched_at': datetime.fromtimestamp(record['completed_at'], timezone.utc).isoformat(),
                    'result_sha256': record['result_sha256'], 'input_sha256': record['input_sha256'],
                    'offset': offset, 'total_characters': len(text),
                    'next_offset': offset + limit if offset + limit < len(text) else None}

    def cancel(self, task_id: str) -> dict:
        """在途请求不能撤回，取消后丢弃其结果并阻止排队任务启动。"""
        with self._lock:
            task = self._get(task_id)
            if task['record']['status'] in ('queued', 'running'):
                task['cancelled'].set()
                task['record'].update(status='cancelled', completed_at=time.time())
                self._write(task_id, 'manifest.json', task['record'])
            return dict(task['record'])

    def close(self) -> None:
        """关闭后不再接收请求；在途取数自行超时，避免阻塞行情 worker 退出。"""
        with self._lock:
            self._closed = True
            self._stopped.set()
            for task_id in self._tasks:
                try:
                    self.cancel(task_id)
                except OSError:
                    pass  # 磁盘不可写也必须停止接单并取消其余任务。
        if threading.current_thread() is not self._worker:
            self._worker.join(timeout=1)
