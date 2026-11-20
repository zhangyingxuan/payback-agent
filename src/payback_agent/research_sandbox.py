"""固定镜像的 rootless Podman Python 执行器，禁止网络、宿主挂载和宿主回退。"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import threading
import time
import uuid

from .research import ResearchPolicyError

TIMEOUT_SECONDS = 20
MAX_OUTPUT = 262144

# 用户代码仅通过标准输入传入；隔离解释器不加载 site、用户配置或宿主 Python 包。
_BOOTSTRAP = '''import json, sys, os
status = dict(line.split(":", 1) for line in open("/proc/self/status") if ":" in line)
if (os.geteuid() != 65534 or status.get("Seccomp", "").strip() != "2"
        or status.get("NoNewPrivs", "").strip() != "1"
        or int(status.get("CapEff", "-1").strip(), 16) != 0):
    raise RuntimeError("sandbox isolation check failed")
memory = int(open("/sys/fs/cgroup/memory.max").read())
pids = int(open("/sys/fs/cgroup/pids.max").read())
quota, period = map(int, open("/sys/fs/cgroup/cpu.max").read().split())
if not (0 < memory <= 134217728 and 0 < pids <= 32 and 0 < quota <= period):
    raise RuntimeError("sandbox resource isolation check failed")
p = json.load(sys.stdin)
scope = {"inputs": p["inputs"]}
try:
    exec(compile(p["code"], "research.py", "exec"), scope)
    json.dump({"result": scope["result"]}, sys.stdout, ensure_ascii=False, allow_nan=False)
except Exception as error:
    import traceback
    frames = traceback.extract_tb(error.__traceback__)
    line = getattr(error, "lineno", None)
    for frame in frames:
        if frame.filename == "research.py":
            line = frame.lineno
    sys.stderr.write("\\nPAYBACK_CODE_ERROR:" + json.dumps({"type": type(error).__name__,
        "line": line, "message": str(error)[:500]}, ensure_ascii=True) + "\\n")
    raise SystemExit(2)
'''


def _reject_constant(value):
    """Python JSON 默认接受 NaN/Infinity，证据输出必须显式拒绝。"""
    raise ValueError('non-finite')


class PythonSandbox:
    """只接受管理员固定摘要镜像，所有资源和网络选项由宿主指定。"""

    def __init__(self, image: str):
        if not isinstance(image, str) or not re.fullmatch(
                r'(?:sha256:[0-9a-f]{64}|[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64})', image):
            raise ResearchPolicyError('Python 沙箱必须配置不可变镜像摘要，禁止可变 tag')
        self.image = image

    def run(self, code: str, inputs: dict, cancelled: threading.Event) -> dict:
        """输入在内存中传入，独立线程有界读取输出，超时/取消时清理远端容器。"""
        if not isinstance(code, str) or not code.strip() or len(code.encode('utf-8')) > 32768:
            raise ResearchPolicyError('Python 代码必须为非空文本且不超过 32 KiB')
        if not isinstance(inputs, dict):
            raise ResearchPolicyError('Python inputs 必须为已验证证据对象')
        payload = json.dumps({'code': code, 'inputs': inputs}, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(payload) > 1024 * 1024:
            raise ResearchPolicyError('沙箱输入超过 1 MiB 限制')
        if cancelled.is_set():
            raise ResearchPolicyError('任务已取消')
        name = 'payback-research-' + uuid.uuid4().hex
        command = ['podman', 'run', '--rm', '--name=' + name, '--label=payback.research=true',
                   '--pull=never', '--network=none', '--read-only', '--read-only-tmpfs=false',
                   '--pid=private', '--ipc=private', '--uts=private', '--cgroupns=private',
                   '--user=65534:65534', '--cap-drop=all', '--security-opt=no-new-privileges',
                   '--pids-limit=32', '--memory=128m', '--memory-swap=128m', '--cpus=1',
                   '--ulimit=cpu=10:10', '--ulimit=nofile=64:64', '--ulimit=core=0:0',
                   '--log-driver=none', '--timeout=' + str(max(1, math.ceil(TIMEOUT_SECONDS))),
                   '--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777', '--workdir=/tmp',
                   '--unsetenv-all', '--entrypoint=/usr/local/bin/python', '-i', self.image,
                   '-I', '-S', '-u', '-c', _BOOTSTRAP]
        process = None
        threads = []
        streams = {'stdout': bytearray(), 'stderr': bytearray()}
        overflow, writing_failed = threading.Event(), threading.Event()
        lock = threading.Lock()

        def read_stream(stream, key):
            """不使用 communicate，避免无限输出先耗尽宿主内存才检查长度。"""
            try:
                while chunk := stream.read(4096):
                    with lock:
                        if sum(map(len, streams.values())) + len(chunk) > MAX_OUTPUT:
                            overflow.set()
                            return
                        streams[key].extend(chunk)
            except (OSError, ValueError):
                writing_failed.set()

        def write_input():
            """写入也不能阻塞监控线程；容器不读 stdin 时仍可超时取消。"""
            try:
                view = memoryview(payload)
                while view:
                    written = process.stdin.write(view)
                    if not written:
                        raise OSError
                    view = view[written:]
            except (OSError, ValueError):
                writing_failed.set()
            finally:
                try:
                    process.stdin.close()
                except OSError:
                    pass

        try:
            info = subprocess.run(['podman', 'info', '--format', 'json'],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            try:
                host = json.loads(info.stdout)['host'] if info.returncode == 0 else {}
                secure = (host.get('security', {}).get('rootless') is True
                          and host.get('security', {}).get('seccompEnabled') is True
                          and host.get('cgroupVersion') == 'v2'
                          and {'cpu', 'memory', 'pids'} <= set(host.get('cgroupControllers', [])))
            except (ValueError, KeyError, TypeError, AttributeError):
                secure = False
            if not secure:
                raise ResearchPolicyError('沙箱要求 rootless、cgroup v2 和 seccomp，当前引擎不满足隔离要求')
            if cancelled.is_set():
                raise ResearchPolicyError('Python 任务已取消，未启动容器')
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, bufsize=0,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            for target, args in ((read_stream, (process.stdout, 'stdout')),
                                 (read_stream, (process.stderr, 'stderr')), (write_input, ())):
                thread = threading.Thread(target=target, args=args, daemon=True)
                thread.start()
                threads.append(thread)
            deadline = time.monotonic() + TIMEOUT_SECONDS
            while process.poll() is None:
                if cancelled.is_set():
                    raise ResearchPolicyError('Python 任务已取消')
                if overflow.is_set():
                    raise ResearchPolicyError('沙箱输出超过 256 KiB 限制')
                if time.monotonic() >= deadline:
                    raise ResearchPolicyError('Python 沙箱执行超时')
                time.sleep(.02)
            for thread in threads:
                thread.join(timeout=1)
            if cancelled.is_set():
                raise ResearchPolicyError('Python 任务已取消')
            if overflow.is_set():
                raise ResearchPolicyError('沙箱输出超过 256 KiB 限制')
            if process.returncode == 2:
                # 仅回传容器内代码的有界诊断，隐藏引擎原始报错和宿主路径。
                for line in streams['stderr'].decode('utf-8', errors='replace').splitlines():
                    if line.startswith('PAYBACK_CODE_ERROR:'):
                        try:
                            error = json.loads(line.removeprefix('PAYBACK_CODE_ERROR:'))
                            kind, number = error['type'], error['line']
                            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', kind):
                                break
                            if number is not None and (type(number) is not int or not 1 <= number <= 32768):
                                break
                            message = ''.join(c for c in str(error['message'])[:500] if ord(c) >= 32)
                        except (ValueError, KeyError, TypeError):
                            break
                        raise ResearchPolicyError(f'隔离 Python {kind}，第 {number} 行：{message}')
            if process.returncode != 0 or writing_failed.is_set() or any(t.is_alive() for t in threads):
                raise ResearchPolicyError('容器或 Python 执行失败；请检查镜像、安全选项和代码，结果须赋值给 result')
            try:
                output = json.loads(streams['stdout'].decode('utf-8'), parse_constant=_reject_constant)
                if not isinstance(output, dict) or set(output) != {'result'}:
                    raise ValueError
                text = json.dumps(output['result'], ensure_ascii=False, allow_nan=False)
            except (UnicodeError, ValueError, TypeError):
                raise ResearchPolicyError('沙箱必须输出唯一有效 JSON 结果，请勿额外 print 或返回非有限数') from None
            return {'text': text, 'source': 'python-sandbox', 'content_type': 'application/json',
                    'image': self.image, 'domain_verified': False, 'trusted_instructions': False,
                    'limitations': '代码已隔离执行；统计口径和输入数据真实性仍需独立验证。'}
        except (OSError, subprocess.TimeoutExpired):
            raise ResearchPolicyError('Podman 不可用或容器启动失败，禁止降级为宿主 Python') from None
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                # --timeout 在容器端兜底；不能只结束 CLI 而遗留容器内无限循环。
                try:
                    subprocess.run(['podman', 'rm', '--force', name], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=5,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                process.wait(timeout=5)
                for thread in threads:
                    thread.join(timeout=1)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if not stream.closed:
                        stream.close()
