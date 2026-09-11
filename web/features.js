// UI state lives in this browser; agent checkpoints remain on the running server.
let timeline = [], conversations = [], replaying = false, archived = false;
let serverInstance = '', pendingApproval = null, receivedAt = Date.now(), noticeTimer;
const HISTORY_LIMIT = 20;
function storageKey() { return 'voltgo.conversations.v1.' + (health.user_id || 'local'); }
function notifyUser(message) {
  $('#notice').textContent = message; $('#notice').hidden = false;
  clearTimeout(noticeTimer); noticeTimer = setTimeout(() => { $('#notice').hidden = true; }, 5000);
}
function loadConversations() {
  try {
    const data = JSON.parse(localStorage.getItem(storageKey()) || '[]');
    conversations = Array.isArray(data) ? data.filter(r => typeof r.id === 'string' && Array.isArray(r.turns)).slice(0, HISTORY_LIMIT) : [];
  } catch { conversations = []; notifyUser('이 브라우저에서 대화 기록을 읽지 못했어요.'); }
}
function recordTurn(turn) { if (!replaying) timeline.push(turn); }
function saveConversation() {
  if (!threadId || archived || replaying || !timeline.length) return;
  const old = conversations.find(r => r.id === threadId);
  const record = {id: threadId, title: timeline.find(t => t.role === 'user')?.text?.slice(0, 70) || '볼티와의 대화',
    created_at: old?.created_at || new Date().toISOString(), updated_at: new Date().toISOString(),
    instance_id: serverInstance, session, response: lastRes, turns: timeline.slice(-100)};
  conversations = [record, ...conversations.filter(r => r.id !== threadId)].slice(0, HISTORY_LIMIT);
  try { localStorage.setItem(storageKey(), JSON.stringify(conversations)); }
  catch { notifyUser('저장 공간이 부족해 이번 대화는 새로고침하면 사라질 수 있어요.'); }
}
function serverNow() {
  const base = Date.parse(session.now);
  return Number.isFinite(base) ? base + (health.clock === 'fixed' ? 0 : Date.now() - receivedAt) : Date.now();
}
function acceptEnvelope(data) {
  session = data.session || {}; serverInstance = data.instance_id || serverInstance;
  if (data.instance_id) health.instance_id = data.instance_id;
  pendingApproval = data.pending_approval || null; receivedAt = Date.now();
  syncFeatures();
}
function approvalExpired() {
  const end = Date.parse(pendingApproval?.expires_at);
  return Number.isFinite(end) && serverNow() > end;
}
function updateApprovalExpiry() {
  let note = $('#approvalExpiry');
  if (!note) { note = document.createElement('p'); note.id = 'approvalExpiry'; note.className = 'fine'; $('#apBody').after(note); }
  const expiry = Date.parse(pendingApproval?.expires_at);
  note.textContent = !Number.isFinite(expiry) ? '' : approvalExpired() ? '승인 시간이 지났어요. 저장하지 않고 닫은 뒤 다시 요청해 주세요.' : `약 ${Math.max(0, Math.ceil((expiry - serverNow()) / 1000))}초 안에 결정해 주세요`;
  $('#approve').disabled = busy || approvalExpired();
}
function preferenceLabel(p) {
  return [CAT[p.category || p.preferred_category], p.dwell_min ? `머무는 시간 ${p.dwell_min}분` : ''].filter(Boolean).join(' · ') || '설정된 취향이 없어요';
}
function currentDestination() {
  if (archived || !session.confirmed || session.confirmed.version !== session.condition_version) return null;
  return session.candidates?.find(c => c.plan_id === session.confirmed.plan_id && c.version === session.confirmed.version) || null;
}
function freshDestinationOption(id) {
  const c = session.candidates?.find(c => c.plan_id === id);
  const age = serverNow() - Date.parse(c?.evaluated_at);
  return !archived && c?.version === session.condition_version && Number.isFinite(age) && age <= 300000 ? c : null;
}
function updatePlanButtons() {
  const current = session.candidates || [];
  document.querySelectorAll('button[data-plan-id]').forEach(button => {
    const matches = current.some(c => c.plan_id === button.dataset.planId && c.version === Number(button.dataset.version) && c.evaluated_at === button.dataset.evaluated);
    const age = serverNow() - Date.parse(button.dataset.evaluated);
    const stale = archived || !matches || Number(button.dataset.version) !== session.condition_version || !Number.isFinite(age) || age > 300000;
    const selected = !stale && session.confirmed?.plan_id === button.dataset.planId;
    button.disabled = stale || selected || busy || Boolean(pendingApproval) || !$('#approval').hidden;
    button.textContent = selected ? '확정한 계획' : stale ? '지난 추천' : `${button.dataset.planLabel}로 갈래요`;
    button.closest('.plan').classList.toggle('is-stale', stale);
    button.closest('.plan').querySelector('.stale-note').hidden = !stale;
  });
  const destination = currentDestination();
  const cards = [...document.querySelectorAll('.plan[data-confirmed-plan]')];
  const active = destination && cards.filter(card => card.dataset.confirmedPlan === destination.plan_id && Number(card.dataset.version) === destination.version).at(-1);
  cards.forEach(card => {
    const selected = card === active;
    card.classList.toggle('done', selected);
    card.querySelector('.destination-label').textContent = selected ? '현재 목적지' : '이전에 선택한 목적지';
    card.querySelector('.done-tag').textContent = selected ? '확정한 계획' : '이전 계획';
    const change = card.querySelector('[data-change-destination]');
    change.hidden = !selected;
    change.disabled = !selected || busy || Boolean(pendingApproval) || !$('#approval').hidden;
  });
  document.querySelectorAll('[data-destination-plan]').forEach(button => {
    button.disabled = !destination || !freshDestinationOption(button.dataset.destinationPlan) || busy || Boolean(pendingApproval);
  });
}
function syncFeatures() {
  if (!$('#conditionSummary')) return;
  const target = session.effective_target_soc_pct ?? session.display_charging?.target_soc_pct ?? session.charging?.target_soc_pct;
  const parts = [session.origin, target ? `목표 ${target}%` : '', session.user_limit_min ? `시간 제한 ${session.user_limit_min}분` : ''].filter(Boolean);
  $('#conditionSummary').textContent = parts.join(' · '); $('#conditionSummary').hidden = !parts.length;
  $('#archiveNote').hidden = !archived;
  const stations = archived ? [] : session.station_candidates || [];
  $('#stationOptions').hidden = !stations.length;
  $('#stationOptions').innerHTML = stations.length ? '<h3>출발할 충전소를 골라 주세요</h3>' + stations.map(s => `<button class="station-option" type="button" data-station-id="${esc(s.poi_id)}"><b>${esc(s.name)}</b><span>${esc(s.address || '주소 미제공')}</span></button>`).join('') : '';
  updatePlanButtons();
}
function formatTime(value) {
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? new Intl.DateTimeFormat('ko-KR', {timeZone: 'Asia/Seoul', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23'}).format(date) : '미확인';
}
function renderVehicleMeta(s) {
  if (!$('#vehicleMeta')) return;
  $('#carDialogTitle').textContent = archived ? '지난 대화의 차량 정보' : '차량 충전 상태';
  const c = s.charging, b = s.home_budget;
  const rows = [['측정 시각', formatTime(c?.observed_at)], ['마지막 조회', formatTime(c?.fetched_at || c?.observed_at)],
    ['시간 계산', b?.estimate_basis === 'energy_power' ? '배터리 용량·평균 전력으로 추정' : b?.estimate_basis === 'reported_remaining' ? '전달받은 남은 시간 기준' : '계산 정보 미확인']];
  if (c?.reported_target_soc_pct != null) rows.push(['차량 설정 목표', c.reported_target_soc_pct + '%']);
  if (s.requested_target_soc_pct != null) rows.push(['요청 / 계산 목표', `${s.requested_target_soc_pct}% / ${s.effective_target_soc_pct ?? '—'}%`]);
  $('#vehicleMeta').innerHTML = rows.map(([k,v]) => `<div><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join('');
}
const categoryOptions = (value = '') => '<option value="">지금 조건 유지</option>' + Object.entries({meal:'식사', cafe:'카페', convenience:'편의점', mart:'마트'}).map(([v,l]) => `<option value="${v}" ${v === value ? 'selected' : ''}>${l}</option>`).join('');
function openFeature(mode, record = true) {
  if (record && (busy || !$('#approval').hidden)) return;
  if (record) { navigateTo('panel', {panelMode: mode}); return; }
  const dialog = $('#featureDialog');
  dialog.dataset.mode = mode;
  if (mode === 'history') {
    saveConversation();
    $('#featureTitle').textContent = '대화 기록';
    $('#featureBody').innerHTML = `<p class="feature-description">이 브라우저에 최근 ${HISTORY_LIMIT}개 대화를 보관해요.</p><button class="feature-primary" id="newConversation" type="button">+ 새 대화</button><div class="history-list">${conversations.length ? conversations.map(r => `<button class="history-item" type="button" data-thread="${esc(r.id)}"><b>${esc(r.title)}</b><span>${esc(formatTime(r.updated_at))} · ${r.id === threadId ? '지금 보는 대화' : r.instance_id === health.instance_id ? '이어서 대화 가능' : '보관된 대화'}</span></button>`).join('') : '<p class="empty-state">아직 대화가 없어요.<br>볼티에게 가고 싶은 곳을 말해 보세요.</p>'}</div><p class="feature-description">브라우저 데이터를 지우면 기록도 사라져요. 서버가 재시작된 대화는 조회만 가능해요.</p>`;
  } else if (mode === 'destination') {
    const current = currentDestination();
    $('#featureTitle').textContent = '목적지 변경';
    if (!current) {
      $('#featureBody').innerHTML = '<p class="empty-state">현재 확정된 목적지가 없어요. 대화에서 다시 추천받아 주세요.</p>';
    } else {
      const options = (session.candidates || []).filter(c => c.plan_id !== current.plan_id && freshDestinationOption(c.plan_id));
      $('#featureBody').innerHTML = `<div class="destination-current"><span>현재 목적지</span><strong>${esc(current.name)}</strong><span>도보 ${min(current.outbound_sec)}분 · ${hhmm(current.return_at)} 차량 복귀</span></div>
        <button class="feature-quiet" id="keepDestination" type="button">이 목적지 유지하기</button>
        <p class="feature-description">다른 장소를 선택하면 남은 시간을 다시 확인해 목적지를 변경해요.</p>
        <div class="destination-options">${options.length ? options.map(c => `<button class="destination-option" type="button" data-destination-plan="${esc(c.plan_id)}"><strong>${esc(c.name)}</strong><span>${esc(CAT[c.category] || c.category)} · 도보 ${min(c.outbound_sec)}분 · ${hhmm(c.return_at)} 복귀</span><em>이곳으로 변경 →</em></button>`).join('') : '<p class="feature-description">바로 선택할 다른 후보가 없어요. 새로 찾아볼까요?</p>'}</div>
        <form id="destinationForm" class="feature-form destination-search"><label>찾아볼 활동<select name="category">${categoryOptions().replace('지금 조건 유지', '같은 활동으로 찾기')}</select></label><label>원하는 장소나 조건 (선택)<input name="request" maxlength="200" placeholder="예: 더 가까운 곳, 테이크아웃 가능한 곳"></label><button class="feature-primary" type="submit">다른 장소 찾아보기</button></form>`;
    }
  } else if (mode === 'conditions') {
    $('#featureTitle').textContent = '이번 외출 조건';
    $('#featureBody').innerHTML = `<p class="feature-description">바꿀 항목만 입력하면 다시 추천해요.</p><form id="conditionForm" class="feature-form"><label>하고 싶은 일<select name="category">${categoryOptions()}</select></label><div class="field-pair"><label>목표 충전량 (%)<input name="target" inputmode="decimal" type="number" min="1" max="100" step="0.1" placeholder="${esc(session.effective_target_soc_pct ?? '예: 80')}"></label><label>시간 제한 (분)<input name="limit" inputmode="numeric" type="number" min="1" max="1440" placeholder="${esc(session.user_limit_min ?? '예: 30')}"></label></div><label>장소에서 머무는 시간 (분)<input name="dwell" inputmode="numeric" type="number" min="5" max="60" placeholder="예: 15"></label><label>출발 충전소<input name="station" maxlength="100" placeholder="${esc(session.origin || '충전소 이름')}"></label><p class="feature-description">목표 충전량은 외출 시간 계산 기준이에요. 차량의 충전 설정은 바꾸지 않아요.</p><button class="feature-primary" type="submit">이 조건으로 다시 추천</button></form>`;
  } else if (mode === 'preferences') {
    $('#featureTitle').textContent = '볼티가 기억하는 취향';
    const p = session.preferences;
    $('#featureBody').innerHTML = `<div class="preference-current">${esc(preferenceLabel(p || {}))}</div><p class="feature-description">이번 외출 조건과 따로 기억해요. 저장 전 내용을 한 번 더 확인할 수 있어요.</p><form id="preferenceForm" class="feature-form"><label>좋아하는 활동<select name="category">${categoryOptions(p?.preferred_category || '')}</select></label><label>평소 머무는 시간 (분)<input name="dwell" type="number" inputmode="numeric" min="5" max="60" placeholder="5~60" value="${esc(p?.dwell_min ?? '')}"></label><button class="feature-primary" type="submit">${p ? '취향 변경 요청' : '이 취향 기억하기'}</button></form>${p ? '<button class="feature-quiet" id="deletePreference" type="button">기억한 취향 지우기</button>' : ''}`;
  } else if (mode === 'remaining') {
    $('#featureTitle').textContent = '남은 충전 시간';
    $('#featureBody').innerHTML = '<p class="feature-description">차량의 남은 시간을 확인하지 못했어요. 직접 입력한 시간을 계산에 반영하는 기능은 연결 준비 중이에요.</p><button class="feature-primary" id="retryCharging" type="button">차량 정보 다시 확인</button>';
  }
  if (!dialog.open) dialog.showModal();
}
function closeFeature() {
  if ($('#featureDialog').open) return goBack();
  return Promise.resolve();
}
async function restoreConversation(id) {
  if (busy) return;
  const record = conversations.find(r => r.id === id); if (!record) return;
  saveConversation(); busy = true; setLock(); await closeFeature();
  let live = null;
  try { live = await api('/api/session?existing=1&thread_id=' + encodeURIComponent(id)); }
  catch { notifyUser('서버에 연결하지 못해 보관된 내용만 보여드려요.'); }
  threadId = id; archived = !live?.exists || live.instance_id !== record.instance_id;
  timeline = record.turns; session = record.session || {}; lastRes = record.response;
  serverInstance = record.instance_id; pendingApproval = null; receivedAt = Date.now();
  if (!archived) acceptEnvelope(live);
  routeMap.reset(); $('#messages').innerHTML = ''; $('#approval').hidden = true;
  replaying = true;
  try { for (const turn of timeline) { if (turn.role === 'user') addMe(turn.text); else if (turn.response) addBot(turn.response, turn.map_data); else if (turn.role === 'error') addError(turn.text); } }
  finally { replaying = false; busy = false; }
  routeMap.hide(); resetNavigation('chat');
  renderCar(session); renderStrip(session, lastRes); syncFeatures();
  if (archived) $('#follow').innerHTML = '<button type="button" id="archiveNew">새 대화로 추천받기</button>';
  if (pendingApproval) openApproval(lastRes || {});
  setLock(); scrollDown();
}
async function refreshVehicle() {
  if (busy || archived || !$('#approval').hidden) return;
  busy = true; setLock(); $('#refreshStatus').textContent = '차량 정보를 다시 확인하고 있어요…';
  try {
    const data = await api('/api/refresh', {thread_id: threadId, instance_id: serverInstance});
    acceptEnvelope(data); renderCar(session); renderStrip(session, null); saveConversation();
    $('#refreshStatus').textContent = '갱신했어요. 추천은 새 충전 정보로 다시 받아 주세요.';
    if (!$('#chat').hidden) $('#follow').innerHTML = '<button type="button" data-text="새 충전 정보와 지금 조건으로 다시 추천해줘">지금 조건으로 다시 추천</button>';
  } catch (error) { $('#refreshStatus').textContent = error.message; }
  finally { busy = false; setLock(); }
}
function initFeatures() {
  document.querySelectorAll('.history-open').forEach(b => b.addEventListener('click', () => openFeature('history')));
  $('#conditionsBtn').addEventListener('click', () => openFeature('conditions'));
  $('#preferencesBtn').addEventListener('click', () => openFeature('preferences'));
  $('#featureClose').addEventListener('click', () => closeFeature());
  $('#featureDialog').addEventListener('cancel', e => { e.preventDefault(); closeFeature(); });
  $('#refreshCar').addEventListener('click', refreshVehicle);
  document.addEventListener('click', async e => {
    const b = e.target.closest('button'); if (!b || b.disabled) return;
    if (b.dataset.thread) restoreConversation(b.dataset.thread);
    if (b.id === 'newConversation' || b.id === 'archiveNew') { await closeFeature(); goHome(); }
    if (b.id === 'deletePreference') { await closeFeature(); send('저장한 선호를 모두 지워줘'); }
    if (b.id === 'remainingInput') openFeature('remaining');
    if (b.hasAttribute('data-change-destination') && currentDestination()) openFeature('destination');
    if (b.id === 'keepDestination') await closeFeature();
    if (b.dataset.destinationPlan) {
      const candidate = freshDestinationOption(b.dataset.destinationPlan);
      if (!candidate || !currentDestination() || busy || pendingApproval) return;
      await closeFeature();
      send(`목적지를 ${candidate.name}으로 변경할게요`, {kind: 'plan', id: candidate.plan_id, version: candidate.version, evaluated_at: candidate.evaluated_at});
    }
    if (b.id === 'retryCharging') { await closeFeature(); openCar(); refreshVehicle(); }
    if (b.dataset.stationId) {
      const station = session.station_candidates?.find(s => s.poi_id === b.dataset.stationId);
      if (station) send(`${station.name}에서 출발할게요`, {kind: 'station', id: station.poi_id});
    }
  });
  $('#featureBody').addEventListener('submit', async e => {
    e.preventDefault();
    if (!e.target.reportValidity()) return;
    const f = new FormData(e.target), category = f.get('category'), dwell = f.get('dwell');
    if (e.target.id === 'destinationForm') {
      const current = currentDestination();
      if (!current || busy || pendingApproval) return;
      const parts = [`${current.name} 대신 갈 다른 장소를 찾아줘`, category ? `${CAT[category]}에 가고 싶어` : '활동 종류는 유지해줘', f.get('request')?.trim(), '나머지 조건은 유지하고 현재 남은 시간으로 다시 추천해줘. 내가 고르기 전에는 새 목적지를 확정하지 마'];
      await closeFeature(); send(parts.filter(Boolean).join('. '));
    } else if (e.target.id === 'preferenceForm') {
      if (!category && !dwell) { notifyUser('좋아하는 활동이나 머무는 시간을 입력해 주세요.'); return; }
      await closeFeature(); send([category ? `${CAT[category]}를 선호해` : '', dwell ? `평소 체류시간은 ${dwell}분이야` : '', '이 취향을 기억해줘'].filter(Boolean).join('. '));
    } else if (e.target.id === 'conditionForm') {
      const parts = [category ? `${CAT[category]}에 가고 싶어` : '', f.get('target') ? `목표 충전량은 ${f.get('target')}%` : '', f.get('limit') ? `시간 제한은 지금부터 ${f.get('limit')}분` : '', dwell ? `체류시간은 ${dwell}분` : '', f.get('station')?.trim() ? `출발 충전소는 ${f.get('station').trim()}` : '', '나머지 조건은 유지하고 현재 시각으로 다시 추천해줘'];
      await closeFeature(); send(parts.filter(Boolean).join('. '));
    }
  });
  setInterval(() => { updatePlanButtons(); if (!$('#approval').hidden) updateApprovalExpiry(); }, 1000);
}
