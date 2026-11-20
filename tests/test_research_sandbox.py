"""容器执行边界测试：命令固定、输入走 stdin、输出有界且无宿主回退。"""

import io
import json
import threading
from types import SimpleNamespace

import pytest

from payback_agent.research_sandbox import PythonSandbox

IMAGE = 'sha256:' + 'a' * 64


@pytest.mark.parametrize('image', ['', 'python:latest', '--privileged', 'python:3.12-slim'])
def test_requires_immutable_image(image):
    with pytest.raises(ValueError, match='摘要'):
        PythonSandbox(image)


def fake_process(monkeypatch, output=b'{"result":{"count":2}}', *, running=False, error=b'', returncode=0):
    """假容器进程不执行用户脚本，记录 stdin 和清理命令。"""
    calls = []

    class Input(io.BytesIO):
        def close(self):
            calls.append(('stdin', self.getvalue()))
            super().close()

    class Process:
        stdin = Input()
        stdout = io.BytesIO(output)
        stderr = io.BytesIO(error)

        def poll(self):
            return None if running else returncode

        def kill(self):
            calls.append('kill')

        def wait(self, timeout=None):
            return 0

    def popen(command, **kwargs):
        calls.append((command, kwargs))
        process = Process()
        process.returncode = returncode
        return process

    monkeypatch.setattr('payback_agent.research_sandbox.subprocess.Popen', popen)
    def run(command, **kwargs):
        if command[1] == 'info':
            return SimpleNamespace(returncode=0, stdout=json.dumps({'host': {
                'security': {'rootless': True, 'seccompEnabled': True},
                'cgroupVersion': 'v2', 'cgroupControllers': ['cpu', 'memory', 'pids']}}).encode())
        calls.append(('cleanup', command))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr('payback_agent.research_sandbox.subprocess.run', run)
    return calls


def test_fixed_isolation_flags_and_no_credentials_or_code_in_argv(monkeypatch):
    calls = fake_process(monkeypatch)
    result = PythonSandbox(IMAGE).run('result = {"count": 2}', {'prices': '[]'}, threading.Event())
    command = calls[0][0]
    for flag in ('--network=none', '--read-only', '--user=65534:65534', '--cap-drop=all',
                 '--security-opt=no-new-privileges', '--pids-limit=32', '--memory=128m', '--pull=never',
                 '--pid=private', '--ipc=private', '--uts=private', '--cgroupns=private'):
        assert flag in command
    assert not any(arg.startswith(('--volume', '--mount', '--privileged', '--env=')) for arg in command)
    assert 'result = {"count": 2}' not in ' '.join(command)
    payload = json.loads(next(item[1] for item in calls if isinstance(item, tuple) and item[0] == 'stdin'))
    assert payload['inputs']['prices'] == '[]'
    assert json.loads(result['text']) == {'count': 2}
    assert result['domain_verified'] is False
    assert any(isinstance(item, tuple) and item[0] == 'cleanup' for item in calls)


@pytest.mark.parametrize('output', [b'not-json', b'{"result":NaN}', b'{}', b'x' * 262145],
                         ids=['invalid-json', 'non-finite', 'missing-result', 'overflow'])
def test_invalid_or_excessive_output_is_rejected(monkeypatch, output):
    fake_process(monkeypatch, output)
    with pytest.raises(ValueError):
        PythonSandbox(IMAGE).run('result = {}', {}, threading.Event())


def test_timeout_kills_container_and_cli(monkeypatch):
    calls = fake_process(monkeypatch, running=True)
    monkeypatch.setattr('payback_agent.research_sandbox.TIMEOUT_SECONDS', .05)
    with pytest.raises(ValueError, match='超时'):
        PythonSandbox(IMAGE).run('while True: pass', {}, threading.Event())
    assert 'kill' in calls
    assert any(isinstance(item, tuple) and item[0] == 'cleanup' for item in calls)


def test_cancelled_task_never_starts_container(monkeypatch):
    monkeypatch.setattr('payback_agent.research_sandbox.subprocess.Popen',
                        lambda *args, **kwargs: pytest.fail('已取消任务不应启动容器'))
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(ValueError, match='取消'):
        PythonSandbox(IMAGE).run('result = {}', {}, cancelled)


def test_missing_engine_has_no_host_python_fallback(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError('podman unavailable')
    monkeypatch.setattr('payback_agent.research_sandbox.subprocess.Popen', missing)
    monkeypatch.setattr('payback_agent.research_sandbox.subprocess.run', missing)
    with pytest.raises(ValueError, match='Podman'):
        PythonSandbox(IMAGE).run('result = {}', {}, threading.Event())


def test_rootful_or_missing_cgroup_isolation_fails_closed(monkeypatch):
    monkeypatch.setattr('payback_agent.research_sandbox.subprocess.run', lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout=b'false v1 false'))
    monkeypatch.setattr('payback_agent.research_sandbox.subprocess.Popen', lambda *args, **kwargs:
                        pytest.fail('不安全引擎不得启动容器'))
    with pytest.raises(ValueError, match='rootless'):
        PythonSandbox(IMAGE).run('result = {}', {}, threading.Event())


def test_cancel_during_preflight_does_not_start_container(monkeypatch):
    cancelled = threading.Event()

    def info(*args, **kwargs):
        cancelled.set()
        return SimpleNamespace(returncode=0, stdout=json.dumps({'host': {
            'security': {'rootless': True, 'seccompEnabled': True},
            'cgroupVersion': 'v2', 'cgroupControllers': ['cpu', 'memory', 'pids']}}).encode())

    monkeypatch.setattr('payback_agent.research_sandbox.subprocess.run', info)
    monkeypatch.setattr('payback_agent.research_sandbox.subprocess.Popen', lambda *args, **kwargs:
                        pytest.fail('预检取消后不得新建容器'))
    with pytest.raises(ValueError, match='取消'):
        PythonSandbox(IMAGE).run('result = {}', {}, cancelled)


def test_bootstrap_rejects_container_without_actual_seccomp(monkeypatch):
    import os
    from payback_agent.research_sandbox import _BOOTSTRAP
    monkeypatch.setattr(os, 'geteuid', lambda: 65534, raising=False)
    monkeypatch.setattr('builtins.open', lambda *args, **kwargs:
                        io.StringIO('Seccomp:\t0\nNoNewPrivs:\t1\nCapEff:\t00000000\n'))
    # 仅执行可信前置检查，不在宿主执行模型生成代码。
    guard = _BOOTSTRAP.split('p = json.load(sys.stdin)')[0]
    with pytest.raises(RuntimeError, match='isolation'):
        exec(compile(guard, 'trusted-sandbox-guard', 'exec'), {})


def test_safe_code_diagnostic_supports_model_repair(monkeypatch):
    fake_process(monkeypatch, b'', returncode=2,
                 error=b'PAYBACK_CODE_ERROR:{"type":"SyntaxError","line":1,"message":"invalid syntax"}\n')
    with pytest.raises(ValueError, match='SyntaxError.*1.*invalid syntax'):
        PythonSandbox(IMAGE).run('result =', {}, threading.Event())
