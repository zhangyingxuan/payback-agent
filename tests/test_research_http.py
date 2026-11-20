"""受控联网边界测试，所有 DNS 与连接使用假实现，不访问真实网络。"""

import socket
from types import SimpleNamespace

import pytest

from payback_agent import research_http as net


def public_dns(monkeypatch, addresses=('93.184.216.34',)):
    """模拟多地址解析，验证不会通过另一次解析绕过检查。"""
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs: [
        (socket.AF_INET6 if ':' in ip else socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443))
        for ip in addresses])


@pytest.mark.parametrize('url', [
    'http://example.com', 'https://localhost/', 'https://127.0.0.1/',
    'https://[::1]/', 'https://user:pass@example.com/', 'https://example.com:8443/',
    'https://example.com/#fragment', 'file:///etc/passwd', 'https://example.com/\r\nHost:x',
])
def test_rejects_invalid_or_non_public_urls(monkeypatch, url):
    public_dns(monkeypatch, ('127.0.0.1',))
    with pytest.raises(net.ResearchNetworkError):
        net.fetch(url)


@pytest.mark.parametrize('addresses', [
    ('93.184.216.34', '10.0.0.1'), ('::ffff:127.0.0.1',), ('169.254.169.254',),
    ('0.0.0.0',), ('192.168.1.1',), ('fe80::1',),
])
def test_all_dns_addresses_must_be_public(monkeypatch, addresses):
    public_dns(monkeypatch, addresses)
    with pytest.raises(net.ResearchNetworkError):
        net.fetch('https://example.com/data')


def fake_connection(monkeypatch, *, status=200, body=b'{"count":0}', headers=None):
    """记录实际连接的 IP、域名和请求，检查鉴权不经过自动重定向。"""
    public_dns(monkeypatch)
    calls = []
    response_headers = {'Content-Type': 'application/json', **(headers or {})}

    class Connection:
        def __init__(self, host, address, timeout):
            calls.append((host, address, timeout))

        def request(self, method, path, body=None, headers=None):
            calls.append((method, path, body, headers))

        def getresponse(self):
            import io
            stream = io.BytesIO(body)
            return SimpleNamespace(status=status, getheader=lambda key, default=None:
                                   response_headers.get(key, default), read=stream.read)

        def close(self):
            calls.append('closed')

    monkeypatch.setattr(net, '_PinnedHTTPSConnection', Connection)
    return calls


def test_uses_checked_ip_preserves_hostname_and_zero(monkeypatch):
    calls = fake_connection(monkeypatch)
    result = net.fetch('https://example.com/data?page=1')
    assert calls[0][:2] == ('example.com', '93.184.216.34')
    assert calls[1][:2] == ('GET', '/data?page=1')
    assert result['text'] == '{"count":0}'
    assert result['content_type'] == 'application/json'
    assert calls[-1] == 'closed'


@pytest.mark.parametrize('status', [301, 302, 307, 401, 429, 500])
def test_http_errors_do_not_follow_redirects_or_echo_secrets(monkeypatch, status):
    calls = fake_connection(monkeypatch, status=status, body=b'secret credential',
                            headers={'Location': 'https://127.0.0.1/'})
    with pytest.raises(net.ResearchNetworkError) as error:
        net.fetch('https://example.com', headers={'Authorization': 'secret credential'})
    assert 'secret' not in str(error.value)
    assert len([call for call in calls if isinstance(call, tuple) and call[0] == 'GET']) == 1
    assert calls[-1] == 'closed'


@pytest.mark.parametrize('headers,body', [
    ({'Content-Length': '999999'}, b''), ({}, b'x' * 262145),
    ({'Content-Encoding': 'gzip'}, b'compressed'),
    ({'Content-Type': 'application/octet-stream'}, b'binary'),
], ids=['length', 'stream', 'encoding', 'binary'])
def test_rejects_large_encoded_or_binary_responses(monkeypatch, headers, body):
    fake_connection(monkeypatch, headers=headers, body=body)
    with pytest.raises(net.ResearchNetworkError):
        net.fetch('https://example.com')


