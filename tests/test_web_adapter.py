"""웹 브리지의 선택 검증/충전 목표/승인 재전송. 실제 외부 API는 호출하지 않는다."""
import importlib.util
import io
import json
from datetime import timedelta
from pathlib import Path

import pytest

from tests.test_approval import _ai, _tc, _plan_ready
from tests.test_decide_entrypoint import _agent, _done

spec = importlib.util.spec_from_file_location('voltgo_web_test', Path(__file__).resolve().parents[1] / 'scripts/web.py')
web = importlib.util.module_from_spec(spec)
spec.loader.exec_module(web)


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(web, 'contexts', {})
    monkeypatch.setattr(web, 'approval_tokens', {})
    monkeypatch.setattr(web, 'decision_results', {})
    monkeypatch.setattr(web, 'agent', None)


def post(path, body):
    handler = object.__new__(web.Handler)
    raw = json.dumps(body).encode()
    handler.path, handler.headers, handler.rfile = path, {'Content-Length': str(len(raw))}, io.BytesIO(raw)
    result = []
    handler._json = lambda data, status=200: result.append((status, data))
    handler.do_POST()
    return result[0]


@pytest.mark.parametrize('browser_key', [None, '  browser-map-test-key  '])
def test_map_config_exposes_only_explicit_browser_key(monkeypatch, browser_key):
    monkeypatch.setenv('TMAP_APP_KEY', 'server-tmap-test-key')
    monkeypatch.setenv('OPENAI_API_KEY', 'server-openai-test-key')
    if browser_key is None:
        monkeypatch.delenv('TMAP_MAP_APP_KEY', raising=False)
    else:
        monkeypatch.setenv('TMAP_MAP_APP_KEY', browser_key)
    handler = object.__new__(web.Handler)
    handler.path, handler.wfile = '/map-config.js', io.BytesIO()
    status, headers = [], {}
    handler.send_response = status.append
    handler.send_header = lambda name, value: headers.update({name: value})
    handler.end_headers = lambda: None
    handler.do_GET()
    raw = handler.wfile.getvalue()
    config = json.loads(raw.decode().removeprefix('window.VOLTGO_MAP_CONFIG = ').removesuffix(';\n'))
    assert status == [200]
    assert config == {'appKey': (browser_key or '').strip()}
    assert headers['Cache-Control'] == 'no-store'
    assert headers['Content-Length'] == str(len(raw))
    assert headers['Content-Type'].startswith('application/javascript')
    assert b'server-tmap-test-key' not in raw
    assert b'server-openai-test-key' not in raw


def test_legacy_session_target_cannot_override_vehicle_display(context, snapshot, budget):
    context.session.charging = snapshot.model_copy(update={'target_soc_pct': 90, 'reported_target_soc_pct': 90, 'reported_remaining_sec': 3600})
    context.session.time_budget = budget
    context.session.target_soc_pct = 80
    result = web.session_summary(context)
    assert result['charging']['reported_target_soc_pct'] == 90
    assert result['effective_target_soc_pct'] == 90
    assert result['display_charging']['target_soc_pct'] == 90
    assert result['display_charging']['reported_remaining_sec'] == 3600
    assert result['home_budget']['finish_at'].endswith('15:00:00+09:00')
    assert result['home_budget']['estimate_basis'] == 'reported_remaining'
    assert 'requested_target_soc_pct' not in result


def test_unknown_goal_stays_unknown(context, snapshot):
    context.session.charging = snapshot.model_copy(update={'target_soc_pct': None, 'reported_target_soc_pct': None})
    result = web.session_summary(context)
    assert result['effective_target_soc_pct'] is None
    assert 'requested_target_soc_pct' not in result
    assert result['home_budget'] is None


def selection(context):
    _plan_ready(context)
    candidate = context.session.candidates['A']
    return {'selection': {'kind': 'plan', 'id': candidate.plan_id, 'version': candidate.version, 'evaluated_at': candidate.evaluated_at.isoformat()}}


def test_clicked_card_uses_matching_version_and_generation(context):
    body = selection(context)
    assert 'confirm_plan' in web.validate_selection(context, body)
    body['selection']['version'] += 1
    with pytest.raises(ValueError, match='조건이 바뀌었거나'):
        web.validate_selection(context, body)


@pytest.mark.parametrize('change', ['generation', 'expiry', 'conditions'])
def test_old_card_cannot_select_reused_place_id(context, change):
    body = selection(context)
    if change == 'generation':
        body['selection']['evaluated_at'] = (context.clock() - timedelta(seconds=1)).isoformat()
    elif change == 'expiry':
        now = context.clock()
        context.clock = lambda: now + timedelta(seconds=301)
    else:
        context.session.bump_version()
    with pytest.raises(ValueError):
        web.validate_selection(context, body)


def test_refresh_invalidates_cards_and_returns_map_coordinates(context):
    selection(context)
    web.contexts['t'] = context
    status, data = post('/api/refresh', {'thread_id': 't', 'instance_id': web.INSTANCE_ID})
    assert status == 200
    assert data['session']['candidates'] == []
    assert data['map_data']['origin']['latitude'] == context.session.origin.latitude
    assert data['map_data']['places']


def test_old_server_request_is_rejected_without_model_call(context):
    web.contexts['t'] = context
    status, _ = post('/api/ask', {'thread_id': 't', 'instance_id': 'old-server', 'text': '추천'})
    assert status == 409
    assert web.agent is None


def test_approval_replay_is_same_result_without_second_save(context, monkeypatch):
    agent = _agent([_ai([_tc('save_preferences', {'category': 'cafe', 'dwell_min': 15}, 'save')]), _done()])
    monkeypatch.setattr(web, 'agent', agent)
    web.contexts['t'] = context
    response = web.ask(agent, '카페 취향 기억해줘', context, 't')
    payload = web.envelope(context, 't', response)
    pending = payload['pending_approval']
    assert pending['actions'][0]['args']['dwell_min'] == 15
    body = {'thread_id': 't', 'decision': 'approve', 'request_id': pending['request_id']}
    first = post('/api/decide', body)
    second = post('/api/decide', body)
    assert first == second
    assert first[0] == 200
    assert first[1]['session']['preferences']['preferred_category'] == 'cafe'
    status, _ = post('/api/decide', {**body, 'decision': 'reject'})
    assert status == 409


def test_wrong_approval_id_does_not_resume(context, monkeypatch):
    agent = _agent([_ai([_tc('save_preferences', {'category': 'cafe'}, 'save')]), _done()])
    monkeypatch.setattr(web, 'agent', agent)
    web.contexts['t'] = context
    web.ask(agent, '기억해줘', context, 't')
    status, _ = post('/api/decide', {'thread_id': 't', 'decision': 'approve', 'request_id': 'wrong'})
    assert status == 409
    assert web.memory.load_preferences(context.user_id) is None
