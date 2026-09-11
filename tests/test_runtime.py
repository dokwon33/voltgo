"""A 진입점과 PR10 연결 회귀. 실제 graph/도구를 쓰고 외부 모델/API만 고정한다."""
from langchain_core.messages import ToolMessage
import json
import pytest

from tests.test_approval import ScriptedModel, _ai, _tc
from tests.test_alternative_agent import _shortage_turn, _decision
from voltgo.agent import memory
from voltgo.agent.agent import ask, build_agent, decide
from voltgo.agent.requests import config_for
from voltgo.agent.schemas import Place


def test_alternative_flow_preserves_preference_request_id_replay(context):
    model = ScriptedModel(script=_shortage_turn(prefix='scope') + [
        _ai([_tc('search_nearby_places', {'category': 'convenience', 'dwell_min': 10}, 'search')]),
        _ai([_tc('get_walking_routes', {'poi_ids': ['E']}, 'routes')]),
        _ai([_tc('select_feasible_plans', {'dwell_min': 10}, 'select')]),
        _decision('choose', '편의점 후보입니다.', ['E'], 'choice'),
        _ai([_tc('save_preferences', {'category': 'convenience'}, 'save')]),
        _decision('done', '선호를 저장했습니다.', tag='saved'),
    ])
    agent = build_agent(model=model)
    thread = 'alternative-plus-idempotency'
    first = ask(agent, '20분 안에 밥 먹고 싶어', context, thread)
    assert first.status == 'need_input' and first.request_id is None
    second = ask(agent, '편의점 좋아', context, thread)
    assert second.status == 'ok' and second.candidates[0].category == 'convenience'
    pending = ask(agent, '편의점 선호를 기억해줘', context, thread)
    assert pending.status == 'awaiting_approval' and pending.request_id
    assert memory.load_preferences(context.user_id) is None
    result = decide(agent, 'approve', context, thread, request_id=pending.request_id)
    used = model.idx
    replay = decide(agent, 'approve', context, thread, request_id=pending.request_id)
    assert replay == result and model.idx == used
    assert memory.load_preferences(context.user_id).preferred_category == 'convenience'
    messages = agent.get_state(config_for(context.user_id, thread)).values['messages']
    assert len([m for m in messages if isinstance(m, ToolMessage) and m.name == 'save_preferences']) == 1


@pytest.mark.parametrize("lookup_station", [False, True], ids=["known-origin", "lookup-origin"])
def test_station_lookup_and_radius_retry_can_finish_alternative_prompt(context, lookup_station):
    base = context.places_client
    class PlacesWithOnlyDistantMeal:
        def find_station(self, keyword):
            return base.find_station(keyword)
        def search_around(self, origin, category, radius_km=1):
            return [Place(poi_id='A', name='700m 식당', category='meal',
                          latitude=origin.latitude + .0063, longitude=origin.longitude,
                          distance_m=700, poi_source='mock')]
    if lookup_station:
        context.session.origin = None
    context.places_client = PlacesWithOnlyDistantMeal()
    station_steps = [_ai([_tc('find_station', {'keyword': '강남역 EV충전소'}, 'station')])] if lookup_station else []
    model = ScriptedModel(script=station_steps + [
        _ai([_tc('get_charging_status', {}, 'charging')]),
        _ai([_tc('calculate_time_budget', {'user_limit_min': 20}, 'budget')]),
        _ai([_tc('search_nearby_places', {'category': 'meal'}, 'search')]),
        _ai([_tc('search_nearby_places', {'category': 'meal', 'max_dist_m': 1000}, 'widen')]),
        _ai([_tc('get_walking_routes', {'poi_ids': ['A']}, 'routes')]),
        _ai([_tc('select_feasible_plans', {'dwell_min': 20}, 'select')]),
        _ai([_tc('assess_time_shortage_alternatives', {}, 'assess')]),
        _decision('clarify', '식사는 어렵습니다. 편의점을 찾아볼까요?', tag='final'),
    ])
    agent = build_agent(model=model)
    thread = 'station-widen-shortage'
    response = ask(agent, '강남역 EV충전소에서 20분 안에 밥 먹고 싶어', context, thread)
    messages = agent.get_state(config_for(context.user_id, thread)).values['messages']
    results = [json.loads(m.content) for m in messages if isinstance(m, ToolMessage)
               and m.name == 'assess_time_shortage_alternatives']
    assert results[-1]['data']['reason'] == 'time_insufficient'
    print('actual_model_calls=', model.idx, 'counter=', context.session.counters['model'],
          'response=', response.status, response.message)
    assert response.status == 'need_input' and '편의점' in response.message
    assert model.idx == 8
    if lookup_station:
        # 마지막 구조화 출력용 모델을 호출하지 않고 검증된 도구 결과를 안내한다.
        assert messages[-1].name == 'assess_time_shortage_alternatives'
        model.script[8:] = [_decision('stop', '다음 요청을 받았습니다.', tag='next')]
        following = ask(agent, '오늘은 여기까지 할게', context, thread)
        assert following.status != 'error' and model.idx == 9


