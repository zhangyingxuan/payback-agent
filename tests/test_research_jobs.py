"""研究任务测试：独立运行、有界排队、取消和证据读取。"""

import json
import threading
import time

import pytest

from payback_agent.research_jobs import ResearchJobs


def wait_done(jobs, task_id):
    """只在测试中短轮询，生产工具不阻塞等待后台任务。"""
    for _ in range(100):
        result = jobs.status(task_id)
        if result['status'] not in ('queued', 'running'):
            return result
        time.sleep(.01)
    pytest.fail('研究任务未结束')


def test_submission_returns_before_work_and_queue_is_bounded(tmp_path):
    gate, started = threading.Event(), threading.Event()

    def run(kind, arguments):
        started.set()
        gate.wait(2)
        return {'text': 'done', 'domain_verified': False}

    jobs = ResearchJobs(tmp_path, run)
    try:
        first = jobs.start('fetch', {'url': 'https://example.com'})
        assert started.wait(1)
        assert jobs.status(first['task_id'])['status'] == 'running'
        queued = [jobs.start('fetch', {}) for _ in range(3)]
        assert jobs.status(queued[-1]['task_id'])['status'] == 'queued'
        with pytest.raises(ValueError, match='队列'):
            jobs.start('fetch', {})
        jobs.cancel(queued[0]['task_id'])
        gate.set()
        assert wait_done(jobs, first['task_id'])['status'] == 'succeeded'
    finally:
        gate.set()
        jobs.close()


def test_result_persists_provenance_and_supports_bounded_reads(tmp_path):
    jobs = ResearchJobs(tmp_path, lambda kind, args: {'text': 'x' * 9000, 'domain_verified': False})
    try:
        task = jobs.start('fetch', {'url': 'https://example.com'})
        result = wait_done(jobs, task['task_id'])
        assert result['characters'] == 9000
        assert result['domain_verified'] is False
        assert result['input_sha256'] and result['result_sha256']
        assert 'text' not in result
        part = jobs.result(task['task_id'], offset=0, limit=6000)
        assert part['fetched_at'].endswith('+00:00')
        assert part['result_sha256'] == result['result_sha256']
        assert len(part['text']) == 6000
        assert part['next_offset'] == 6000
        assert jobs.result(task['task_id'], offset=6000, limit=6000)['next_offset'] is None
        manifest = json.loads((tmp_path / task['task_id'] / 'manifest.json').read_text(encoding='utf-8'))
        assert manifest['status'] == 'succeeded'
        assert manifest['created_at'] <= manifest['completed_at']
        assert not list(tmp_path.rglob('*.new'))
    finally:
        jobs.close()


def test_cancelling_running_task_never_publishes_result(tmp_path):
    gate, started = threading.Event(), threading.Event()

    def run(kind, args):
        started.set()
        gate.wait(2)
        return {'text': 'must not publish'}

    jobs = ResearchJobs(tmp_path, run)
    try:
        task = jobs.start('fetch', {})
        assert started.wait(1)
        jobs.cancel(task['task_id'])
        gate.set()
        assert jobs.status(task['task_id'])['status'] == 'cancelled'
        with pytest.raises(ValueError, match='未成功'):
            jobs.result(task['task_id'])
        jobs.close()
        assert not (tmp_path / task['task_id'] / 'result.json').exists()
    finally:
        gate.set()
        jobs.close()


@pytest.mark.parametrize('task_id', ['../secret', 'a' * 32, '/tmp/file', ''])
def test_cannot_read_foreign_or_traversal_task(tmp_path, task_id):
    jobs = ResearchJobs(tmp_path, lambda *args: {})
    try:
        with pytest.raises(ValueError, match='不存在'):
            jobs.status(task_id)
    finally:
        jobs.close()


