"""真实 Podman 冒烟：验证隔离、资源限制、超时、取消和输出上限，不修改业务数据。"""

import argparse
import json
import os
import threading
import time

from payback_agent import research_sandbox
from payback_agent.research import ResearchPolicyError
from payback_agent.research_sandbox import PythonSandbox


def main():
    """镜像必须由管理员预先下载并以不可变摘要传入，脚本不自动联网安装。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    sandbox = PythonSandbox(parser.parse_args().image)
    code = '''import os, socket
checks = {"uid": os.getuid(), "no_host_secret": os.getenv("PAYBACK_SMOKE_SECRET") is None}
try:
    open("/must-not-write", "w").write("unsafe")
    checks["read_only"] = False
except OSError:
    checks["read_only"] = True
try:
    socket.create_connection(("1.1.1.1", 443), timeout=.2).close()
    checks["network_none"] = False
except OSError:
    checks["network_none"] = True
checks["memory_max"] = open("/sys/fs/cgroup/memory.max").read().strip()
checks["pids_max"] = open("/sys/fs/cgroup/pids.max").read().strip()
checks["cpu_max"] = open("/sys/fs/cgroup/cpu.max").read().strip()
checks["no_new_privileges"] = "NoNewPrivs:\\t1" in open("/proc/self/status").read()
result = checks
'''
    original = os.environ.get('PAYBACK_SMOKE_SECRET')
    os.environ['PAYBACK_SMOKE_SECRET'] = 'fake-probe-value-not-a-real-key'
    try:
        checks = json.loads(sandbox.run(code, {}, threading.Event())['text'])
    finally:
        if original is None:
            os.environ.pop('PAYBACK_SMOKE_SECRET', None)
        else:
            os.environ['PAYBACK_SMOKE_SECRET'] = original
    assert checks['uid'] == 65534
    assert all(checks[key] for key in ('no_host_secret', 'read_only', 'network_none', 'no_new_privileges'))
    assert checks['memory_max'] == '134217728' and checks['pids_max'] == '32'
    quota, period = map(int, checks['cpu_max'].split())
    assert quota == period
    print('PASS: non-root / no host credentials / read-only / no network / memory / pids / CPU / no-new-privileges')

    try:
        sandbox.run('print("x" * 300000)', {}, threading.Event())
        raise AssertionError('未拒绝超长输出')
    except ResearchPolicyError as exc:
        assert '限制' in str(exc)
    print('PASS: bounded output')

    original_timeout = research_sandbox.TIMEOUT_SECONDS
    research_sandbox.TIMEOUT_SECONDS = 1
    started = time.monotonic()
    try:
        try:
            sandbox.run('while True: pass', {}, threading.Event())
            raise AssertionError('未终止无限循环')
        except ResearchPolicyError:
            assert time.monotonic() - started < 8
    finally:
        research_sandbox.TIMEOUT_SECONDS = original_timeout
    print('PASS: infinite loop terminated')

    cancelled = threading.Event()
    timer = threading.Timer(.5, cancelled.set)
    timer.start()
    try:
        try:
            sandbox.run('while True: pass', {}, cancelled)
            raise AssertionError('取消后仍返回成功')
        except ResearchPolicyError as exc:
            assert '取消' in str(exc)
    finally:
        timer.cancel()
    print('PASS: cancellation')


if __name__ == '__main__':
    main()
