"""研究来源与请求计划测试，模型只能提供管理员定义的参数。"""

import json

import pytest

from payback_agent.research import ResearchBackend


def backend(tmp_path, monkeypatch):
    """提供假只读数据源，密钥仅用于验证宿主注入与脱敏。"""
    config = {'example': {'label': '示例', 'domain': 'a-share',
        'origin': 'https://api.example.com', 'documentation': 'https://example.com/docs',
        'auth': {'env': 'TEST_RESEARCH_KEY', 'header': 'Authorization', 'prefix': 'Bearer '},
        'operations': {'query': {'path': '/v1/query', 'method': 'POST', 'read_only': True,
            'parameters': {'query': {'type': 'string', 'required': True, 'max_length': 100},
                           'page': {'type': 'integer', 'minimum': 1, 'maximum': 20}}}}}}
    path = tmp_path / 'sources.json'
    path.write_text(json.dumps(config), encoding='utf-8')
    monkeypatch.setenv('TEST_RESEARCH_KEY', 'private-test-key')
    return ResearchBackend(path), path


def test_sources_prioritize_aiwencai_without_inventing_endpoint(tmp_path, monkeypatch):
    from payback_agent.research_iwencai import IwencaiSource
    monkeypatch.setattr(IwencaiSource, 'available', property(lambda self: False))
    service, _ = backend(tmp_path, monkeypatch)
    result = service.sources()
    assert result['preferred_a_share_source'] == 'aiwencai'
    assert result['aiwencai_configured'] is False
    serialized = json.dumps(result)
    assert 'private-test-key' not in serialized
    assert 'Authorization' not in serialized
    assert result['sources'][0]['operations']['query']['parameters']['page']['maximum'] == 20


def test_request_injects_bound_credential_and_redacts_output(tmp_path, monkeypatch):
    service, _ = backend(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr('payback_agent.research.fetch', lambda url, **kwargs:
        calls.append((url, kwargs)) or {'content_type': 'application/json',
                                       'text': '{"count":0,"echo":"private-test-key"}'})
    result = service.run('request', {'source': 'example', 'operation': 'query',
                                     'parameters': {'query': '样本', 'page': 1}})
    assert calls[0][0] == 'https://api.example.com/v1/query'
    assert calls[0][1]['headers']['Authorization'] == 'Bearer private-test-key'
    assert json.loads(calls[0][1]['body']) == {'query': '样本', 'page': 1}
    assert json.loads(result['text'])['count'] == 0
    assert 'private-test-key' not in json.dumps(result)
    assert result['domain_verified'] is False


@pytest.mark.parametrize('parameters', [
    {}, {'query': 'x', 'url': 'https://evil.test'}, {'query': 'x', 'page': True},
    {'query': 'x', 'page': 0}, {'query': 'x', 'page': 21}, {'query': 123},
    {'query': 'x' * 101}, {'query': 'x', 'page': float('nan')},
])
def test_invalid_parameters_never_reach_network(tmp_path, monkeypatch, parameters):
    service, _ = backend(tmp_path, monkeypatch)
    monkeypatch.setattr('payback_agent.research.fetch', lambda *args, **kwargs:
                        pytest.fail('未授权参数触发了网络请求'))
    with pytest.raises(ValueError):
        service.run('request', {'source': 'example', 'operation': 'query', 'parameters': parameters})


def test_every_request_rechecks_admin_policy(tmp_path, monkeypatch):
    service, path = backend(tmp_path, monkeypatch)
    path.write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='未登记'):
        service.run('request', {'source': 'example', 'operation': 'query', 'parameters': {'query': 'x'}})


@pytest.mark.parametrize('change', ['write', 'origin', 'path', 'header'])
def test_bad_admin_policy_fails_closed(tmp_path, monkeypatch, change):
    service, path = backend(tmp_path, monkeypatch)
    config = json.loads(path.read_text(encoding='utf-8'))
    source = config['example']
    if change == 'write':
        source['operations']['query']['read_only'] = False
    elif change == 'origin':
        source['origin'] = 'http://127.0.0.1'
    elif change == 'path':
        source['operations']['query']['path'] = '//evil.test/data'
    else:
        source['auth']['header'] = 'Host'
    path.write_text(json.dumps(config), encoding='utf-8')
    with pytest.raises(ValueError):
        service.run('request', {'source': 'example', 'operation': 'query', 'parameters': {'query': 'x'}})


def test_fetch_strips_script_and_keeps_untrusted_label(monkeypatch):
    monkeypatch.setattr('payback_agent.research.fetch', lambda *args, **kwargs:
        {'content_type': 'text/html', 'text': '<h1>文档</h1><script>steal()</script><p>数据</p>'})
    result = ResearchBackend().run('fetch', {'url': 'https://example.com/docs'})
    assert '文档' in result['text'] and '数据' in result['text']
    assert 'steal()' not in result['text']
    assert result['trusted_instructions'] is False


def test_search_uses_fixed_brave_endpoint(monkeypatch):
    monkeypatch.setenv('AGENT_RESEARCH_BRAVE_API_KEY', 'private-search-key')
    calls = []
    monkeypatch.setattr('payback_agent.research.fetch', lambda url, **kwargs:
        calls.append((url, kwargs)) or {'content_type': 'application/json', 'text': '{"web":{"results":[]}}'})
    result = ResearchBackend().run('search', {'query': '公开接口文档'})
    assert calls[0][0].startswith('https://api.search.brave.com/res/v1/web/search?')
    assert calls[0][1]['headers']['X-Subscription-Token'] == 'private-search-key'
    assert result['source'] == 'brave'


def test_missing_keys_and_unknown_actions_are_explicit(monkeypatch):
    monkeypatch.delenv('AGENT_RESEARCH_BRAVE_API_KEY', raising=False)
    with pytest.raises(ValueError, match='未配置'):
        ResearchBackend().run('search', {'query': 'test'})
    with pytest.raises(ValueError, match='不支持'):
        ResearchBackend().run('python', {'code': 'print(123)'})


def test_unsupported_parameter_constraints_are_not_silently_ignored(tmp_path, monkeypatch):
    service, path = backend(tmp_path, monkeypatch)
    config = json.loads(path.read_text(encoding='utf-8'))
    config['example']['operations']['query']['parameters']['query']['enum'] = ['only-allowed-query']
    path.write_text(json.dumps(config), encoding='utf-8')
    with pytest.raises(ValueError, match='配置无效'):
        service.sources()


def test_broken_registry_does_not_disable_public_fetch_or_search(tmp_path, monkeypatch):
    service, path = backend(tmp_path, monkeypatch)
    path.write_text('broken json', encoding='utf-8')
    monkeypatch.setenv('AGENT_RESEARCH_BRAVE_API_KEY', 'private-search-key')
    monkeypatch.setattr('payback_agent.research.fetch', lambda *args, **kwargs:
        {'text': 'public-data', 'content_type': 'text/plain'})
    assert service.run('fetch', {'url': 'https://example.com'})['text'] == 'public-data'
    assert service.run('search', {'query': 'public'})['source'] == 'brave'
    with pytest.raises(ValueError):
        service.sources()