def test_unexpected_exception_does_not_persist_raw_exception(tmp_path):
    def run(*args):
        raise RuntimeError('Authorization: private credential')

    jobs = ResearchJobs(tmp_path, run)
    try:
        task = jobs.start('fetch', {})
        result = wait_done(jobs, task['task_id'])
        assert result['status'] == 'failed'
        assert 'credential' not in json.dumps(result)
        assert 'credential' not in (tmp_path / task['task_id'] / 'manifest.json').read_text(encoding='utf-8')
    finally:
        jobs.close()


def test_rejects_unknown_kind_bad_payload_and_read_bounds(tmp_path):
    jobs = ResearchJobs(tmp_path, lambda *args: {'text': 'data'})
    try:
        for kind, args in [('python', {}), ('fetch', []), ('fetch', {'x': 'x' * 9000})]:
            with pytest.raises(ValueError):
                jobs.start(kind, args)
        task = jobs.start('fetch', {})
        wait_done(jobs, task['task_id'])
        for offset, limit in [(-1, 10), (0, 6001), (True, 10), (0, 0), (0, True)]:
            with pytest.raises(ValueError):
                jobs.result(task['task_id'], offset, limit)
    finally:
        jobs.close()
    with pytest.raises(ValueError, match='关闭'):
        jobs.start('fetch', {})


def test_retention_removes_old_managed_tasks_only(tmp_path):
    import os
    old = tmp_path / ('a' * 32)
    old.mkdir()
    (old / 'manifest.json').write_text('{}')
    os.utime(old, (1, 1))
    keep = tmp_path / 'user-files'
    keep.mkdir()
    (keep / 'file.txt').write_text('keep')
    jobs = ResearchJobs(tmp_path, lambda *args: {'text': 'x'})
    try:
        assert not old.exists()
        assert (keep / 'file.txt').read_text() == 'keep'
        for _ in range(66):
            task = jobs.start('fetch', {})
            wait_done(jobs, task['task_id'])
        assert len(list(tmp_path.glob('*/manifest.json'))) <= 64
    finally:
        jobs.close()


def test_worker_survives_result_cleanup_permission_error(tmp_path, monkeypatch):
    """异常产物删不掉也不能杀死唯一 worker，后续任务仍应正常结束。"""
    from pathlib import Path
    original = Path.unlink

    def locked(path, *args, **kwargs):
        if path.name == 'result.json':
            raise PermissionError('locked evidence file')
        return original(path, *args, **kwargs)

    def run(kind, args):
        if args.get('fail'):
            raise RuntimeError('fake upstream failure')
        return {'text': 'next job succeeds'}

    jobs = ResearchJobs(tmp_path, run)
    monkeypatch.setattr(Path, 'unlink', locked)
    try:
        first = jobs.start('fetch', {'fail': True})
        assert wait_done(jobs, first['task_id'])['status'] == 'failed'
        second = jobs.start('fetch', {})
        assert wait_done(jobs, second['task_id'])['status'] == 'succeeded'
    finally:
        jobs.close()


def test_cancelled_queue_items_remain_physically_bounded(tmp_path):
    gate, started = threading.Event(), threading.Event()

    def run(kind, args):
        started.set()
        gate.wait(2)
        return {'text': 'done'}

    jobs = ResearchJobs(tmp_path, run)
    try:
        jobs.start('fetch', {})
        assert started.wait(1)
        for _ in range(4):
            queued = jobs.start('fetch', {})
            jobs.cancel(queued['task_id'])
        with pytest.raises(ValueError, match='队列'):
            jobs.start('fetch', {})
    finally:
        gate.set()
        jobs.close()


def test_completed_evidence_survives_runtime_restart(tmp_path):
    first = ResearchJobs(tmp_path, lambda *args: {'text': 'retained evidence'})
    task = first.start('fetch', {'url': 'https://example.com'})
    wait_done(first, task['task_id'])
    first.close()
    second = ResearchJobs(tmp_path, lambda *args: pytest.fail('恢复结果不得重发请求'))
    try:
        assert second.status(task['task_id'])['status'] == 'succeeded'
        assert second.result(task['task_id'])['text'] == 'retained evidence'
    finally:
        second.close()


