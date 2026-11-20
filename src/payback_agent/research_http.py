"""研究取数的 HTTPS 边界：固定公网 IP 连接，按原域名验证 TLS，不跟随重定向。"""

from __future__ import annotations

import http.client
import ipaddress
import io
import socket
import ssl
import threading
import time
from email.message import Message
from urllib.parse import urlsplit

MAX_BYTES = 262144
REQUEST_SECONDS = 20


class ResearchNetworkError(ValueError):
    """可向模型返回的脱敏网络错误。"""


class _DeadlineReader(io.RawIOBase):
    """每次底层接收前检查绝对期限，响应头和 chunk 元数据也不能慢速拖延。"""

    def __init__(self, raw, transport, deadline):
        self.raw, self.transport, self.deadline = raw, transport, deadline

    def readable(self):
        return True

    def readinto(self, buffer):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('request deadline exceeded')
        self.transport.settimeout(min(10, remaining))
        return self.raw.readinto(buffer)

    def close(self):
        try:
            self.raw.close()
        finally:
            super().close()


class _DeadlineSocket:
    """让 http.client 的缓冲读取在每次实际 recv 时重新计算剩余预算。"""

    def __init__(self, transport, deadline):
        self.transport, self.deadline = transport, deadline

    def makefile(self, mode):
        raw = self.transport.makefile(mode, buffering=0)
        return io.BufferedReader(_DeadlineReader(raw, self.transport, self.deadline))

    def sendall(self, data):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('request deadline exceeded')
        self.transport.settimeout(min(10, remaining))
        self.transport.sendall(data)

    def close(self):
        self.transport.close()


def public_target(url: str) -> tuple[str, str, str]:
    """校验所有解析地址，并返回唯一一次解析选定的 IP，防止检查后重新解析。"""
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) <= 32 for c in url) or '\\' in url:
        raise ResearchNetworkError('URL 无效')
    try:
        parts = urlsplit(url)
        if (parts.scheme != 'https' or not parts.hostname or parts.port not in (None, 443)
                or parts.username is not None or parts.password is not None or parts.fragment):
            raise ValueError
        host = parts.hostname.encode('idna').decode('ascii').lower()
        answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        addresses = [answer[4][0] for answer in answers]
        if not addresses:
            raise ValueError
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if (not ip.is_global or ip.is_multicast or ip.is_reserved
                    or (isinstance(ip, ipaddress.IPv6Address)
                        and (ip.ipv4_mapped or ip.sixtofour or ip.teredo))):
                raise ValueError
        return host, addresses[0], (parts.path or '/') + ('?' + parts.query if parts.query else '')
    except (ValueError, OSError, UnicodeError):
        raise ResearchNetworkError('仅允许 HTTPS/443 公网地址，拒绝凭据、私网和无效 DNS') from None


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """不再解析域名，保留默认 CA 校验、原始 Host 和 SNI。"""

    def __init__(self, host: str, address: str, timeout: float):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address
        self.deadline = time.monotonic() + REQUEST_SECONDS

    def connect(self) -> None:
        """使用预先检查的 IP；TLS 失败时关闭裸连接，避免资源泄漏。"""
        raw = socket.create_connection((self.address, 443), self.timeout)
        try:
            # 先保留 TLS socket 再握手，绝对时限才能中断慢速握手、响应头和正文。
            self.transport = self._context.wrap_socket(raw, server_hostname=self.host, do_handshake_on_connect=False)
            self.transport.do_handshake()
            self.sock = _DeadlineSocket(self.transport, self.deadline)
        except BaseException:
            if getattr(self, 'transport', None) is not None:
                self.transport.close()
            raw.close()
            raise


def fetch(url: str, *, method: str = 'GET', headers: dict | None = None,
          body: bytes | None = None) -> dict:
    """有界读取文本/JSON/CSV；每次只发一次请求，错误响应不回显正文和请求头。"""
    host, address, path = public_target(url)
    connection = _PinnedHTTPSConnection(host, address, timeout=10)
    timed_out = threading.Event()

    def interrupt() -> None:
        """单次 socket timeout 会被慢速传输重置，绝对预算必须主动中断连接。"""
        timed_out.set()
        transport = getattr(connection, 'transport', None)
        if transport is not None:
            try:
                # HTTPResponse 可能仍持有已标记关闭的 TLS socket，直接中断底层 TCP。
                if isinstance(transport, socket.socket):
                    socket.socket.shutdown(transport, socket.SHUT_RDWR)
                else:
                    transport.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    deadline = time.monotonic() + REQUEST_SECONDS
    connection.deadline = deadline
    timer = threading.Timer(REQUEST_SECONDS, interrupt)
    timer.daemon = True
    timer.start()
    try:
        connection.request(method, path, body=body, headers={
            'User-Agent': 'PaybackResearch/1.0', 'Accept-Encoding': 'identity', **(headers or {})})
        response = connection.getresponse()
        if response.status < 200 or response.status >= 300:
            raise ResearchNetworkError(f'数据源返回 HTTP {response.status}，未跟随重定向或自动重试')
        content_header = Message()
        content_header['Content-Type'] = response.getheader('Content-Type', '')
        content_type = content_header.get_content_type().lower()
        charset = content_header.get_content_charset('utf-8')
        if not (content_type.startswith('text/') or content_type == 'application/json'
                or content_type.endswith('+json')):
            raise ResearchNetworkError('首批仅支持文本、JSON 和 CSV 响应')
        if charset not in ('utf-8', 'utf8', 'ascii', 'gb18030', 'gbk', 'gb2312', 'iso-8859-1'):
            raise ResearchNetworkError('响应字符编码不支持，未尝试替换乱码')
        if ('json' in content_type) and charset not in ('utf-8', 'utf8'):
            raise ResearchNetworkError('JSON 响应必须使用 UTF-8')
        if response.getheader('Content-Encoding', 'identity').lower() not in ('', 'identity'):
            raise ResearchNetworkError('不接受压缩响应')
        length = response.getheader('Content-Length')
        if length is not None and (not length.isdigit() or int(length) > MAX_BYTES):
            raise ResearchNetworkError('响应长度无效或超过 256 KiB 限制')
        chunks, size = [], 0
        while True:
            if timed_out.is_set() or time.monotonic() > deadline:
                raise ResearchNetworkError('读取响应超时')
            chunk = response.read(min(16384, MAX_BYTES + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_BYTES:
                raise ResearchNetworkError('响应超过 256 KiB 限制')
            chunks.append(chunk)
        if timed_out.is_set():
            raise ResearchNetworkError('HTTPS 请求超过总时限')
        if length is not None and size != int(length):
            raise ResearchNetworkError('响应不完整，实际长度与 Content-Length 不一致')
        try:
            text = b''.join(chunks).decode(charset, errors='strict')
        except UnicodeError:
            raise ResearchNetworkError('响应字符编码无效，未尝试替换乱码') from None
        return {'content_type': content_type, 'text': text}
    except ResearchNetworkError:
        raise
    except (OSError, ValueError, http.client.HTTPException):
        raise ResearchNetworkError('HTTPS 请求失败，请检查网络或数据源权限') from None
    finally:
        timer.cancel()
        connection.close()
