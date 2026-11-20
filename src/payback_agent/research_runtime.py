"""研究资源由服务持有，MCP 重连仅借用队列，CLI 则在上下文退出时释放。"""

from __future__ import annotations

import os
from pathlib import Path

from .config import REPO_ROOT
from .research import ResearchBackend, ResearchPolicyError
from .research_jobs import ResearchJobs
from .research_sandbox import PythonSandbox


class ResearchRuntime:
    """一个服务生命周期只创建一个研究执行队列。"""

    def __init__(self, backend: ResearchBackend, jobs: ResearchJobs):
        self.backend, self.jobs = backend, jobs

    def close(self):
        """仅由持有者调用，取消执行并在 worker 退出后释放证据目录锁。"""
        self.jobs.close()


def create_research_runtime() -> ResearchRuntime | None:
    """默认关闭；固定镜像缺失时仅禁用 Python，不降级到宿主解释器。"""
    if os.getenv('AGENT_RESEARCH_ENABLED', '').lower() not in ('1', 'true'):
        return None
    backend = ResearchBackend(os.getenv('AGENT_RESEARCH_SOURCES') or None)
    image = os.getenv('AGENT_RESEARCH_PYTHON_IMAGE', '').strip()
    sandbox, error = None, None
    if image:
        try:
            sandbox = PythonSandbox(image)
        except ResearchPolicyError as exc:
            error = str(exc)
    jobs = ResearchJobs(Path(os.getenv('AGENT_RESEARCH_DIR') or REPO_ROOT / 'data' / 'research'),
                        backend.run, sandbox=sandbox, cancellable_run=backend.run_cancellable)
    jobs.sandbox_error = error
    return ResearchRuntime(backend, jobs)
