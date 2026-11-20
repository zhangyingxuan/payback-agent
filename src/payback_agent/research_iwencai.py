"""复用后端爱问财协议及 Node 共享库 createV，读取有界股票名单、总数与解析条件。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

from .research import ResearchPolicyError
from .research_http import fetch

URL = 'https://www.iwencai.com/unifiedwap/unified-wap/v2/result/get-robot-data'
PAGE_URL = 'https://www.iwencai.com/gateway/urp/v7/landing/getDataList'
REQUEST_INTERVAL = 1.2
PARAMETERS = {'question': {'type': 'string', 'required': True, 'max_length': 1000},
              'per_page': {'type': 'integer', 'minimum': 1, 'maximum': 100},
              'max_pages': {'type': 'integer', 'minimum': 1, 'maximum': 20}}


class IwencaiSource:
    """固定上游地址与表单，不向模型开放签名、请求头或任意 Node 命令。"""

    def __init__(self, server_dir: Path | None = None):
        default = Path(__file__).resolve().parents[3] / 'payback' / 'packages' / 'server'
        self.server_dir = Path(server_dir or os.getenv('AGENT_IWENCAI_SERVER_DIR') or default).resolve()
        self.core_module = self.server_dir / 'node_modules' / 'pay-back-core'
        self._lock = threading.Lock()
        self._next_request = 0.0

    @property
    def available(self) -> bool:
        """本地依赖就绪不代表上游一定可用；网络和反爬状态在实际查询时确认。"""
        return bool(shutil.which('node') and (self.core_module / 'lib' / 'index.cjs').is_file())

    def _token(self) -> str:
        """只调用项目已有签名算法，避免另写 Python 版本而与后端产生漂移。"""
        if not self.available:
            raise ResearchPolicyError('爱问财 Node 共享库不可用，请先安装并构建现有 payback 后端依赖')
        try:
            result = subprocess.run([shutil.which('node'), '-e',
                'process.stdout.write(require(process.argv[1]).createV())', str(self.core_module)],
                cwd=self.server_dir, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            token = result.stdout.decode('ascii').strip()
            if result.returncode != 0 or not re.fullmatch(r'[A-Za-z0-9_-]{20,2048}', token):
                raise ValueError
            return token
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise ResearchPolicyError('爱问财签名生成失败，请检查后端共享库构建与 Node 环境') from None

    def descriptor(self) -> dict:
        """描述来自后端源码，不要求不存在的 OpenAPI Key 或猜测专用端点。"""
        return {'id': 'aiwencai', 'label': '爱问财（复用后端协议）', 'domain': 'a-share',
                'documentation': 'payback/packages/server/src/pay-back/core/fetchUtil.ts',
                'configured': self.available, 'origin': 'https://www.iwencai.com',
                'operations': {'query': {'method': 'POST', 'parameters': PARAMETERS}},
                'limitations': '默认首屏，最多20页；返回上游 row_count 与覆盖情况，解析条件须核对。'}

    def _request(self, url: str, form: dict, cancelled) -> dict:
        """分页复用同一限速入口；取消可中断等待，并阻止后续请求。"""
        with self._lock:
            delay = self._next_request - time.monotonic()
            if delay > 0:
                cancelled.wait(delay) if cancelled is not None else time.sleep(delay)
            try:
                if cancelled is not None and cancelled.is_set():
                    raise ResearchPolicyError('爱问财任务已取消，未发起后续请求')
                token = self._token()
                if cancelled is not None and cancelled.is_set():
                    raise ResearchPolicyError('爱问财任务已取消，未发起后续请求')
                response = fetch(url, method='POST', body=urlencode(form).encode('utf-8'), headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
                    'Origin': 'https://www.iwencai.com', 'Referer': 'https://www.iwencai.com/',
                    'Accept': 'application/json, text/plain, */*', 'Accept-Language': 'zh-CN,zh;q=0.9',
                    'Content-Type': 'application/x-www-form-urlencoded', 'hexin-v': token})
                text = response['text'].replace(token, '[REDACTED]')
            finally:
                self._next_request = time.monotonic() + REQUEST_INTERVAL
        try:
            return json.loads(text)
        except ValueError:
            raise ResearchPolicyError('爱问财未返回有效 JSON，未将异常解释为零结果') from None

    @staticmethod
    def _ok_status(value) -> bool:
        """首屏与分页共用业务成功判定，False/null 不能冒充状态码0。"""
        return (type(value) is int and value == 0) or (type(value) is str and value == '0')

    @staticmethod
    def _codes(rows: list) -> set:
        """明细行重复代码不能当作股票数量；结构异常不降级为零。"""
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError
        codes = [row.get('股票代码') or row.get('证券代码') or row.get('code') for row in rows]
        if any(not isinstance(code, str) or not code.strip() for code in codes) or len(set(codes)) != len(codes):
            raise ValueError
        return set(codes)

    def query(self, question: str, per_page: int = 20, max_pages: int = 1, *, cancelled=None) -> dict:
        """使用后端已有两条 API；只报告上游总数和有界名单，不声称统计语义已验证。"""
        if (not isinstance(question, str) or not question.strip() or len(question) > 1000
                or type(per_page) is not int or not 1 <= per_page <= 100
                or type(max_pages) is not int or not 1 <= max_pages <= 20):
            raise ResearchPolicyError('爱问财 question 为1–1000字符，per_page 为1–100整数，max_pages 为1–20整数')
        payload = self._request(URL, {'source': 'Ths_iwencai_Xuangu', 'version': '2.0', 'question': question,
            'perpage': per_page, 'page': 1, 'secondary_intent': 'stock', 'rsh': '',
            'log_info': '{"input_type":"click"}',
            'add_info': '{"urp":{"scene":1,"company":1,"business":1},"contentType":"json","searchInfo":true}'}, cancelled)
        try:
            if not self._ok_status(payload.get('status_code')):
                raise ValueError
            components = [component for answer in payload['data']['answer'] for part in answer['txt']
                          for component in part['content']['components']
                          if isinstance(component.get('data', {}).get('datas'), list)
                          and 'row_count' in component.get('data', {}).get('meta', {}).get('extra', {})]
            if len(components) != 1:
                raise ValueError
            data = components[0]['data']
            rows, extra = data['datas'], data['meta']['extra']
            if not self._ok_status(extra.get('status_code', 0)):
                raise ValueError
            total = extra['row_count']
            if isinstance(total, str) and re.fullmatch(r'[0-9]+', total):
                total = int(total)
            if type(total) is not int or total < len(rows) or total < 0 or not all(isinstance(row, dict) for row in rows):
                raise ValueError
            condition = extra.get('condition')
            parsed = json.loads(condition) if isinstance(condition, str) else condition
            if not isinstance(parsed, (list, dict)) or not parsed:
                raise ValueError
            codes = self._codes(rows)
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ResearchPolicyError('爱问财未返回有效股票筛选条件或表结构异常；不能据此统计股票数量') from None
        pages, pages_requested, stop_reason, paging_error = 1, 1, 'max_pages', None
        for page in range(2, max_pages + 1):
            if cancelled is not None and cancelled.is_set():
                raise ResearchPolicyError('爱问财任务已取消，未发起后续分页')
            if len(rows) >= total:
                break
            component = components[0]
            if component.get('cid') is None or component.get('puuid') is None:
                raise ResearchPolicyError('爱问财缺少分页组件信息，未猜测分页参数')
            pages_requested += 1
            next_payload = self._request(PAGE_URL, {'urp_sort_way': 'desc', 'query': question,
                'query_type': 'stock', 'source': 'Ths_iwencai_Xuangu', 'perpage': per_page, 'page': page,
                'comp_id': component['cid'], 'uuid': component['puuid'],
                'condition': condition if isinstance(condition, str) else json.dumps(condition, ensure_ascii=False)}, cancelled)
            try:
                status_values = [next_payload.get('status_code', 0)] + [
                    item.get('data', {}).get('meta', {}).get('extra', {}).get('status_code', 0)
                    for item in next_payload['answer']['components']]
                errors = [value for value in status_values if not self._ok_status(value)]
                if errors:
                    value = errors[0]
                    paging_error = value if type(value) in (int, str) and re.fullmatch(r'-?[0-9]{1,6}', str(value)) else 'unknown'
                    stop_reason = 'upstream_paging_error'
                    break  # 保留首屏证据，但业务错误不能伪装成合法空页或完整名单。
                candidates = [item['data']['datas'] for item in next_payload['answer']['components']
                              if isinstance(item.get('data', {}).get('datas'), list)]
                if len(candidates) != 1:
                    raise ValueError
                chunk = candidates[0]
                new_codes = self._codes(chunk)
                if codes & new_codes or len(rows) + len(chunk) > total:
                    raise ValueError
            except (ValueError, KeyError, TypeError, AttributeError):
                raise ResearchPolicyError('爱问财分页结构、重复代码或总数不一致，未报告完整名单') from None
            pages = page
            if len(json.dumps(rows + chunk, ensure_ascii=False).encode('utf-8')) > 524288:
                stop_reason = 'size_limit'
                break
            rows.extend(chunk)
            codes.update(new_codes)
            if not chunk:
                stop_reason = 'empty_page'
                break
        if cancelled is not None and cancelled.is_set():
            raise ResearchPolicyError('爱问财任务已取消，丢弃本次结果')
        result = {'question': question, 'reported_total': total, 'returned_count': len(rows),
                  'page': 1, 'per_page': per_page, 'pages_fetched': pages, 'complete': len(rows) == total,
                  'pages_requested': pages_requested, 'paging_error_code': paging_error,
                  'stop_reason': None if len(rows) == total else stop_reason, 'snapshot_verified': False,
                  'parsed_conditions': parsed, 'condition_verified': False,
                  'fields': sorted({key for row in rows for key in row}), 'rows': rows,
                  'limitations': '总数为上游报告值，不是独立复算；complete仅表示名单数量覆盖，需核对解析条件、时点、复权与跨页快照一致性。'}
        return {'source': 'aiwencai', 'url': URL, 'content_type': 'application/json',
                'text': json.dumps(result, ensure_ascii=False, allow_nan=False),
                'trusted_instructions': False, 'domain_verified': False,
                'limitations': result['limitations']}