@pytest.mark.parametrize("invalid", ["sk-" + "test_only_fake_token_" * 2, "x" * 501, "   "])
def test_rejected_input_never_enters_checkpoint_or_next_model(context, monkeypatch, invalid):
    seen = []
    original = ScriptedModel._generate
    def capture(self, messages, **kwargs):
        seen.extend(messages)
        return original(self, messages, **kwargs)
    monkeypatch.setattr(ScriptedModel, "_generate", capture)
    model = ScriptedModel(script=[_decision('stop', '안내했습니다.', tag='safe')])
    agent = build_agent(model=model)
    thread = 'input-guard'
    response = ask(agent, invalid, context, thread)
    config = config_for(context.user_id, thread)
    assert response.status == 'need_input' and model.idx == 0
    assert not list(agent.get_state_history(config))
    following = ask(agent, '카페를 찾아줘', context, thread)
    assert following.status != 'error' and model.idx == 1
    assert all(invalid not in str(m.content) for m in seen)
    for snapshot in agent.get_state_history(config):
        assert all(invalid not in str(m.content) for m in snapshot.values.get('messages', []))


@pytest.mark.parametrize("transient,attempts", [(True, 2), (False, 1)])
def test_model_failure_is_sanitized_and_next_turn_recovers(context, monkeypatch, caplog, transient, attempts):
    class APITimeoutError(RuntimeError):
        pass
    calls = []
    original = ScriptedModel._generate
    def fail(self, messages, **kwargs):
        calls.append(1)
        raise (APITimeoutError if transient else RuntimeError)('private-upstream-details')
    monkeypatch.setattr(ScriptedModel, '_generate', fail)
    model = ScriptedModel(script=[_decision('stop', '복구했습니다.', tag='recovered')])
    agent = build_agent(model=model)
    response = ask(agent, '카페를 찾아줘', context, 'failure')
    assert len(calls) == attempts
    assert response.status == 'error' and 'AGENT_EXECUTION_FAILED' in response.message
    assert 'private-upstream-details' not in response.message + caplog.text
    monkeypatch.setattr(ScriptedModel, '_generate', original)
    assert ask(agent, '다시 안내해줘', context, 'failure').status != 'error'


def test_ask_preserves_pending_approval_and_original_prompt(context):
    model = ScriptedModel(script=[_ai([_tc('save_preferences', {'category': 'cafe'}, 'save')]),
                                 _decision('done', '완료', tag='done')])
    agent = build_agent(model=model)
    pending = ask(agent, '카페 기억해줘', context, 'pending')
    before = agent.get_state(config_for(context.user_id, 'pending')).values
    blocked = ask(agent, '식당도 찾아줘', context, 'pending')
    assert 'PENDING_APPROVAL' in blocked.message and model.idx == 1
    assert agent.get_state(config_for(context.user_id, 'pending')).values == before
    assert decide(agent, 'approve', context, 'pending', request_id=pending.request_id).status != 'error'


def test_budget_fallback_does_not_use_stale_or_unverified_assessments(context):
    from voltgo.agent.assembler import exhausted_alternative_response
    from langchain.messages import HumanMessage
    name = 'assess_time_shortage_alternatives'
    call = _ai([_tc(name, {}, 'assess')])
    reply = ToolMessage(name=name, tool_call_id='assess', content=json.dumps({
        'status': 'ok', 'data': {'reason': 'time_insufficient', 'alternatives': ['convenience']}}))
    for messages in [[call, reply, HumanMessage(content='다른 질문')],
                     [call, reply.model_copy(update={'content': 'invalid json'})],
                     [call, reply.model_copy(update={'status': 'error'})],
                     [call, reply.model_copy(update={'tool_call_id': 'unmatched'})],
                     [call, reply]]:  # Session 검증 상태 없는 결과도 거부
        assert exhausted_alternative_response({'messages': messages}, context) is None