def test_interrupted_tasks_require_explicit_resume_and_recheck_policy(tmp_path):
    import hashlib
    task_id = 'b' * 32
    directory = tmp_path / task_id
    directory.mkdir()
    request = {'url': 'https://example.com'}
    encoded = json.dumps(request, ensure_ascii=False, sort_keys=True).encode('utf-8')
    (directory / 'request.json').write_bytes(encoded)
    (directory / 'manifest.json').write_text(json.dumps({
        'task_id': task_id, 'kind': 'fetch', 'status': 'running', 'created_at': time.time(),
        'input_sha256': hashlib.sha256(encoded).hexdigest()}), encoding='utf-8')
    calls = []
    jobs = ResearchJobs(tmp_path, lambda kind, args: calls.append((kind, args)) or {'text': 'new result'})
    try:
        assert jobs.status(task_id)['status'] == 'interrupted'
        assert calls == []
        resumed = jobs.resume(task_id)
        assert resumed['task_id'] != task_id
        assert wait_done(jobs, resumed['task_id'])['resumed_from'] == task_id
        assert calls == [('fetch', request)]
        with pytest.raises(ValueError, match='摘要'):
            (directory / 'request.json').write_text('{}', encoding='utf-8')
            jobs.resume(task_id)
    finally:
        jobs.close()


def test_one_writer_owns_research_directory(tmp_path):
    jobs = ResearchJobs(tmp_path, lambda *args: {'text': 'data'})
    try:
        with pytest.raises(ValueError, match='占用'):
            ResearchJobs(tmp_path, lambda *args: {})
    finally:
        jobs.close()


def test_corrupt_persisted_records_do_not_break_startup(tmp_path):
    directory = tmp_path / ('c' * 32)
    directory.mkdir()
    (directory / 'manifest.json').write_text('not json', encoding='utf-8')
    jobs = ResearchJobs(tmp_path, lambda *args: {'text': 'ok'})
    try:
        task = jobs.start('fetch', {})
        assert wait_done(jobs, task['task_id'])['status'] == 'succeeded'
        with pytest.raises(ValueError):
            jobs.status('c' * 32)
    finally:
        jobs.close()


def test_python_uses_complete_verified_evidence_and_exports_review_draft(tmp_path):
    captured = []

    class Sandbox:
        def run(self, code, inputs, cancelled):
            captured.append((code, inputs))
            return {'text': '{"count":2}', 'source': 'python-sandbox', 'image': 'sha256:' + 'a' * 64}

    jobs = ResearchJobs(tmp_path, lambda *args: {'text': 'x' * 9000}, sandbox=Sandbox())
    try:
        source = jobs.start('fetch', {})
        wait_done(jobs, source['task_id'])
        code = 'result = {"count": 2}'
        task = jobs.start('python', {'code': code, 'inputs': {'prices': source['task_id']}})
        assert wait_done(jobs, task['task_id'])['status'] == 'succeeded'
        assert len(captured[0][1]['prices']) == 9000
        result = jobs.result(task['task_id'])
        assert result['input_evidence']['prices']['task_id'] == source['task_id']
        draft = jobs.export_tool(task['task_id'], 'compare_prices')
        assert draft['status'] == 'review_required' and draft['code'] == code
        assert draft['domain_verified'] is False
        assert (tmp_path / task['task_id'] / 'tool-draft.json').is_file()
    finally:
        jobs.close()


def test_python_rejects_tampered_inputs_before_container_start(tmp_path):
    class Sandbox:
        def run(self, *args):
            pytest.fail('篡改证据不得进入容器')

    jobs = ResearchJobs(tmp_path, lambda *args: {'text': 'original'}, sandbox=Sandbox())
    try:
        source = jobs.start('fetch', {})
        wait_done(jobs, source['task_id'])
        (tmp_path / source['task_id'] / 'result.json').write_text('{"text":"tampered"}', encoding='utf-8')
        task = jobs.start('python', {'code': 'result = {}', 'inputs': {'data': source['task_id']}})
        assert wait_done(jobs, task['task_id'])['status'] == 'failed'
    finally:
        jobs.close()