def test_tls_uses_origin_hostname(monkeypatch):
    """连接固定公网 IP，但证书校验和 SNI 必须使用原始域名。"""
    observed = {}
    sock = object()
    monkeypatch.setattr(socket, 'create_connection', lambda address, timeout:
                        observed.update(address=address, timeout=timeout) or sock)
    context = SimpleNamespace(wrap_socket=lambda raw, server_hostname, **kwargs:
                              observed.update(raw=raw, hostname=server_hostname)
                              or SimpleNamespace(do_handshake=lambda: None))
    monkeypatch.setattr(net.ssl, 'create_default_context', lambda: context)
    connection = net._PinnedHTTPSConnection('example.com', '93.184.216.34', 10)
    connection.connect()
    assert observed['address'] == ('93.184.216.34', 443)
    assert observed['hostname'] == 'example.com'
    assert observed['raw'] is sock


def test_rejects_truncated_content_length(monkeypatch):
    fake_connection(monkeypatch, headers={'Content-Length': '20'}, body=b'{"count":0}')
    with pytest.raises(net.ResearchNetworkError, match='不完整'):
        net.fetch('https://example.com')


@pytest.mark.parametrize('phase', ['headers', 'body'])
def test_absolute_deadline_interrupts_slow_stream(monkeypatch, phase):
    """模拟持续慢速传输，不能靠不断收到一个字节重置 socket 超时。"""
    import threading
    import time
    public_dns(monkeypatch)
    interrupted = threading.Event()
    monkeypatch.setattr(net, 'REQUEST_SECONDS', .05)

    def blocked(*args):
        assert interrupted.wait(1), '绝对时限没有关闭在途连接'
        raise OSError('connection interrupted')

    class SlowConnection:
        def __init__(self, *args, **kwargs):
            self.transport = SimpleNamespace(shutdown=lambda *args: interrupted.set())

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            if phase == 'headers':
                blocked()
            return SimpleNamespace(status=200,
                getheader=lambda key, default=None: 'text/plain' if key == 'Content-Type' else default,
                read=blocked)

        def close(self):
            pass

    monkeypatch.setattr(net, '_PinnedHTTPSConnection', SlowConnection)
    started = time.monotonic()
    with pytest.raises(net.ResearchNetworkError):
        net.fetch('https://example.com')
    assert time.monotonic() - started < .8


def test_deadline_interrupts_socket_even_if_tls_shutdown_wrapper_refuses(monkeypatch):
    """HTTPResponse 留住底层连接时，不能依赖 TLS 包装层的关闭状态。"""
    public_dns(monkeypatch)
    monkeypatch.setattr(net, 'REQUEST_SECONDS', .05)
    left, right = socket.socketpair()

    class Transport(socket.socket):
        def shutdown(self, how):
            raise OSError('TLS wrapper already closed')

    transport = Transport(fileno=left.detach())
    transport.settimeout(.2)

    class Connection:
        def __init__(self, *args, **kwargs):
            self.transport = transport

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return SimpleNamespace(status=200,
                getheader=lambda key, default=None: 'text/plain' if key == 'Content-Type' else default,
                read=lambda size: transport.recv(size))

        def close(self):
            transport.close()

    monkeypatch.setattr(net, '_PinnedHTTPSConnection', Connection)
    try:
        with pytest.raises(net.ResearchNetworkError):
            net.fetch('https://example.com')
    finally:
        right.close()
        transport.close()


def test_preserves_declared_chinese_charset_and_rejects_invalid_utf8(monkeypatch):
    fake_connection(monkeypatch, headers={'Content-Type': 'text/html; charset=GB18030'},
                    body='中文接口文档'.encode('gb18030'))
    assert net.fetch('https://example.com')['text'] == '中文接口文档'
    fake_connection(monkeypatch, headers={'Content-Type': 'application/json'}, body=b'\xff')
    with pytest.raises(net.ResearchNetworkError, match='编码'):
        net.fetch('https://example.com')


@pytest.mark.parametrize('phase', ['headers', 'body'])
def test_buffered_reads_check_deadline_on_every_underlying_receive(phase):
    """即使每个 recv 都有数据，缓冲层内部循环也必须在绝对期限停止。"""
    import io
    import time

    class SlowRaw(io.RawIOBase):
        def readinto(self, buffer):
            time.sleep(.01)
            buffer[0] = ord('x')
            return 1

    transport = SimpleNamespace(settimeout=lambda value: None)
    reader = io.BufferedReader(net._DeadlineReader(SlowRaw(), transport, time.monotonic() + .05))
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            reader.readline() if phase == 'headers' else reader.read(1024)
        assert time.monotonic() - started < .5
    finally:
        reader.close()
