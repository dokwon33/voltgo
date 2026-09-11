  const IMG = { hello: 'img/volty_hello.png', think: 'img/volty_think.png', point: 'img/volty_point.png',
                coffee: 'img/volty_coffee.png', clock: 'img/volty_clock.png' };
  // 상태별 볼티 표정과 한 줄 (설계서 2.4 VoltGoResponse.status)
  const MOOD = {
    idle: ['hello', '어디에 다녀오고 싶으세요?'],
    busy: ['think', '충전 상태랑 근처 장소, 왕복 시간을 보고 있어요…'],
    ok: ['point', '지금 다녀올 수 있는 곳이에요. 출발 마감만 지켜 주세요!'],
    need_input: ['think', '조금만 더 알려주시면 바로 계산해 볼게요.'],
    no_feasible: ['think', '이번엔 시간이 빠듯해요. 더 가까운 곳은 어떠세요?'],
    awaiting_approval: ['clock', '실행 전에 한 번만 확인할게요.'],
    confirmed: ['coffee', '목적지를 정했어요. 지도를 보거나 다른 곳으로 바꿀 수 있어요.'],
    error: ['think', '잠깐 문제가 생겼어요. 다시 말해 주세요.'],
  };
  const CAT = { meal: '식사', cafe: '카페', convenience: '편의점', mart: '마트' };
  const PLUG = { fast: '급속', slow: '완속', none: '미연결' };

  const $ = (s) => document.querySelector(s);
  const routeMap = new VoltGoRouteMap($('#routeMap'));
  const homeCharacter = new VoltGoHomeCharacter($('#homeVolty'), $('.hero-say'), $('.hero .hand'));
  const esc = (t) => String(t ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const hhmm = (iso) => iso ? iso.slice(11, 16) : '—';
  const min = (sec) => Math.floor((sec || 0) / 60);

  let threadId = '', busy = false, health = {}, session = {}, lastRes = null;

  async function api(path, body) {
    // 사용자는 서버가 발급한 HttpOnly 쿠키로만 구분한다. 화면은 user_id 를 보내지 않는다.
    const opt = body ? { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
                     : { credentials: 'same-origin', cache: 'no-store' };
    const r = await fetch(path, opt);
    const data = await r.json().catch(() => ({ error: '서버 응답을 읽지 못했어요' }));
    if (!r.ok || data.error) {
      const error = new Error(data.error || ('HTTP ' + r.status));
      error.status = r.status;
      if (r.status === 401) sessionExpired();
      throw error;
    }
    return data;
  }

  // ---------- 대화 ID : 서버가 발급한다 ----------
  let threadPromise = null, threadRevision = 0;
  function bindThread(id) {
    threadId = id;
    if (history.state?.scope === navigationScope) history.replaceState({...history.state, voltgo: id}, '');
  }
  function startThread() {
    const revision = ++threadRevision;
    threadId = '';
    threadPromise = api('/api/session', {}).then((d) => {
      if (revision !== threadRevision) return null;        // 그사이 다른 대화로 넘어갔다
      if (!d.thread_id) throw new Error('새 대화를 준비하지 못했어요');
      bindThread(d.thread_id);
      if (!timeline.length) { acceptEnvelope(d); renderCar(session); }
      return d.thread_id;
    });
    threadPromise.catch(() => {
      if (revision !== threadRevision || timeline.length) return;
      $('#carMain').textContent = '서버에 연결하지 못했어요'; $('#carSub').textContent = '터미널에서 python scripts/web.py 를 켜고 새로고침해 주세요'; $('#carTimes').hidden = true;
    });
    return threadPromise;
  }
  async function ensureThread() {
    if (threadId) return threadId;
    const id = await (threadPromise || startThread());
    if (!id) throw new Error('새 대화를 준비하지 못했어요. 다시 시도해 주세요.');
    return id;
  }
  function forgetPendingThread() { threadRevision++; threadPromise = null; }
  let expiredOnce = false;
  function sessionExpired() {
    if (expiredOnce) return;
    expiredOnce = true;
    notifyUser('접속 세션이 만료되어 새로 시작해요.');
    api('/api/health').then(h => { health = h; loadConversations(); }).catch(() => {})
      .finally(() => { expiredOnce = false; busy = false; goHome(); });
  }

  // ---------- 홈 카드 ----------
  function renderCar(s) {
    homeCharacter.render(s, lastRes);
    const c = s.charging ?? s.display_charging, b = s.home_budget;
    const soc = typeof c?.soc_pct === 'number' && Number.isFinite(c.soc_pct) && c.soc_pct >= 0 && c.soc_pct <= 100 ? c.soc_pct : null;
    const label = soc === null ? '—' : Number(soc.toFixed(1)) + '%';
    // 화면 표시용 잔량 구간. 실제 추천/충전 판단 정책과는 별개다.
    const level = soc === null ? 'unknown' : soc <= 20 ? 'low' : soc <= 40 ? 'medium' : 'normal';
    const charging = c?.charging === true ? 'active' : c?.charging === false ? 'idle' : 'unknown';
    const status = { active: '충전 중', idle: '충전 안 함', unknown: '상태 미확인' }[charging];
    const targetPct = vehicleTarget(s);
    $('#batteryTarget').hidden = targetPct === null;
    $('#batteryTarget').style.left = targetPct + '%';
    $('#batteryTarget').title = `목표 ${targetPct}%`;
    $('#batteryEstimate').hidden = charging !== 'active';
    $('#batteryRemaining').textContent = '';
    $('#batteryFinish').textContent = '';
    if (charging === 'active') {
      // 서버 시각을 기준으로 계산해 고정 시계 시연에서도 같은 잔여시간을 표시한다.
      const nowMs = Date.parse(s.now), finishMs = Date.parse(b?.finish_at);
      const reported = typeof c.reported_remaining_sec === 'number' && Number.isFinite(c.reported_remaining_sec) && c.reported_remaining_sec >= 0 ? c.reported_remaining_sec : null;
      const basisMs = Date.parse(c.observed_at);
      const remaining = Number.isFinite(finishMs) && Number.isFinite(nowMs) ? Math.max(0, (finishMs - nowMs) / 1000) : reported !== null && Number.isFinite(basisMs) && Number.isFinite(nowMs) ? Math.max(0, reported - (nowMs - basisMs) / 1000) : reported;
      const observedMs = Date.parse(c.observed_at);
      const estimatedFinish = Number.isFinite(finishMs) ? finishMs : Number.isFinite(observedMs) && reported !== null ? observedMs + reported * 1000 : null;
      if (targetPct !== null && soc !== null && soc >= targetPct) {
        $('#batteryRemaining').textContent = `${targetPct}% 목표에 도달했어요`;
      } else {
        const duration = remaining === null ? '시간 확인 중' : remaining <= 0 ? '곧 도달' : remaining < 60 ? '1분 미만' : `약 ${Math.ceil(remaining / 60)}분`;
        $('#batteryRemaining').textContent = targetPct === null ? '차량의 목표 충전량을 확인해 주세요' : `${targetPct}%까지 ${duration}`;
        if (targetPct !== null && estimatedFinish !== null && (remaining === null || remaining > 0)) {
          const time = new Intl.DateTimeFormat('ko-KR', { timeZone: 'Asia/Seoul', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(estimatedFinish);
          $('#batteryFinish').textContent = `${time} 완료 예정`;
        }
      }
    }
    $('#batterySummary').dataset.level = level;
    $('#batterySummary').dataset.charging = charging;
    $('#batteryStatus').textContent = status;
    const captions = {
      unknown: '잔량 미확인',
      low: '잔량 부족',
      medium: '잔량 보통',
      normal: soc === 100 ? '완충' : '잔량 여유',
    };
    $('#batteryCaption').textContent = captions[level];
    $('#batteryCaption').hidden = level !== 'low' && !(level === 'unknown' && charging !== 'unknown');
    $('#batteryPct').textContent = label;
    $('#batteryFill').style.width = (soc ?? 0) + '%';
    // 앞으로 채워질 구간: 충전 중이고 차량 목표가 현재보다 높을 때만 보여준다
    const toFill = charging === 'active' && soc !== null && targetPct !== null && targetPct > soc;
    $('#batteryCharge').hidden = !toFill;
    if (toFill) { $('#batteryCharge').style.left = soc + '%'; $('#batteryCharge').style.width = (targetPct - soc) + '%'; }
    $('#batteryGauge').setAttribute('aria-valuetext', `${soc === null ? '잔량 미확인' : label} · ${targetPct === null ? '목표 미확인' : `목표 ${targetPct}%`} · ${status} · ${captions[level]}`);
    if (soc === null) $('#batteryGauge').removeAttribute('aria-valuenow');
    else $('#batteryGauge').setAttribute('aria-valuenow', soc);
    $('#soc').textContent = label;
    $('#ring').style.setProperty('--soc', soc ?? 0);
    $('#ring').style.setProperty('--battery-ring', { low: '#ed8d7a', medium: '#ebbc64', normal: '#65ce91', unknown: '#adbbb4' }[level]);
    $('#carTimes').hidden = charging !== 'active' || !b?.finish_at;
    $('#carFinish').textContent = hhmm(b?.finish_at);
    renderVehicleMeta(s);
    if (!c) {
      $('#carMain').textContent = '차량 상태를 아직 못 읽었어요';
      $('#carSub').textContent = '대화를 시작하면 다시 확인해요';
      return;
    }
    const target = targetPct !== null ? `목표 ${targetPct}%까지 ` : '';
    if (charging === 'idle') $('#carMain').textContent = '지금은 충전 중이 아니에요';
    else if (charging === 'unknown') $('#carMain').textContent = '충전 상태를 아직 확인하지 못했어요';
    else $('#carMain').textContent = $('#batteryRemaining').textContent || target + '충전 중';
    const plug = c.plug_type === 'none' ? '충전 케이블 미연결' : PLUG[c.plug_type] ? PLUG[c.plug_type] + ' 충전' : '';
    $('#carSub').textContent = [plug, s.origin].filter(Boolean).join(' · ');
  }

  // ---------- 대화 화면 ----------
  function setMood(key) {
    const [img, say] = MOOD[key] || MOOD.busy;
    $('#moodImg').src = IMG[img];
    $('#moodSay').textContent = say;
  }
  function renderStrip(s, res) {
    const b = s.time_budget;
    const finish = b ? b.finish_at : res && res.finish_at;
    const dl = b ? b.return_deadline : res && res.return_deadline;
    if (!finish && !dl) { $('#strip').hidden = true; return; }
    $('#strip').hidden = false;
    $('#strip').innerHTML = `<span>충전 완료 <b>${hhmm(finish)}</b></span><span>복귀 마감 <b>${hhmm(dl)}</b></span>` +
      (b ? `<span>여유 <b>${min(b.available_sec)}분</b></span>` : '');
  }
  function scrollDown() { const m = $('#messages'); m.scrollTop = m.scrollHeight; }

  function addMe(text) {
    recordTurn({role: 'user', text});
    $('#messages').insertAdjacentHTML('beforeend', `<div class="row me"><div class="say"><p>${esc(text)}</p></div></div>`);
    scrollDown();
  }
  function addTyping() {
    $('#messages').insertAdjacentHTML('beforeend',
      `<div class="row bot" id="typing"><img class="mini" src="${IMG.think}" alt=""><div class="col"><div class="say"><span class="typing"><i></i><i></i><i></i></span></div></div></div>`);
    scrollDown();
  }
  function removeTyping() { const t = $('#typing'); if (t) t.remove(); }

  function planCard(c, status, mapKey) {
    const label = routeMap.entries.get(mapKey)?.label || '';
    const done = status === 'confirmed';
    const seg = (sec, cls) => sec > 0 ? `<span class="seg ${cls}" style="flex:${sec}"></span>` : '';
    return `
      <article class="plan${done ? ' done' : ''}" ${done ? `data-confirmed-plan="${esc(c.plan_id)}"` : ''} data-version="${esc(c.version)}" data-evaluated="${esc(c.evaluated_at)}">
        ${done ? '<p class="destination-label">현재 목적지</p>' : ''}
        <div class="plan-head"><span class="plan-label" aria-label="추천 ${esc(label)}">${esc(label)}</span><span class="cat">${esc(CAT[c.category] || c.category)}</span><h4>${esc(c.name)}</h4></div>
        <p class="plan-line">걸어서 ${min(c.outbound_sec)}분 · ${min(c.dwell_sec)}분 머물고 · <b>${hhmm(c.return_at)}</b> 복귀
          <span class="slack">여유 ${min(c.slack_sec)}분</span></p>
        <div class="tbar" title="가는 길 ${min(c.outbound_sec)}분 / 체류 ${min(c.dwell_sec)}분 / 오는 길 ${min(c.inbound_sec)}분 / 여유 ${min(c.slack_sec)}분">
          ${seg(c.outbound_sec, 'out')}${seg(c.dwell_sec, 'stay')}${seg(c.inbound_sec, 'in')}${seg(c.slack_sec, 'slack')}</div>
        <div class="plan-foot">
          <span class="leave">늦어도 <b>${hhmm(c.leave_by)}</b> 출발</span>
          ${done ? '<span class="done-tag">확정한 계획</span>'
                 : `<button type="button" class="pick" data-plan-id="${esc(c.plan_id)}" data-plan-name="${esc(c.name)}" data-plan-label="${esc(label)}" data-version="${esc(c.version)}" data-evaluated="${esc(c.evaluated_at)}">${esc(label)}로 갈래요</button>`}
        </div>
        ${done ? `<div class="destination-actions">
          <button class="map-open destination-map" type="button" data-map-key="${mapKey}" aria-controls="routeMap" aria-pressed="false" aria-label="${esc(c.name)} 목적지 지도 보기"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m3 6 6-3 6 3 6-3v15l-6 3-6-3-6 3V6ZM9 3v15M15 6v15"/></svg><span>목적지 지도 보기</span><span aria-hidden="true">→</span></button>
          <button class="destination-change" type="button" data-change-destination>다른 장소로 변경</button>
        </div>` : ''}
        <details class="route-details"><summary>왕복 시간·경로 자세히</summary><p>가는 길 ${min(c.outbound_sec)}분 · 머무는 시간 ${min(c.dwell_sec)}분 · 오는 길 ${min(c.inbound_sec)}분</p><p>${esc(c.route_summary || '경로 설명을 확인하지 못했어요.')}</p></details>
        <p class="stale-note" hidden>지난 추천 · 현재 조건으로 다시 추천받아 주세요.</p>
        ${done ? '' : `<button class="map-open" type="button" data-map-key="${mapKey}" aria-controls="routeMap" aria-pressed="false">지도에서 보기</button>`}
      </article>`;
  }

  function addBot(res, mapData) {
    recordTurn({role: 'bot', response: res, map_data: mapData});
    const cands = res.candidates || [];
    const mapKeys = routeMap.register(res, mapData);
    const mood = MOOD[res.status] ? res.status : 'busy';
    // 후보는 카드로, 시간 요약은 위 띠로 표시한다. 보조 입력 안내와 주석은 대화에서 생략한다.
    const lines = (res.message || '').split('\n').filter((l) => l.trim() && !(cands.length && /^\d+\)\s/.test(l))
      && !/^(?:※|알려주시면 좋은 것:|충전 완료 예정)/.test(l.trim()));
    const confirmed = res.status === 'confirmed' && cands[0];
    const html = `
      <div class="row bot">
        <img class="mini" src="${IMG[MOOD[mood][0]]}" alt="">
        <div class="col">
          ${lines.length ? `<div class="say">${lines.map((l) => `<p>${esc(l)}</p>`).join('')}</div>` : ''}
          ${confirmed ? `<div class="arrival"><div><span>늦어도 출발</span><b>${hhmm(confirmed.leave_by)}</b></div><div><span>차량 복귀</span><b>${hhmm(confirmed.return_at)}</b></div></div>` : ''}
          ${cands.length ? cands.map((c, i) => planCard(c, res.status, mapKeys[i])).join('') : ''}
        </div>
      </div>`;
    $('#messages').insertAdjacentHTML('beforeend', html);
    // A reply arriving after Home was clicked must not reopen a map or change the page.
    if (!replaying && sameScope(history.state) && history.state.view === 'map') {
      if (mapKeys.length) navigateTo('map', {mapKey: mapKeys[0], replace: true});
      else navigateTo('chat', {replace: true});
    }
    setMood(mood);
    renderFollow(res);
    if (!replaying && res.status === 'awaiting_approval' && !$('#chat').hidden && !overlayViews.has(history.state?.view)) openApproval(res);
    syncFeatures();
    scrollDown();
  }

  function addError(text) {
    recordTurn({role: 'error', text});
    $('#messages').insertAdjacentHTML('beforeend',
      `<div class="row bot"><img class="mini" src="${IMG.think}" alt=""><div class="col"><div class="say"><p>${esc(text)}</p></div></div></div>`);
    setMood('error'); $('#follow').innerHTML = ''; scrollDown();
  }

  // 답변 다음에 이어 말할 만한 것들
  function renderFollow(res) {
    const c = (res.candidates || [])[0];
    let items = [];
    if (res.status === 'ok' && c) items = [['20분은 걸릴 것 같아요', `${CAT[c.category] || '체류'}는 20분 걸려`], ['카페로 바꿔볼래요', '카페로 바꿔서 다시 찾아줘']];
    else if (res.status === 'no_feasible') items = [['편의점도 괜찮아요', '편의점도 괜찮아. 다시 찾아줘'], ['시간을 줄여볼게요', '체류는 10분이면 돼']];
    else if (res.status === 'confirmed' && c) items = [['이 취향 기억해줘', `${CAT[c.category] || ''}를 선호해. 다음에도 기억해줘`]];
    if (session.preferences && res.status !== 'awaiting_approval') items.push(['기억한 취향 지우기', '저장한 선호 지워줘']);
    $('#follow').innerHTML = items.map(([label, text]) => `<button type="button" data-text="${esc(text)}">${esc(label)}</button>`).join('');
    if (res.status === 'need_input' && res.missing_fields?.includes('remaining_min')) {
      $('#follow').insertAdjacentHTML('beforeend', '<button type="button" id="remainingInput">남은 충전 시간 확인</button>');
    }
  }

  // ---------- 승인 시트 ----------
  function openApproval(res, record = true) {
    if (record) {
      if (history.state?.view === 'approval') openApproval(res, false);
      else navigateTo('approval');
      return;
    }
    const c = (res.candidates || [])[0];
    const isSave = !c && /선호를 저장/.test(res.message || '');
    if (isSave) {
      $('#apTitle').textContent = '이 취향을 기억해둘까요?';
      $('#apBody').textContent = '다음 대화부터 그 취향에 맞는 곳을 먼저 보여드려요. 승인해야 저장돼요.';
      $('#approve').textContent = '네, 기억해줘'; $('#reject').textContent = '괜찮아요';
    } else {
      $('#apTitle').textContent = '이 계획으로 확정할까요?';
      $('#apBody').textContent = (c ? `${c.name} · 늦어도 ${hhmm(c.leave_by)} 출발. ` : '') + '지금 시각으로 한 번 더 확인한 뒤 확정해요.';
      $('#approve').textContent = '네, 확정할게요'; $('#reject').textContent = '다시 볼래요';
    }
    if (pendingApproval?.actions?.length) {
      $('#apTitle').textContent = session.preferences ? '취향을 바꿔 기억할까요?' : '이 취향을 기억해둘까요?';
      $('#apBody').textContent = pendingApproval.actions.map(a => preferenceLabel(a.args || {})).join(' / ') + '\n승인한 내용만 다음 대화에 반영해요.';
      $('#approve').textContent = '네, 기억해줘'; $('#reject').textContent = '저장 안 할게요';
    }
    $('#approval').hidden = false;
    updateApprovalExpiry();
    setLock();
  }

  function setLock() {
    const awaiting = Boolean(pendingApproval) || !$('#approval').hidden;
    $('#pendingReview').hidden = !pendingApproval || !$('#approval').hidden;
    $('#pendingReview').disabled = busy;
    $('#input').disabled = busy || awaiting || archived;
    $('#send').disabled = busy || awaiting || archived;
    $('#input').placeholder = archived ? '새 대화에서 다시 이야기해요' : awaiting ? '위에서 먼저 결정해 주세요' : '볼티에게 말하기';
    document.querySelectorAll('#follow button, #intents button').forEach((b) => (b.disabled = busy || awaiting));
    $('#approve').disabled = busy || approvalExpired(); $('#reject').disabled = busy;
    document.querySelectorAll('.history-open, #conditionsBtn, #preferencesBtn, #refreshCar').forEach(b => b.disabled = busy || awaiting || (archived && !b.classList.contains('history-open')));
    updatePlanButtons();
  }

  // ---------- 실행 정보 ----------
  function renderInfo() {
    const p = session.preferences, k = session.counters || {};
    const rows = [
      ['시계', health.clock === 'fixed' ? '2026-09-10 14:00 고정' : '실제 시각'],
      ['모델', health.model || '—'],
      ['기억한 취향', p ? [CAT[p.preferred_category], p.dwell_min ? `체류 ${p.dwell_min}분` : ''].filter(Boolean).join(' · ') : '없음'],
      ['조건 버전', session.condition_version ? 'v' + session.condition_version + (session.confirmed ? ' · 확정 ' + session.confirmed.plan_id : '') : '—'],
      ['호출 (모델 / 도구 / API)', `${k.model ?? 0} / ${k.tool ?? 0} / ${k.api ?? 0}`],
      ['thread', threadId],
    ];
    $('#infoList').innerHTML = rows.map(([a, b]) => `<div><span>${esc(a)}</span><b>${esc(b)}</b></div>`).join('');
  }

  // Navigation entries keep the background of a dialog as well as the selected map.
  let navigationScope = '', backInFlight = null, finishBack = null;
  const overlayViews = new Set(['car', 'panel', 'info', 'approval']);
  function sameScope(state) { return Boolean(navigationScope) && state?.scope === navigationScope; }
  function backgroundView(state = history.state) {
    if (!sameScope(state)) return {view: 'home', mapKey: null};
    return overlayViews.has(state.view) ? state.background : {view: state.view, mapKey: state.mapKey};
  }
  function resetNavigation(view = 'home') {
    navigationScope = crypto.randomUUID();
    const state = {voltgo: threadId, scope: navigationScope, depth: 0, view, mapKey: null};
    history.replaceState(state, '');
    renderNavigation(state);
  }
  function navigateTo(view, options = {}) {
    const previous = history.state;
    const mapKey = view === 'map' ? options.mapKey ?? routeMap.selected ?? null : null;
    if (sameScope(previous) && previous.view === view && previous.mapKey === mapKey &&
        (view !== 'panel' || previous.panelMode === options.panelMode)) return;
    const state = {voltgo: threadId, scope: navigationScope,
      depth: sameScope(previous) ? previous.depth + (options.replace ? 0 : 1) : 0,
      view, mapKey, panelMode: options.panelMode,
      background: overlayViews.has(view) ? backgroundView(previous) : null};
    history[options.replace ? 'replaceState' : 'pushState'](state, '');
    renderNavigation(state);
  }
  function showChat(record = true) {
    if (record && backgroundView().view !== 'chat') { navigateTo('chat'); return; }
    $('#home').hidden = true; $('#chat').hidden = false;
    $('#backBtn').hidden = false;
  }
  function showHome() {
    routeMap.hide();
    $('#chat').hidden = true; $('#home').hidden = false;
    $('#backBtn').hidden = true;
    renderCar(session);
  }
  function renderNavigation(state) {
    if ($('#carDialog').open && state.view !== 'car') $('#carDialog').close();
    if ($('#featureDialog').open && state.view !== 'panel') $('#featureDialog').close();
    $('#info').hidden = state.view !== 'info';
    $('#approval').hidden = state.view !== 'approval';
    const base = overlayViews.has(state.view) ? state.background : state;
    $('#chat').classList.toggle('is-map', base?.view === 'map');
    $('#mapBtn').setAttribute('aria-pressed', String(base?.view === 'map'));
    if (base?.view === 'home') {
      if (archived) { goHome(); return; }
      showHome();
    } else {
      showChat(false);
      if (base?.view === 'map') {
        if (routeMap.entries.has(base.mapKey)) {
          if (routeMap.panel.hidden || routeMap.selected !== base.mapKey) routeMap.select(base.mapKey, false, false);
        } else showEmptyMap();
      } else routeMap.hide();
    }
    if (state.view === 'car') openCar(false);
    if (state.view === 'panel') openFeature(state.panelMode, false);
    if (state.view === 'info') renderInfo();
    if (state.view === 'approval') {
      if (pendingApproval) openApproval(lastRes || {}, false);
      else { navigateTo(base?.view || 'chat', {mapKey: base?.mapKey, replace: true}); return; }
    }
    setLock();
  }
  function goBack() {
    if (backInFlight) return backInFlight;
    if (sameScope(history.state) && history.state.depth > 0) {
      backInFlight = new Promise(resolve => { finishBack = resolve; });
      history.back();
      return backInFlight;
    }
    navigateTo('home', {replace: true});
    return Promise.resolve();
  }
  async function navigateHome() {
    if (backInFlight) await backInFlight;
    saveConversation();
    if (archived) { goHome(); return; }
    navigateTo('home');
  }
  function showEmptyMap() {
    routeMap.hide();
    routeMap.overview(currentMapData);
  }
  routeMap.onSelect = () => navigateTo('map', {
    mapKey: routeMap.selected, replace: sameScope(history.state) && history.state.view === 'map',
  });
  function openMap() {
    const destination = currentDestination();
    const confirmedKey = destination && [...routeMap.entries].reverse().find(([, e]) => e.candidate.plan_id === destination.plan_id && e.candidate.version === destination.version)?.[0];
    const key = confirmedKey || (routeMap.entries.has(routeMap.selected) ? routeMap.selected : [...routeMap.entries.keys()].at(-1));
    navigateTo('map', {mapKey: key || null});
  }
  function openCar(record = true) {
    if (record) { navigateTo('car'); return; }
    renderCar(session);
    if (!$('#carDialog').open) $('#carDialog').showModal();
  }
  function closeCar() { return goBack(); }
  $('#brandHome').addEventListener('click', navigateHome);
  $('#mapBtn').addEventListener('click', openMap);
  $('#carBtn').addEventListener('click', () => openCar());
  $('#carClose').addEventListener('click', closeCar);
  $('#carDialog').addEventListener('cancel', e => { e.preventDefault(); closeCar(); });
  $('#carDialog').addEventListener('click', e => {
    const rect = $('#carDialog').getBoundingClientRect();
    if (e.target === $('#carDialog') && (e.clientX < rect.left || e.clientX > rect.right || e.clientY < rect.top || e.clientY > rect.bottom)) closeCar();
  });
  $('#backBtn').addEventListener('click', goBack);
  $('.map-back').addEventListener('click', goBack);
  $('.map-start').addEventListener('click', () => {
    navigateTo(timeline.length ? 'chat' : 'home');
    $('#input').focus();
  });
  window.addEventListener('popstate', event => {
    if (sameScope(event.state)) renderNavigation(event.state);
    else resetNavigation('home'); // A different conversation's old entries must not restore this thread's map.
    const resolve = finishBack; backInFlight = null; finishBack = null; resolve?.();
  });

  async function send(text, selection = null) {
    text = (text || '').trim();
    if (!text || busy || archived || pendingApproval || !$('#approval').hidden) return;
    // 홈에서 시작하는 요청은 새 대화다. 기존 대화는 기록에 보관한다.
    if (!$('#home').hidden && timeline.length) goHome({loadSession: false});
    showChat(); addMe(text); $('#input').value = '';
    busy = true; setLock(); setMood('busy'); addTyping();
    try {
      const d = await api('/api/ask', { thread_id: await ensureThread(), text, selection });
      acceptEnvelope(d); lastRes = d.response; renderCar(session);
      removeTyping(); renderStrip(session, d.response); addBot(d.response, d.map_data);
    } catch (e) { removeTyping(); addError(e.message); }
    finally { busy = false; setLock(); saveConversation(); }
  }

  async function decideNow(decision) {
    if (busy || (decision === 'approve' && approvalExpired())) return;
    const label = decision === 'approve' ? $('#approve').textContent : $('#reject').textContent;
    busy = true; setLock();
    if (history.state?.view === 'approval') await goBack();
    $('#approval').hidden = true;
    addMe(label);
    busy = true; setLock(); setMood('busy'); addTyping();
    try {
      const d = await api('/api/decide', { thread_id: threadId, decision, approval_id: pendingApproval?.approval_id });
      acceptEnvelope(d); lastRes = d.response; renderCar(session);
      removeTyping(); renderStrip(session, d.response); addBot(d.response, d.map_data);
    } catch (e) { removeTyping(); addError(e.message); if (pendingApproval) openApproval(lastRes || {}); }
    finally { busy = false; setLock(); saveConversation(); }
  }

  function goHome({loadSession = true} = {}) {
    if (busy) return;
    saveConversation(); timeline = []; archived = false; pendingApproval = null; serverInstance = health.instance_id || '';
    $('#archiveNote').hidden = true; $('#stationOptions').hidden = true; $('#conditionSummary').hidden = true;
    if ($('#carDialog').open) $('#carDialog').close();
    routeMap.reset();
    session = {}; lastRes = null;
    currentMapData = {};
    setMood('idle');
    renderCar(session);
    $('#backBtn').hidden = true;
    $('#messages').innerHTML = ''; $('#follow').innerHTML = ''; $('#strip').hidden = true;
    $('#approval').hidden = true; $('#chat').hidden = true; $('#home').hidden = false;
    busy = false; setLock();
    startThread();        // 첫 질문 전이면 받은 Session 으로 홈 카드를 채운다
    resetNavigation('home');
  }

  $('#composer').addEventListener('submit', (e) => { e.preventDefault(); send($('#input').value); });
  document.addEventListener('click', (e) => {
    const mapButton = e.target.closest('button[data-map-key]');
    if (mapButton) { routeMap.select(mapButton.dataset.mapKey, true); return; }
    const plan = e.target.closest('button[data-plan-id]');
    if (plan && !plan.disabled) { send(`${plan.dataset.planLabel}. ${plan.dataset.planName}으로 갈게요`, {kind: 'plan', id: plan.dataset.planId, version: Number(plan.dataset.version), evaluated_at: plan.dataset.evaluated}); return; }
    const b = e.target.closest('button[data-text]');
    if (b && !b.disabled) send(b.dataset.text);
  });
  $('#approve').addEventListener('click', () => decideNow('approve'));
  $('#reject').addEventListener('click', () => decideNow('reject'));
  $('#pendingReview').addEventListener('click', () => openApproval(lastRes || {}));
  $('#homeVolty').addEventListener('click', (e) => {          // 누르면 다시 인사
    const volty = e.currentTarget;
    volty.classList.remove('greet'); void volty.offsetWidth; volty.classList.add('greet');
  });
  $('#infoBtn').addEventListener('click', () => navigateTo('info'));
  $('#infoClose').addEventListener('click', goBack);
  $('#info').addEventListener('click', e => { if (e.target === $('#info')) goBack(); });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && (!$('#info').hidden || !$('#approval').hidden)) { e.preventDefault(); goBack(); }
  });

  initFeatures();
  api('/api/health').then(h => { health = h; }).catch(() => {}).finally(() => { loadConversations(); goHome(); });