def test_retention_keeps_inputs_of_new_python_task(tmp_path):
    class Sandbox:
        def run(self, code, inputs, cancelled):
            return {'text': inputs['old']}

    jobs = ResearchJobs(tmp_path, lambda *args: {'text': 'evidence'}, sandbox=Sandbox())
    try:
        first = jobs.start('fetch', {})
        wait_done(jobs, first['task_id'])
        for _ in range(63):
            task = jobs.start('fetch', {})
            wait_done(jobs, task['task_id'])
        computation = jobs.start('python', {'code': 'result = inputs["old"]', 'inputs': {'old': first['task_id']}})
        assert wait_done(jobs, computation['task_id'])['status'] == 'succeeded'
    finally:
        jobs.close()


def test_invalid_python_arguments_cannot_pin_unbounded_artifacts(tmp_path):
    class Sandbox:
        def run(self, *args):
            pytest.fail('无效参数不能进入容器')

    jobs = ResearchJobs(tmp_path, lambda *args: {'text': 'data'}, sandbox=Sandbox())
    try:
        for arguments in ({'code': 'result = {}', 'inputs': {str(i): 'a' * 32 for i in range(65)}},
                          {'code': '', 'inputs': {}}, {'code': 'x' * 32769, 'inputs': {}},
                          {'code': 'result = {}', 'inputs': {'x': '../file'}}):
            with pytest.raises(ValueError):
                jobs.start('python', arguments)
        assert not list(tmp_path.glob('*/manifest.json'))
    finally:
        jobs.close()


def test_artifact_metadata_is_bound_to_evidence_digest(tmp_path):
    jobs = ResearchJobs(tmp_path, lambda *args: {'text': 'data', 'source': 'original'})
    try:
        task = jobs.start('fetch', {})
        wait_done(jobs, task['task_id'])
        path = tmp_path / task['task_id'] / 'result.json'
        path.write_text('{"text":"data","source":"forged"}', encoding='utf-8')
        with pytest.raises(ValueError, match='摘要'):
            jobs.result(task['task_id'])
    finally:
        jobs.close()


def test_restart_at_retention_limit_preserves_resumable_inputs(tmp_path):
    import hashlib
    class Sandbox:
        def run(self, code, inputs, cancelled):
            return {'text': inputs['prices']}

    first = ResearchJobs(tmp_path, lambda *args: {'text': 'retained'}, sandbox=Sandbox())
    ids = []
    for _ in range(64):
        task = first.start('fetch', {})
        wait_done(first, task['task_id'])
        ids.append(task['task_id'])
    first.close()
    request = {'code': 'result = inputs["prices"]', 'inputs': {'prices': ids[0]}}
    path = tmp_path / ids[-1]
    encoded = json.dumps(request, ensure_ascii=False, sort_keys=True).encode('utf-8')
    (path / 'request.json').write_bytes(encoded)
    record = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
    record.update(kind='python', status='queued', input_sha256=hashlib.sha256(encoded).hexdigest())
    (path / 'manifest.json').write_text(json.dumps(record), encoding='utf-8')
    second = ResearchJobs(tmp_path, lambda *args: {}, sandbox=Sandbox())
    try:
        assert second.status(ids[0])['status'] == 'succeeded'
        assert second.status(ids[-1])['status'] == 'interrupted'
        resumed = second.resume(ids[-1])
        assert wait_done(second, resumed['task_id'])['status'] == 'succeeded'
    finally:
        second.close()


def test_cancellable_network_runner_receives_task_event(tmp_path):
    captured = []

    def run(kind, arguments, cancelled):
        captured.append(cancelled)
        return {'text': 'network data'}

    jobs = ResearchJobs(tmp_path, lambda *args: pytest.fail('不能绕过可取消执行器'), cancellable_run=run)
    try:
        task = jobs.start('request', {})
        assert wait_done(jobs, task['task_id'])['status'] == 'succeeded'
        assert isinstance(captured[0], threading.Event)
    finally:
        jobs.close()
