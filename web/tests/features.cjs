// Run with @playwright/test installed: NODE_PATH=$(npm root -g) node web/tests/features.cjs
const { chromium } = require('@playwright/test');
const fs = require('node:fs/promises');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 390, height: 844 }, reducedMotion: 'reduce' });
    page.setDefaultTimeout(15000);
    const errors = [], requests = []; page.on('pageerror', error => { errors.push(error.message); console.error('PAGE:', error.message); });
    let instance = 'server-one', pending = null, nextResponse = null;
    const now = '2026-09-11T10:48:00+09:00';
    const candidate = { plan_id: 'A', poi_id: 'A', version: 1, name: '잠깐의 커피', category: 'cafe', outbound_sec: 240, inbound_sec: 300, dwell_sec: 900, total_sec: 1440, slack_sec: 660, return_at: '2026-09-11T11:12:00+09:00', leave_by: '2026-09-11T10:59:00+09:00', evaluated_at: now, route_summary: '왕복 650m · 조용한 골목길', poi_source: 'mock', route_source: 'mock', opening_status: 'unknown' };
    let session = {now, user_id: 'test', condition_version: 1, origin: '역삼역 EV충전소', candidates: [], charging: { soc_pct: 68, charging: true, plug_type: 'fast', target_soc_pct: 80, reported_target_soc_pct: 80, observed_at: now, source: 'mock', reported_remaining_sec: 2400 }, effective_target_soc_pct: 80, home_budget: { finish_at: '2026-09-11T11:28:00+09:00', estimate_basis: 'reported_remaining' }};
    let threadCount = 0;
    const envelope = response => ({session, thread_id: 'features-' + threadCount, instance_id: instance, pending_approval: pending, response, exists: true, map_data: { origin: {name: '역삼역 EV충전소', latitude: 37.5, longitude: 127.03}, places: [{poi_id: 'A', name: '잠깐의 커피', latitude: 37.501, longitude: 127.032}], routes: [] }});
    await page.route('**/*', async route => {
      const u = new URL(route.request().url());
      if (u.hostname !== 'voltgo.test') return route.abort();
      if (u.pathname === '/api/health') return route.fulfill({json: {clock: 'fixed', user_id: 'test', instance_id: instance, charging: 'mock', tmap: 'mock'}});
      if (u.pathname.startsWith('/api/')) {
        const body = route.request().postDataJSON(); requests.push({path: u.pathname, body});
        if (u.pathname === '/api/session' && route.request().method() === 'POST') threadCount++;
        let response = null;
        if (u.pathname === '/api/ask') {
          response = nextResponse || {status: 'ok', message: '잠깐 쉬어가기 좋은 곳을 찾았어요.', candidates: [{...candidate, version: session.condition_version}], warnings: []};
          nextResponse = null;
          session.candidates = response.candidates || [];
        }
        if (u.pathname === '/api/charging/refresh') { session.condition_version += 1; session.candidates = []; }
        if (u.pathname === '/api/decide') { pending = null; session.preferences = {preferred_category: 'cafe', dwell_min: 15}; response = {status: 'ok', message: '취향을 기억했어요.', candidates: []}; }
        return route.fulfill({json: envelope(response)});
      }
      const file = u.pathname === '/' ? '/index.html' : u.pathname;
      const ext = path.extname(file);
      return route.fulfill({contentType: {'.css':'text/css','.js':'application/javascript','.woff2':'font/woff2','.png':'image/png','.html':'text/html'}[ext] || 'application/octet-stream', body: await fs.readFile(root + file)});
    });
    await page.goto('https://voltgo.test');
    await page.waitForFunction(() => document.querySelector('#batteryPct').textContent === '68%');
    await page.evaluate(() => document.fonts.ready);
    assert.equal(await page.locator('#resumeBtn').count(), 0);
    assert.equal(await page.locator('#batteryRemaining').innerText(), '80%까지 약 40분');
    assert.deepEqual(await page.locator('#batteryCharge').evaluate(el => [el.hidden, el.style.left, el.style.width]), [false, '68%', '12%']);
    await page.screenshot({path: '/tmp/voltgo-front-home.png', fullPage: true});
    await page.locator('#home .history-open').click(); assert.match(await page.locator('#featureBody').innerText(), /아직 대화/);
    await page.locator('#featureClose').click(); await page.waitForFunction(() => history.state.view === 'home');
    await page.locator('#input').fill('커피 마시고 싶어'); await page.locator('#send').click();
    await page.waitForFunction(() => !busy && !!document.querySelector('[data-plan-id]'));
    assert.equal(await page.locator('[data-plan-id]').isEnabled(), true);
    await page.locator('.route-details summary').click(); assert.match(await page.locator('.route-details').innerText(), /오는 길 5분/);
    await page.locator('[data-plan-id]').click();
    await page.waitForFunction(() => !busy);
    const selectRequest = requests.find(r => r.body?.selection?.kind === 'plan');
    assert.equal(selectRequest.body.selection.version, 1); assert.equal(selectRequest.body.selection.evaluated_at, now);
    await page.locator('#conditionsBtn').click();
    assert.equal(await page.locator('[name=target]').count(), 0);
    assert.equal(await page.locator('#vehicleTarget').inputValue(), '80%');
    assert.equal(await page.locator('#vehicleTarget').isEditable(), false);
    await page.locator('[name=limit]').fill('25'); await page.locator('[name=dwell]').fill('10');
    session.condition_version = 2;
    await page.locator('#conditionForm button').click(); await page.waitForFunction(() => !busy && document.querySelectorAll('[data-plan-id]').length === 3);
    assert.equal(await page.locator('[data-plan-id]').first().isDisabled(), true);
    assert.equal(await page.locator('[data-plan-id]').last().isEnabled(), true);
    assert.match(requests.at(-1).body.text, /25분.*10분/);
    assert.doesNotMatch(requests.at(-1).body.text, /목표 충전량|\d+%/);
    await page.screenshot({path: '/tmp/voltgo-front-chat.png', fullPage: true});
    await page.locator('#carBtn').click(); await page.locator('#refreshCar').click();
    await page.waitForFunction(() => !busy && document.querySelector('#refreshStatus').textContent.includes('갱신했어요'));
    assert.equal(await page.locator('[data-plan-id]').last().isDisabled(), true);
    assert.equal(await page.locator('#carDialog').innerText().then(t => t.includes('돌아올 시간')), false);
    await page.screenshot({path: '/tmp/voltgo-front-car.png', fullPage: true});
    await page.locator('#carClose').click(); await page.waitForFunction(() => !document.querySelector('#carDialog').open);
    session.station_candidates = [{poi_id: 'S1', name: '역삼역 동쪽 충전소', address: '서울 강남구 테헤란로 1'}];
    nextResponse = {status: 'need_input', message: '출발 충전소를 골라 주세요.', missing_fields: ['origin'], candidates: []};
    await page.locator('#input').fill('역삼역 충전소 찾아줘'); await page.locator('#send').click(); await page.waitForFunction(() => !busy);
    assert.match(await page.locator('#stationOptions').innerText(), /테헤란로 1/);
    await page.locator('[data-station-id=S1]').click(); await page.waitForFunction(() => !busy);
    assert.equal(requests.at(-1).body.selection.id, 'S1');
    session.station_candidates = [];
    await page.locator('#preferencesBtn').click(); await page.locator('[name=category]').selectOption('cafe'); await page.locator('[name=dwell]').fill('15');
    pending = {approval_id: 'approval-one', expires_at: '2026-09-11T10:50:00+09:00', actions: [{name: 'save_preferences', args: {category: 'cafe', dwell_min: 15}}]};
    nextResponse = {status: 'awaiting_approval', message: '선호를 저장할까요?', candidates: []};
    await page.locator('#preferenceForm button').click(); await page.waitForFunction(() => !busy && !document.querySelector('#approval').hidden);
    assert.match(await page.locator('#apBody').innerText(), /카페 · 머무는 시간 15분/);
    await page.locator('#approve').click(); await page.waitForFunction(() => !busy);
    assert.equal(requests.at(-1).body.approval_id, 'approval-one');
    await page.locator('#chat .history-open').click(); assert.equal(await page.locator('.history-item').count(), 1);
    await page.screenshot({path: '/tmp/voltgo-front-history.png', fullPage: true});
    await page.locator('#newConversation').click(); await page.waitForFunction(() => !document.querySelector('#home').hidden);
    await page.locator('#home .history-open').click(); await page.locator('.history-item').click();
    await page.waitForFunction(() => !busy && !document.querySelector('#chat').hidden);
    assert.equal(await page.locator('#input').isEnabled(), true);
    await page.reload(); await page.waitForFunction(() => document.querySelector('#batteryPct').textContent === '68%');
    await page.locator('#home .history-open').click(); assert.equal(await page.locator('.history-item').count(), 1);
    instance = 'server-two';
    await page.locator('.history-item').click(); await page.waitForFunction(() => !busy && !document.querySelector('#archiveNote').hidden);
    assert.equal(await page.locator('#input').isDisabled(), true);
    assert.equal(await page.locator('[data-plan-id]').last().isDisabled(), true);
    await page.locator('#archiveNew').click();
    // Wait for the new thread's session response before testing manually supplied battery states.
    await page.waitForFunction(() => !document.querySelector('#home').hidden && session.charging?.soc_pct === 68);
    await page.evaluate(() => renderCar({charging: {charging:true, soc_pct:40, target_soc_pct:60, reported_target_soc_pct:80}, display_charging: {charging:true, soc_pct:40, target_soc_pct:60}, effective_target_soc_pct:60}));
    assert.equal(await page.locator('#batteryTarget').getAttribute('title'), '목표 80%');
    assert.match(await page.locator('#vehicleMeta').innerText(), /80%/);
    await page.evaluate(() => renderCar({charging: {charging:true, soc_pct: 15, target_soc_pct: null}}));
    assert.equal(await page.locator('#batteryTarget').isVisible(), false);
    assert.equal(await page.locator('#batteryCharge').isVisible(), false, 'no fill-to zone without a vehicle target');
    assert.match(await page.locator('#batteryRemaining').innerText(), /목표 충전량/);
    assert.equal(await page.locator('#batterySummary').getAttribute('data-level'), 'low');
    await page.setViewportSize({width: 320, height: 700});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.locator('#input').fill('다시 추천해줘'); await page.locator('#send').click(); await page.waitForFunction(() => !busy);
    await page.locator('#conditionsBtn').click();
    assert.equal(await page.locator('#featureDialog').evaluate(e => e.scrollWidth > e.clientWidth), false);
    await page.screenshot({path: '/tmp/voltgo-front-conditions-small.png', fullPage: true});
    assert.deepEqual(errors, []);
    console.log('PASS: history save/reload/live/archive, metadata selection, stale cards, route details, conditions, stations, preference approval ID, vehicle refresh, unknown target, 320px layout.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
